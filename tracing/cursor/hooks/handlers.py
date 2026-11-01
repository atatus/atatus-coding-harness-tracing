#!/usr/bin/env python3
"""Cursor hook handlers: one dispatcher routes all IDE and CLI events."""
import hashlib
import json
import sys

from core.common import (
    LLM_EFFORT_ATTR,
    LLM_THINKING_ATTR,
    build_span,
    env,
    error,
    generate_trace_id,
    get_timestamp_ms,
    log,
    normalize_effort,
    read_stdin_text,
    redact_content,
    send_span,
    send_span_async,
)
from tracing.cursor.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    active_turn_add_response,
    active_turn_add_thought_hash,
    active_turn_clear,
    active_turn_clear_if_matches,
    active_turn_get,
    active_turn_record_alias,
    active_turn_save,
    active_turn_update,
    check_requirements,
    conversation_cleanup,
    gc_stale_state_files,
    gen_root_span_get,
    gen_root_span_save,
    sanitize,
    span_id_16,
    state_cleanup_generation,
    state_pop,
    state_push,
    terminal_nogen_claim,
    terminal_nogen_clear,
    terminal_turn_claim_many,
    terminal_turn_is_marked,
    terminal_turn_mark,
    terminal_turn_mark_many,
    trace_id_from_generation,
    truncate_attr,
    turn_matches_generation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send_span_async(span_dict: dict, on_success=None) -> None:
    """Detached span send. ``sender`` keeps this module's ``send_span`` binding
    on the synchronous fallback path so test doubles still intercept it."""
    send_span_async(span_dict, sender=send_span, on_success=on_success)


def _print_permissive(event: str) -> None:
    """Print the permissive JSON response to stdout.

    before* events -> {"permission": "allow"}
    all others     -> {"continue": true}

    Uses sys.__stdout__ (the original stdout saved by Python) in case
    sys.stdout has been redirected.
    """
    stdout = sys.__stdout__ or sys.stdout
    if event.startswith("before"):
        stdout.write('{"permission": "allow"}')
    else:
        stdout.write('{"continue": true}')
    stdout.flush()


def _jq_str(input_json: dict, *keys, default: str = "") -> str:
    for k in keys:
        v = input_json.get(k)
        if v is not None and v != "":
            if isinstance(v, (dict, list)):
                return json.dumps(v)
            return str(v)
    return default


#: Anthropic and Grok models report the level under ``effort``, OpenAI ones
#: under ``reasoning``. Only one is ever present for a given model.
_EFFORT_PARAM_IDS = ("effort", "reasoning")


def _model_settings(input_json: dict) -> tuple[str, bool]:
    """Return (effort, thinking) from the payload's structured model params.

    The payload's ``model`` is a variant slug that already has both baked into
    it (``claude-opus-5-thinking-high``), so the parsed params are the only
    place they can be read as values rather than pattern-matched out of a name.
    """
    params = input_json.get("model_params")
    if not isinstance(params, list):
        params = input_json.get("modelParams")
    if not isinstance(params, list):
        return "", False
    values = {
        item["id"]: item.get("value")
        for item in params
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    effort = ""
    for key in _EFFORT_PARAM_IDS:
        effort = normalize_effort(values.get(key))
        if effort:
            break
    return effort, str(values.get("thinking", "")).strip().lower() == "true"


def _resolve_user_id(input_json: dict) -> str:
    """env.get_user_id(SERVICE_NAME) (global config < harnesses.cursor.user_id < ATATUS_USER_ID env)
    > payload `user_email` > "".
    """
    user_id = env.get_user_id(SERVICE_NAME)
    if user_id:
        return user_id
    return _jq_str(input_json, "user_email", "userEmail", "email", default="")


def _resolve_user_login_id(input_json: dict) -> str:
    """Auto-detected login identity only, no manual override."""
    return _jq_str(input_json, "user_email")


def _to_int(v) -> "int | None":
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _event_name(input_json: dict) -> str:
    return _jq_str(input_json, "hook_event_name", "hookEventName", "event_name", "eventName", "event")


def _is_cursor_ide_hook_payload(input_json: dict) -> bool:
    if input_json.get("hook_event_name"):
        return True
    if input_json.get("hookEventName"):
        return False
    return "hook_event_name" in input_json or "hookEventName" not in input_json


def _trace_id_from_event(gen_id: str, conversation_id: str) -> str:
    if conversation_id:
        return trace_id_from_generation(conversation_id)
    if gen_id:
        return trace_id_from_generation(gen_id)
    return generate_trace_id()


def _turn_state_key(conversation_id: str, gen_id: str) -> str:
    # Falls back to a generation-derived key when Cursor omits conversation_id, so the turn is still stored and closable by stop.
    if conversation_id:
        return conversation_id
    if gen_id:
        return f"__gen__:{gen_id}"
    return ""


def _resolve_turn_readonly(conversation_id: str, gen_id: str, trace_id: str, now_ms: int):
    # An active turn outranks a saved generation root unless that root is the active turn's own.
    turn_key = _turn_state_key(conversation_id, gen_id)
    active = active_turn_get(turn_key)
    if active and active.get("root_span_id") and active.get("trace_id"):
        if gen_id and not turn_matches_generation(active, gen_id) and terminal_turn_is_marked(turn_key, gen_id):
            direct_parent = gen_root_span_get(gen_id)
            return trace_id, direct_parent, gen_id, None
        return (
            active["trace_id"],
            active["root_span_id"],
            active.get("generation_id", gen_id),
            active,
        )
    direct_parent = gen_root_span_get(gen_id)
    if direct_parent:
        return trace_id, direct_parent, gen_id, None
    return trace_id, "", gen_id, None


def _resolve_turn(
    conversation_id: str,
    gen_id: str,
    trace_id: str,
    now_ms: int,
    touch: bool = True,
):
    # Not used by stop: recording the alias first would make every stop match by construction.
    turn_key = _turn_state_key(conversation_id, gen_id)
    resolved_trace, parent, canonical_gen, active = _resolve_turn_readonly(conversation_id, gen_id, trace_id, now_ms)
    if active:
        if gen_id and not turn_matches_generation(active, gen_id):
            alias_time = now_ms if touch else int(active.get("last_activity_ms") or now_ms)
            updated = active_turn_record_alias(turn_key, gen_id, alias_time, active.get("root_span_id", ""))
            if updated:
                active = updated
                resolved_trace = active.get("trace_id", resolved_trace)
                parent = active.get("root_span_id", parent)
                canonical_gen = active.get("generation_id", canonical_gen)
        elif touch:
            active_turn_update(turn_key, {"last_activity_ms": now_ms})
    return resolved_trace, parent, canonical_gen, active


def _token_attrs(input_json: dict) -> dict:
    raw_input = input_json.get("input_tokens")
    prompt_tokens = _to_int(raw_input if raw_input is not None else input_json.get("inputTokens"))
    raw_output = input_json.get("output_tokens")
    completion_tokens = _to_int(raw_output if raw_output is not None else input_json.get("outputTokens"))
    raw_read = input_json.get("cache_read_tokens")
    cache_read = _to_int(raw_read if raw_read is not None else input_json.get("cacheReadTokens"))
    raw_write = input_json.get("cache_write_tokens")
    cache_write = _to_int(raw_write if raw_write is not None else input_json.get("cacheWriteTokens"))

    attrs = {}
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        attrs["llm.token_count.total"] = prompt_total + completion_tokens
    model = _jq_str(input_json, "model", "model_name")
    if model:
        attrs["llm.model_name"] = model
    return attrs


def _response_text(turn: dict) -> str:
    responses = turn.get("responses")
    if not isinstance(responses, list):
        return ""
    joined = "\n".join(str(item.get("text", "")) for item in responses if isinstance(item, dict))
    return truncate_attr(joined)


def _emit_closed_turn(turn: dict, end_ms: int, token_attrs: "dict | None" = None) -> None:
    trace_id = turn.get("trace_id", "")
    root_span_id = turn.get("root_span_id", "")
    conversation_id = turn.get("conversation_id", "")
    start_ms = int(turn.get("start_ms") or end_ms)
    output = _response_text(turn)
    prompt = redact_content(env.log_prompts, str(turn.get("prompt", "")))
    user_id = turn.get("user_id", "")
    login_id = turn.get("login_id", "")
    model = turn.get("model", "")
    effort = turn.get("effort", "")
    thinking = turn.get("thinking", False)

    root_attrs = {
        "openinference.span.kind": "CHAIN",
        "input.value": prompt,
        "output.value": output,
        "session.id": conversation_id,
    }
    if conversation_id:
        root_attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        root_attrs["user.id"] = user_id
    if login_id:
        root_attrs["user.login_id"] = login_id
    if effort:
        root_attrs[LLM_EFFORT_ATTR] = effort
    if thinking:
        root_attrs[LLM_THINKING_ATTR] = "true"
    if model:
        root_attrs["llm.model_name"] = model
    _send_span_async(
        build_span(
            "User Prompt",
            "CHAIN",
            root_span_id,
            trace_id,
            "",
            start_ms,
            end_ms,
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    responses = turn.get("responses")
    if not isinstance(responses, list):
        responses = []
    if responses or token_attrs or prompt:
        llm_start_ms = min(
            (int(item.get("start_ms") or start_ms) for item in responses if isinstance(item, dict)),
            default=start_ms,
        )
        final_response_ms = max(
            (int(item.get("observed_ms") or start_ms) for item in responses if isinstance(item, dict)),
            default=end_ms,
        )
        attrs = {
            "openinference.span.kind": "LLM",
            "input.value": prompt,
            "output.value": output,
            "session.id": conversation_id,
            "cursor.llm.usage.scope": "turn",
            "cursor.llm.timing.scope": "turn",
        }
        if conversation_id:
            attrs["cursor.conversation.id"] = conversation_id
        if user_id:
            attrs["user.id"] = user_id
        if login_id:
            attrs["user.login_id"] = login_id
        if effort:
            attrs[LLM_EFFORT_ATTR] = effort
        if thinking:
            attrs[LLM_THINKING_ATTR] = "true"
        if model:
            attrs["llm.model_name"] = model
        if token_attrs:
            attrs.update(token_attrs)
        _send_span_async(
            build_span(
                "Agent Response",
                "LLM",
                turn.get("llm_span_id") or span_id_16(),
                trace_id,
                root_span_id,
                llm_start_ms,
                final_response_ms,
                attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )


def _flush_active_turn(conversation_id: str, end_ms: int) -> "dict | None":
    # Marks the canonical id and every alias terminal so a late event under any of them reads as a duplicate.
    turn = active_turn_clear(conversation_id)
    if not turn:
        return None
    canonical_gen = str(turn.get("generation_id", ""))
    aliases = turn.get("generation_aliases") or []
    terminal_turn_mark_many(conversation_id, [canonical_gen, *aliases])
    _emit_closed_turn(turn, end_ms)
    if canonical_gen:
        state_cleanup_generation(canonical_gen)
    for alias in aliases:
        state_cleanup_generation(alias)
    return turn




def _dispatch(event: str, input_json: dict) -> None:
    if not check_requirements():
        return
    now_ms = get_timestamp_ms()
    conversation_id = _jq_str(input_json, "conversation_id", "conversationId")
    gen_id = _jq_str(input_json, "generation_id", "generationId")
    trace_id = _trace_id_from_event(gen_id, conversation_id)
    if not trace_id:
        trace_id = generate_trace_id()

    if event in ("beforeSubmitPrompt", "before_submit_prompt"):
        _handle_before_submit_prompt(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterAgentResponse", "after_agent_response"):
        _handle_after_agent_response(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterAgentThought", "after_agent_thought"):
        _handle_after_agent_thought(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("beforeShellExecution", "before_shell_execution"):
        _handle_before_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterShellExecution", "after_shell_execution"):
        _handle_after_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("beforeMCPExecution", "before_mcp_execution"):
        _handle_before_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterMCPExecution", "after_mcp_execution"):
        _handle_after_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("beforeReadFile", "before_read_file"):
        _handle_before_read_file(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterFileEdit", "after_file_edit"):
        _handle_after_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("beforeTabFileRead", "before_tab_file_read"):
        _handle_before_tab_file_read(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("afterTabFileEdit", "after_tab_file_edit"):
        _handle_after_tab_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event == "stop":
        _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms)
        gc_stale_state_files()
    elif event in ("sessionStart", "session_start"):
        _handle_session_start(input_json, conversation_id, gen_id, trace_id, now_ms)
    elif event in ("sessionEnd", "session_end"):
        _handle_session_end(input_json, conversation_id, gen_id, trace_id, now_ms)
        gc_stale_state_files()
    elif event in ("postToolUse", "post_tool_use"):
        _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms)
    else:
        log(f"Unknown event: {event}")




def _handle_before_submit_prompt(input_json, conversation_id, gen_id, trace_id, now_ms):
    # Without conversation_id the turn is keyed by generation, so next-prompt fallback closure cannot apply and it must close via its own stop.
    turn_key = _turn_state_key(conversation_id, gen_id)
    if conversation_id:
        _flush_active_turn(conversation_id, now_ms)
        if not gen_id:
            terminal_nogen_clear(conversation_id)

    sid = span_id_16()
    if not trace_id:
        trace_id = trace_id_from_generation(gen_id or sid)
    if gen_id:
        gen_root_span_save(gen_id, sid)

    prompt = _jq_str(input_json, "prompt", "input", "text")
    model = _jq_str(input_json, "model", "model_name")
    effort, thinking = _model_settings(input_json)
    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    turn = {
        "generation_id": gen_id,
        "trace_id": trace_id,
        "root_span_id": sid,
        "llm_span_id": span_id_16(),
        "conversation_id": conversation_id,
        "start_ms": now_ms,
        "last_activity_ms": now_ms,
        "prompt": prompt,
        "model": model,
        "user_id": user_id,
        "login_id": login_id,
        "effort": effort,
        "thinking": thinking,
        "thought_hashes": [],
        "responses": [],
        "generation_aliases": [],
    }
    if not turn_key:
        error("beforeSubmitPrompt: no conversation_id or generation_id available; closing turn immediately")
        _emit_closed_turn(turn, now_ms)
        return
    if not active_turn_save(turn_key, turn):
        error(f"beforeSubmitPrompt: active_turn_save failed for key={turn_key}; closing turn immediately")
        _emit_closed_turn(turn, now_ms)
        if gen_id:
            state_cleanup_generation(gen_id)
        terminal_turn_mark(turn_key, gen_id)
        return
    log(f"beforeSubmitPrompt: deferred root span {sid} (trace={trace_id}, key={turn_key})")


def _handle_after_agent_response(input_json, conversation_id, gen_id, trace_id, now_ms):
    resolved_trace, parent, _, turn = _resolve_turn(conversation_id, gen_id, trace_id, now_ms, touch=False)
    response = redact_content(
        env.log_prompts,
        _jq_str(input_json, "text", "response", "output"),
    )
    model = _jq_str(input_json, "model", "model_name")
    effort, thinking = _model_settings(input_json)
    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)
    if turn:
        updated = active_turn_add_response(
            _turn_state_key(conversation_id, gen_id),
            response,
            now_ms,
            model,
            user_id,
            turn.get("root_span_id", ""),
            login_id=login_id,
            effort=effort,
            thinking=thinking,
        )
        if updated:
            log("afterAgentResponse: appended response to active turn")
            return
        log("afterAgentResponse: active turn closed before response append")

    if gen_id:
        turn_key = _turn_state_key(conversation_id, gen_id)
        if terminal_turn_is_marked(turn_key, gen_id):
            log("afterAgentResponse: ignored response for a terminal generation")
            return
        state_push(
            f"orphan_llm_{sanitize(gen_id)}",
            {
                "span_id": span_id_16(),
                "trace_id": resolved_trace,
                "parent": parent,
                "output": response,
                "model": model,
                "effort": effort,
                "thinking": thinking,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "login_id": login_id,
                "observed_ms": now_ms,
            },
        )
        log("afterAgentResponse: deferred standalone response until stop")
        return

    attrs = {
        "openinference.span.kind": "LLM",
        "output.value": response,
        "session.id": conversation_id,
        "cursor.llm.usage.scope": "turn",
        "cursor.llm.timing.scope": "turn",
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if effort:
        attrs[LLM_EFFORT_ATTR] = effort
    if thinking:
        attrs[LLM_THINKING_ATTR] = "true"
    if model:
        attrs["llm.model_name"] = model
    _send_span_async(
        build_span(
            "Agent Response",
            "LLM",
            span_id_16(),
            resolved_trace,
            parent,
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )
    log("afterAgentResponse: sent standalone point span")


def _handle_after_agent_thought(input_json, conversation_id, gen_id, trace_id, now_ms):
    resolved_trace, parent, _, turn = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return
    raw_thought = _jq_str(input_json, "thought", "thinking", "text")
    thought = redact_content(env.log_prompts, raw_thought)
    normalized = raw_thought.replace("\r\n", "\n").strip()
    salt = str(turn.get("root_span_id", "")) if turn else ""
    thought_hash = hashlib.sha256(f"{salt}\0{thought}\0{normalized}".encode()).hexdigest()
    if turn and not active_turn_add_thought_hash(
        _turn_state_key(conversation_id, gen_id),
        thought_hash,
        now_ms,
        turn.get("root_span_id", ""),
    ):
        log("afterAgentThought: skipped exact duplicate")
        return
    sid = span_id_16()

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "output.value": thought,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        "Agent Thinking",
        "CHAIN",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterAgentThought: span {sid}")


def _handle_before_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 163-179."""
    resolved_trace, _, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not canonical_gen:
        return

    command = _jq_str(input_json, "command", "shell_command")
    cwd = _jq_str(input_json, "cwd", "working_directory")

    state_push(
        f"shell_{sanitize(canonical_gen)}",
        {
            "command": command,
            "cwd": cwd,
            "start_ms": str(now_ms),
            "trace_id": resolved_trace,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeShellExecution: pushed state for gen={canonical_gen}")


def _handle_after_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 184-232."""
    sid = span_id_16()
    resolved_trace, parent, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return
    popped = state_pop(f"shell_{sanitize(canonical_gen)}") if canonical_gen else None

    if popped:
        start_ms = popped.get("start_ms", "")
        start_ms = int(start_ms) if start_ms.isdigit() else now_ms
        resolved_trace = popped.get("trace_id", resolved_trace)
        conversation_id = popped.get("conversation_id", conversation_id)
        command = popped.get("command", "")
    else:
        start_ms = now_ms
        command = _jq_str(input_json, "command", "shell_command")

    after_cmd = _jq_str(input_json, "command", "shell_command")
    if after_cmd:
        command = after_cmd

    output = _jq_str(input_json, "output", "stdout", "result")
    exit_code = _jq_str(input_json, "exit_code", "exitCode", "status")

    command = redact_content(env.log_tool_details, command)
    output = redact_content(env.log_tool_content, output)

    parsed_exit_code = _to_int(exit_code)
    status_code = 0
    status_message = ""
    if parsed_exit_code == 0:
        status_code = 1
    elif parsed_exit_code is not None:
        status_code = 2
        status_message = truncate_attr(output, 1024)

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "shell",
        "input.value": command,
        "output.value": output,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if exit_code:
        attrs["shell.exit_code"] = str(exit_code)

    span = build_span(
        "Shell",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=status_code,
        status_message=status_message,
    )
    _send_span_async(span)
    log(f"afterShellExecution: span {sid} (merged)")


def _handle_before_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 237-257."""
    resolved_trace, _, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not canonical_gen:
        return

    tool_name = _jq_str(input_json, "tool_name", "toolName", "name")
    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments")
    mcp_url = _jq_str(input_json, "url")
    mcp_cmd = _jq_str(input_json, "command")

    state_push(
        f"mcp_{sanitize(canonical_gen)}",
        {
            "tool_name": tool_name,
            "tool_input": redact_content(env.log_tool_content, tool_input),
            "url": redact_content(env.log_tool_details, mcp_url),
            "command": redact_content(env.log_tool_details, mcp_cmd),
            "start_ms": str(now_ms),
            "trace_id": resolved_trace,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeMCPExecution: pushed state for gen={canonical_gen}")


def _handle_after_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 262-312."""
    sid = span_id_16()
    resolved_trace, parent, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return
    popped = state_pop(f"mcp_{sanitize(canonical_gen)}") if canonical_gen else None

    if popped:
        start_ms = popped.get("start_ms", "")
        start_ms = int(start_ms) if start_ms.isdigit() else now_ms
        resolved_trace = popped.get("trace_id", resolved_trace)
        conversation_id = popped.get("conversation_id", conversation_id)
        tool_name = popped.get("tool_name", "")
        tool_input = popped.get("tool_input", "")
        mcp_url = popped.get("url", "")
        mcp_cmd = popped.get("command", "")
    else:
        start_ms = now_ms
        tool_name = _jq_str(input_json, "tool_name", "toolName", "name")
        tool_input = redact_content(env.log_tool_content, _jq_str(input_json, "tool_input", "toolInput", "input"))
        mcp_url = redact_content(env.log_tool_details, _jq_str(input_json, "url"))
        mcp_cmd = redact_content(env.log_tool_details, _jq_str(input_json, "command"))

    after_tool = _jq_str(input_json, "tool_name", "toolName", "name")
    if after_tool:
        tool_name = after_tool
    tool_name = tool_name or "unknown"

    result = redact_content(env.log_tool_content, _jq_str(input_json, "result", "output", "response"))

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": result,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if mcp_url:
        attrs["mcp.url"] = mcp_url
    if mcp_cmd:
        attrs["mcp.command"] = mcp_cmd

    span = build_span(
        f"MCP: {tool_name}",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterMCPExecution: span {sid} (merged)")


def _handle_before_read_file(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file read. Replaces bash lines 317-339."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        "Read File",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"beforeReadFile: span {sid}")


def _handle_after_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file edit. Replaces bash lines 344-371."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if edits:
        attrs["output.value"] = edits

    span = build_span(
        "File Edit",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterFileEdit: span {sid}")


def _handle_before_tab_file_read(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file read. Replaces bash lines 376-398."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this tab read - span dropped")
        return

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file_tab",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        "Tab Read File",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"beforeTabFileRead: span {sid}")


def _handle_after_tab_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file edit. Replaces bash lines 403-430."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this tab edit - span dropped")
        return

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file_tab",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if edits:
        attrs["output.value"] = edits

    span = build_span(
        "Tab File Edit",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterTabFileEdit: span {sid}")
    log(f"afterTabFileEdit: span {sid}")


def _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    # Clears only a turn whose canonical id or previously recorded alias matches; a mismatched stop stays standalone.
    turn_key = _turn_state_key(conversation_id, gen_id)
    resolved_trace, parent, canonical_gen, active = _resolve_turn_readonly(conversation_id, gen_id, trace_id, now_ms)
    if not turn_key and not parent and not active:
        log("cursor: no root span for this generation - span dropped")
        return

    if active:
        active_has_generation = bool(active.get("generation_id") or active.get("generation_aliases"))
        if gen_id and not turn_matches_generation(active, gen_id):
            log(
                f"stop: ignored generation {gen_id!r} because it does not match "
                "the active turn's canonical generation or observed aliases"
            )
            return
        elif not gen_id and active_has_generation:
            log("stop: ignored generation-less terminal event for a generated active turn")
            return

    terminal_generations = []
    if active:
        terminal_generations = [
            active.get("generation_id", ""),
            *((active.get("generation_aliases") or [])),
            gen_id,
        ]
    elif gen_id or canonical_gen:
        terminal_generations = [gen_id, canonical_gen]

    if any(terminal_generations):
        if not terminal_turn_claim_many(turn_key, terminal_generations):
            log(f"stop: skipped duplicate terminal event for gen={gen_id}")
            return
    elif not terminal_nogen_claim(turn_key):
        log("stop: skipped duplicate no-generation terminal event")
        return

    turn = None
    if active:
        turn = active_turn_clear_if_matches(
            turn_key,
            root_span_id=active.get("root_span_id", ""),
            generation_id=active.get("generation_id", ""),
        )
        if turn is None:
            log(f"stop: active turn was closed concurrently for gen={gen_id}")
            return

    token_attrs = _token_attrs(input_json)
    has_token_counts = any(key.startswith("llm.token_count.") for key in token_attrs)
    orphan_entries = []
    if not turn and gen_id:
        key = f"orphan_llm_{sanitize(gen_id)}"
        while True:
            entry = state_pop(key)
            if entry is None:
                break
            orphan_entries.append(entry)
        orphan_entries.reverse()

    effort, thinking = _model_settings(input_json)
    login_id = _resolve_user_login_id(input_json)

    emitted_llm_span = False
    if turn:
        resolved_trace = turn.get("trace_id", resolved_trace)
        parent = turn.get("root_span_id", parent)
        if effort and not turn.get("effort"):
            turn["effort"] = effort
        if thinking and not turn.get("thinking"):
            turn["thinking"] = thinking
        _emit_closed_turn(turn, now_ms, token_attrs)
        emitted_llm_span = True
    elif orphan_entries:
        first = orphan_entries[0]
        latest = orphan_entries[-1]
        output = "\n".join(str(entry.get("output", "")) for entry in orphan_entries)
        orphan_conversation_id = latest.get("conversation_id") or first.get("conversation_id") or conversation_id
        llm_attrs = {
            "openinference.span.kind": "LLM",
            "output.value": truncate_attr(output),
            "session.id": orphan_conversation_id,
            "cursor.llm.usage.scope": "turn",
            "cursor.llm.timing.scope": "turn",
            **token_attrs,
        }
        if conversation_id:
            llm_attrs["cursor.conversation.id"] = conversation_id
        user_id = next((entry.get("user_id", "") for entry in reversed(orphan_entries) if entry.get("user_id")), "")
        if user_id:
            llm_attrs["user.id"] = user_id
        orphan_login = next((entry.get("login_id", "") for entry in reversed(orphan_entries) if entry.get("login_id")), "") or login_id
        if orphan_login:
            llm_attrs["user.login_id"] = orphan_login
        model = next((entry.get("model", "") for entry in reversed(orphan_entries) if entry.get("model")), "")
        if model and "llm.model_name" not in llm_attrs:
            llm_attrs["llm.model_name"] = model
        orphan_effort = next((entry.get("effort", "") for entry in reversed(orphan_entries) if entry.get("effort")), "") or effort
        if orphan_effort:
            llm_attrs[LLM_EFFORT_ATTR] = orphan_effort
        if any(entry.get("thinking") for entry in orphan_entries) or thinking:
            llm_attrs[LLM_THINKING_ATTR] = "true"

        _send_span_async(
            build_span(
                "Agent Response",
                "LLM",
                first.get("span_id") or span_id_16(),
                first.get("trace_id", resolved_trace),
                first.get("parent", parent),
                int(first.get("observed_ms") or now_ms),
                now_ms,
                llm_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )
        emitted_llm_span = True
    elif has_token_counts:
        llm_attrs = {
            "openinference.span.kind": "LLM",
            "session.id": conversation_id,
            "cursor.llm.usage.scope": "turn",
            "cursor.llm.timing.scope": "turn",
            **token_attrs,
        }
        if conversation_id:
            llm_attrs["cursor.conversation.id"] = conversation_id
        standalone_user_id = _resolve_user_id(input_json)
        if standalone_user_id:
            llm_attrs["user.id"] = standalone_user_id
        if login_id:
            llm_attrs["user.login_id"] = login_id
        if effort:
            llm_attrs[LLM_EFFORT_ATTR] = effort
        if thinking:
            llm_attrs[LLM_THINKING_ATTR] = "true"
        _send_span_async(
            build_span(
                "Agent Response",
                "LLM",
                span_id_16(),
                resolved_trace,
                parent,
                now_ms,
                now_ms,
                llm_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )
        emitted_llm_span = True

    sid = span_id_16()
    status = _jq_str(input_json, "status", "reason")
    loop_count = _jq_str(input_json, "loop_count", "loopCount", "iterations")
    duration_raw = input_json.get("duration_ms")
    duration_ms = _to_int(duration_raw if duration_raw is not None else input_json.get("durationMs"))
    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    user_id = _resolve_user_id(input_json)
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if status:
        attrs["cursor.stop.status"] = status
    if loop_count:
        attrs["cursor.stop.loop_count"] = loop_count
    if duration_ms is not None:
        attrs["cursor.stop.duration_ms"] = duration_ms
    if not emitted_llm_span and "llm.model_name" in token_attrs:
        attrs["llm.model_name"] = token_attrs["llm.model_name"]
    if not emitted_llm_span:
        if effort:
            attrs[LLM_EFFORT_ATTR] = effort
        if thinking:
            attrs[LLM_THINKING_ATTR] = "true"

    _send_span_async(
        build_span(
            "Agent Stop",
            "CHAIN",
            sid,
            resolved_trace,
            parent,
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    closed_gen = turn.get("generation_id", "") if turn else ""
    aliases = (turn.get("generation_aliases") if turn else None) or []
    if closed_gen:
        state_cleanup_generation(closed_gen)
    for alias in aliases:
        state_cleanup_generation(alias)
    if gen_id and gen_id != closed_gen and gen_id not in aliases:
        state_cleanup_generation(gen_id)
    if turn:
        terminal_turn_mark_many(turn_key, [closed_gen, *aliases, gen_id])
    log(f"stop: span {sid}, cleaned up gen={closed_gen or gen_id}")


def _handle_session_start(input_json, conversation_id, gen_id, trace_id, now_ms):
    # Saved as the generation root so CLI spans sharing this generation_id parent to the session instead of becoming orphan roots.
    if _is_cursor_ide_hook_payload(input_json):
        log("sessionStart: skipped standalone IDE session trace")
        return

    sid = span_id_16()

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id

    cursor_version = _jq_str(input_json, "cursor_version", "cursorVersion", "version")
    if cursor_version:
        attrs["cursor.version"] = cursor_version

    cwd = _jq_str(input_json, "cwd", "working_directory")
    if cwd:
        attrs["cursor.session.cwd"] = cwd

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        "Session Start",
        "CHAIN",
        sid,
        trace_id,
        "",
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    if gen_id:
        gen_root_span_save(gen_id, sid)
    log(f"sessionStart: span {sid} (trace={trace_id})")


def _handle_session_end(input_json, conversation_id, gen_id, trace_id, now_ms):
    turn_key = _turn_state_key(conversation_id, gen_id)
    _flush_active_turn(turn_key, now_ms)
    sid = span_id_16()
    duration_raw = input_json.get("duration_ms")
    duration_ms = _to_int(duration_raw if duration_raw is not None else input_json.get("durationMs"))
    final_status = _jq_str(input_json, "final_status", "finalStatus", "status")
    reason = _jq_str(input_json, "reason")
    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if duration_ms is not None:
        attrs["cursor.session.duration_ms"] = duration_ms
    if final_status:
        attrs["cursor.session.final_status"] = final_status
    if reason:
        attrs["cursor.session.reason"] = reason

    raw_input = input_json.get("input_tokens")
    input_tokens = _to_int(raw_input if raw_input is not None else input_json.get("inputTokens"))
    raw_output = input_json.get("output_tokens")
    output_tokens = _to_int(raw_output if raw_output is not None else input_json.get("outputTokens"))
    raw_read = input_json.get("cache_read_tokens")
    cache_read = _to_int(raw_read if raw_read is not None else input_json.get("cacheReadTokens"))
    raw_write = input_json.get("cache_write_tokens")
    cache_write = _to_int(raw_write if raw_write is not None else input_json.get("cacheWriteTokens"))
    prompt_total = None
    if input_tokens is not None:
        attrs["cursor.session.token_count.input"] = input_tokens
        prompt_total = input_tokens + (cache_read or 0) + (cache_write or 0)
        attrs["cursor.session.token_count.prompt"] = prompt_total
    if output_tokens is not None:
        attrs["cursor.session.token_count.output"] = output_tokens
    if cache_read is not None:
        attrs["cursor.session.token_count.cache_read"] = cache_read
    if cache_write is not None:
        attrs["cursor.session.token_count.cache_write"] = cache_write
    if prompt_total is not None and output_tokens is not None:
        attrs["cursor.session.token_count.total"] = prompt_total + output_tokens

    _send_span_async(
        build_span(
            "Session End",
            "CHAIN",
            sid,
            trace_id,
            "",
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )
    if gen_id:
        state_cleanup_generation(gen_id)
    conversation_cleanup(conversation_id)
    log(f"sessionEnd: span {sid}, cleaned up gen={gen_id}")


_DEDICATED_TOOL_NAMES = frozenset(
    {
        "shell",
        "terminal",
        "terminalcmd",
        "runterminalcmd",
        "bash",
        "runcommand",
        "runshell",
        "readfile",
        "read",
        "viewfile",
        "view",
        "editfile",
        "edit",
        "multiedit",
        "searchreplace",
        "applypatch",
        "strreplaceeditor",
        "writefile",
        "write",
        "createfile",
        "deletefile",
        "tabfileread",
        "tabfileedit",
        "mcp",
    }
)


def _normalize_tool_name(name: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]", "", name).lower()


def _has_dedicated_handler(tool_name: str) -> bool:
    normalized = _normalize_tool_name(tool_name)
    if not normalized:
        return False
    return normalized in _DEDICATED_TOOL_NAMES or normalized.startswith("mcp")


def _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    tool_name = _jq_str(input_json, "tool_name", "toolName", "name", "tool")
    if _has_dedicated_handler(tool_name):
        log(f"postToolUse: skipping {tool_name!r} — covered by dedicated handler")
        return

    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not parent:
        log("cursor: no root span for this generation - span dropped")
        return

    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments", "args")
    output = _jq_str(input_json, "result", "output", "response", "stdout")

    tool_input = redact_content(env.log_tool_content, tool_input)
    output = redact_content(env.log_tool_content, output)

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if tool_name:
        attrs["tool.name"] = tool_name
    if tool_input:
        attrs["input.value"] = tool_input
    if output:
        attrs["output.value"] = output

    span_name = f"Tool: {tool_name}" if tool_name else "Tool"
    span = build_span(
        span_name,
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"postToolUse: span {sid} for tool={tool_name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for atatus-hook-cursor. Cursor hook.

    Input contract: JSON on stdin, all 15 events (IDE + CLI) routed here.
    stdout: MUST print permissive JSON response, even on error.
    stderr: redirected to ATATUS_LOG_FILE at adapter import time via
        core.common.redirect_stderr_to_log_file().
    """
    event = ""
    try:
        if not check_requirements():
            return

        input_json = json.loads(read_stdin_text() or "{}")
        event = _event_name(input_json)
        _dispatch(event, input_json)
    except Exception as e:
        error(f"cursor hook failed ({event}): {e}")
    finally:
        # ALWAYS print permissive response — this is the LAST thing that happens
        _print_permissive(event)
