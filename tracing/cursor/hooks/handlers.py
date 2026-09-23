#!/usr/bin/env python3
"""Cursor hook handler: single entry point dispatching all 15 Cursor hook events.

Replaces tracing/cursor/hooks/hook-handler.sh (475 lines).

Input contract: JSON on stdin, all 15 events (IDE + CLI) routed here.
stdout: MUST print permissive JSON response, even on error.
stderr: redirected to ATATUS_LOG_FILE before dispatch.
"""
import json
import re
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
    check_requirements,
    gc_stale_state_files,
    gen_root_span_get,
    gen_root_span_save,
    sanitize,
    span_id_16,
    state_cleanup_generation,
    state_pop,
    state_push,
    trace_id_from_seed,
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
    """Try multiple keys in order, return first non-None/non-empty string value.

    Matches bash: echo "$INPUT" | jq -r "$1" 2>/dev/null || echo "${2:-}"
    """
    for key in keys:
        val = input_json.get(key)
        if val is not None and val != "":
            return str(val)
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

    Cursor has no per-session state for user_id, so each handler resolves it
    inline. Configured user_id wins over the implicit `user_email` payload field
    so an explicitly set user takes precedence on shared workstations.
    """
    return env.get_user_id(SERVICE_NAME) or _jq_str(input_json, "user_email")


def _resolve_user_login_id(input_json: dict) -> str:
    """Auto-detected login identity only, no manual override."""
    return _jq_str(input_json, "user_email")


def _to_int(v):
    """Coerce *v* to int if possible; return None for None, empty, or ``"--"``."""
    try:
        return int(v) if v not in (None, "", "--") else None
    except (TypeError, ValueError):
        return None


def _event_name(input_json: dict) -> str:
    """Extract event name from payload, tolerant of IDE and CLI key variants.

    Cursor IDE uses ``hook_event_name``; Cursor CLI uses ``hookEventName``.
    """
    return _jq_str(input_json, "hook_event_name", "hookEventName", "event_name", "eventName", "event")


def _is_cursor_ide_hook_payload(input_json: dict) -> bool:
    """Return True when the stdin JSON looks like Cursor IDE (vs CLI) hook payloads.

    IDE emits ``hook_event_name``; CLI emits ``hookEventName`` — same split as ``_event_name``.
    Root CHAIN timing: IDE keeps the original deferred span (sent at afterAgentResponse with
    full turn duration and output on CHAIN). CLI sends the root at beforeSubmitPrompt so
    strict OTLP backends see the parent before tool spans.

    If neither key is set, default to IDE so existing payloads without a discriminator keep
    the original semantics.
    """
    if input_json.get("hook_event_name"):
        return True
    if input_json.get("hookEventName"):
        return False
    return True


def _trace_id_from_event(gen_id: str, conversation_id: str) -> str:
    """Derive a trace ID for an event, preferring the conversation.

    Keyed on the conversation, not the generation: Cursor mints a fresh
    generation id for every retry and continuation within a single exchange, so
    hashing the generation split one conversation into several unrelated traces
    — including two traces for one prompt the user had retried once.

    Falls back to the generation id when no conversation id is present, and to
    a random id when neither is, because an empty trace id is rejected by the
    receiver and would take the rest of the batch down with it.
    """
    if conversation_id:
        return trace_id_from_seed(conversation_id)
    if gen_id:
        return trace_id_from_seed(gen_id)
    return generate_trace_id()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(event: str, input_json: dict) -> None:
    """Route event to the appropriate handler."""
    conversation_id = input_json.get("conversation_id", "")
    gen_id = input_json.get("generation_id", "")

    # Early exit: tracing disabled
    if not env.trace_enabled:
        return

    trace_id = _trace_id_from_event(gen_id, conversation_id)
    now_ms = get_timestamp_ms()

    # Cancelled turns leave state behind that nothing will ever pop. Sweeping on
    # the turn boundaries keeps it off the hot per-keystroke events.
    if event in ("sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd"):
        gc_stale_state_files()

    handlers = {
        "beforeSubmitPrompt": _handle_before_submit_prompt,
        "afterAgentResponse": _handle_after_agent_response,
        "afterAgentThought": _handle_after_agent_thought,
        "beforeShellExecution": _handle_before_shell_execution,
        "afterShellExecution": _handle_after_shell_execution,
        "beforeMCPExecution": _handle_before_mcp_execution,
        "afterMCPExecution": _handle_after_mcp_execution,
        "beforeReadFile": _handle_before_read_file,
        "afterFileEdit": _handle_after_file_edit,
        "beforeTabFileRead": _handle_before_tab_file_read,
        "afterTabFileEdit": _handle_after_tab_file_edit,
        "stop": _handle_stop,
        "sessionStart": _handle_session_start,
        "sessionEnd": _handle_session_end,
        "postToolUse": _handle_post_tool_use,
    }

    handler = handlers.get(event)
    if handler:
        handler(input_json, conversation_id, gen_id, trace_id, now_ms)
    else:
        log(f"Unknown hook event: {event}")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_before_submit_prompt(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Root CHAIN for the turn.

    * **IDE** (``hook_event_name``): deferred until afterAgentResponse — original CHAIN span
      with full duration and output on the root.
    * **CLI** (``hookEventName`` only): sent here so tool spans that fire before
      afterAgentResponse parent to an existing span on strict OTLP backends.
    """
    sid = span_id_16()
    gen_root_span_save(gen_id, sid)

    prompt = _jq_str(input_json, "prompt", "input", "text")
    model = _jq_str(input_json, "model", "model_name")
    effort, thinking = _model_settings(input_json)
    deferred_root = _is_cursor_ide_hook_payload(input_json)

    state_push(
        f"root_{sanitize(gen_id)}",
        {
            "span_id": sid,
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "start_ms": now_ms,
            "prompt": prompt,
            "model": model,
            "effort": effort,
            "thinking": thinking,
            "deferred_root": deferred_root,
        },
    )

    if deferred_root:
        log(f"beforeSubmitPrompt: deferred root span {sid} (trace={trace_id})")
        return

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    root_attrs = {
        "openinference.span.kind": "CHAIN",
        "input.value": redact_content(env.log_prompts, prompt),
        "session.id": conversation_id,
    }
    if conversation_id:
        root_attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        root_attrs["user.id"] = user_id
    if login_id:
        root_attrs["user.login_id"] = login_id
    if model:
        root_attrs["llm.model_name"] = model
    if effort:
        root_attrs[LLM_EFFORT_ATTR] = effort
    if thinking:
        root_attrs[LLM_THINKING_ATTR] = "true"

    root_span = build_span(
        "User Prompt",
        "CHAIN",
        sid,
        trace_id,
        "",
        now_ms,
        now_ms,
        root_attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(root_span)
    log(f"beforeSubmitPrompt: root span {sid} (trace={trace_id})")


def _handle_after_agent_response(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Defers LLM span until stop (so per-turn tokens land on it). IDE also sends deferred User Prompt CHAIN."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    # "text" is the documented field; fall back to "response"/"output" for compat
    response = _jq_str(input_json, "text", "response", "output")
    # "model" is a base field on all hook events
    model = _jq_str(input_json, "model", "model_name")
    effort, thinking = _model_settings(input_json)

    safe_gen = sanitize(gen_id) if gen_id else ""
    root_state = state_pop(f"root_{safe_gen}") if safe_gen else None
    prompt = root_state.get("prompt", "") if root_state else ""
    deferred_root = root_state.get("deferred_root", True) if root_state else True

    # Redact prompt and model response unless opted in via ATATUS_LOG_PROMPTS.
    prompt = redact_content(env.log_prompts, prompt)
    response = redact_content(env.log_prompts, response)

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    # The root is not sent here. It used to be, ending at this moment — but the
    # LLM span, Agent Stop and any late tool span all carry timestamps from the
    # stop hook, which is later, so every turn rendered with its children
    # spilling past the end of their parent. The stop hook closes it instead.
    if root_state and deferred_root:
        root_state["response"] = response
        root_state["model"] = model or root_state.get("model", "")
        root_state["effort"] = effort or root_state.get("effort", "")
        root_state["thinking"] = thinking or root_state.get("thinking", False)
        root_state["user_id"] = user_id
        root_state["login_id"] = login_id
        state_push(f"root_{safe_gen}", root_state)
        log(f"afterAgentResponse: root span {root_state.get('span_id')} held for stop")

    llm_entry = {
        "span_id": sid,
        "parent": parent,
        "trace_id": trace_id,
        "input": prompt,
        "output": response,
        "model": model,
        "effort": effort,
        "thinking": thinking,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "login_id": login_id,
        "start_ms": now_ms,
    }

    if gen_id:
        # Defer LLM span to stop; tokens (only available at stop) attach there.
        state_push(f"llm_{sanitize(gen_id)}", llm_entry)
        log(f"afterAgentResponse: deferred LLM span {sid}")
        return

    # Fallback: no gen_id means we have no key to stash under — send inline.
    attrs = {
        "openinference.span.kind": "LLM",
        "input.value": prompt,
        "output.value": response,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if model:
        attrs["llm.model_name"] = model
    if effort:
        attrs[LLM_EFFORT_ATTR] = effort
    if thinking:
        attrs[LLM_THINKING_ATTR] = "true"

    span = build_span(
        "Agent Response",
        "LLM",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterAgentResponse: child span {sid} (no gen_id, sent inline)")


def _handle_after_agent_thought(input_json, conversation_id, gen_id, trace_id, now_ms):
    """CHAIN span for thinking. Replaces bash lines 138-158."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
        log("cursor: no root span for this generation - span dropped")
        return

    thought = _jq_str(input_json, "thought", "thinking", "text")

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "output.value": redact_content(env.log_prompts, thought),
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
        trace_id,
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
    if not gen_id:
        return

    command = _jq_str(input_json, "command", "shell_command")
    cwd = _jq_str(input_json, "cwd", "working_directory")

    state_push(
        f"shell_{sanitize(gen_id)}",
        {
            "command": command,
            "cwd": cwd,
            "start_ms": str(now_ms),
            "trace_id": trace_id,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeShellExecution: pushed state for gen={gen_id}")


def _handle_after_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 184-232."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)
    popped = state_pop(f"shell_{sanitize(gen_id)}") if gen_id else None

    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
        log("cursor: no root span for this generation - span dropped")
        return

    if popped:
        start_ms = popped.get("start_ms", "")
        command = popped.get("command", "")
    else:
        start_ms = ""
        command = ""
    start_ms = start_ms or str(now_ms)

    # Override command from after-event if present
    after_cmd = _jq_str(input_json, "command", "shell_command")
    if after_cmd:
        command = after_cmd

    output = _jq_str(input_json, "output", "stdout", "result")
    exit_code = _jq_str(input_json, "exit_code", "exitCode")

    command = redact_content(env.log_tool_details, command)
    output = redact_content(env.log_tool_content, output)

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
        attrs["shell.exit_code"] = exit_code

    span = build_span(
        "Shell",
        "TOOL",
        sid,
        trace_id,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterShellExecution: span {sid} (merged)")


def _handle_before_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 237-257."""
    if not gen_id:
        return

    tool_name = _jq_str(input_json, "tool_name", "toolName", "name")
    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments")
    mcp_url = _jq_str(input_json, "url", "server_url", "serverUrl")
    mcp_cmd = _jq_str(input_json, "command")

    state_push(
        f"mcp_{sanitize(gen_id)}",
        {
            "tool_name": tool_name,
            "tool_input": redact_content(env.log_tool_content, tool_input),
            "url": redact_content(env.log_tool_details, mcp_url),
            "command": redact_content(env.log_tool_details, mcp_cmd),
            "start_ms": str(now_ms),
            "trace_id": trace_id,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeMCPExecution: pushed state for gen={gen_id}")


def _handle_after_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 262-312."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)
    popped = state_pop(f"mcp_{sanitize(gen_id)}") if gen_id else None
    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
        log("cursor: no root span for this generation - span dropped")
        return

    if popped:
        start_ms = popped.get("start_ms", "")
        tool_name = popped.get("tool_name", "")
        tool_input = popped.get("tool_input", "")
    else:
        start_ms = ""
        tool_name = ""
        tool_input = ""
    start_ms = start_ms or str(now_ms)

    # Override tool name from after-event if present
    after_tool = _jq_str(input_json, "tool_name", "toolName", "name")
    if after_tool:
        tool_name = after_tool
    tool_name = tool_name or "unknown"

    result = redact_content(env.log_tool_content, _jq_str(input_json, "result", "output", "result_json"))

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

    span = build_span(
        f"MCP: {tool_name}",
        "TOOL",
        sid,
        trace_id,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterMCPExecution: span {sid} (merged, tool={tool_name})")


def _handle_before_read_file(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file read. Replaces bash lines 317-339."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
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
        trace_id,
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
    parent = gen_root_span_get(gen_id)
    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
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

    span = build_span(
        "File Edit",
        "TOOL",
        sid,
        trace_id,
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
    parent = gen_root_span_get(gen_id)

    if not parent:
        # Tab is autocomplete, not agent work, so it usually has no turn to
        # attach to. This event is no longer registered at install time; the
        # guard is here for installs whose hooks.json predates that.
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
        trace_id,
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
    parent = gen_root_span_get(gen_id)

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

    span = build_span(
        "Tab File Edit",
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"afterTabFileEdit: span {sid}")


def _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Flush deferred LLM span(s) with per-turn tokens, then send Agent Stop CHAIN + cleanup."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    status = _jq_str(input_json, "status", "reason")
    loop_count = _jq_str(input_json, "loop_count", "loopCount", "iterations")

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    # Token counts from stop payload
    # Use explicit None checks — 0 is a valid token count but falsy with ``or``
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
    model = _jq_str(input_json, "model")
    effort, thinking = _model_settings(input_json)
    _dur = input_json.get("duration_ms")
    duration_ms = _to_int(_dur if _dur is not None else input_json.get("durationMs"))

    # OpenInference: ``prompt`` is the total prompt. Cursor's ``input_tokens`` is
    # the uncached remainder (mirrors Anthropic), so the cache buckets are added
    # back in to form the total; they are also reported via ``prompt_details.*``
    # subsets so a cost model prices cache reads (~0.1x) and writes (~1.25x) at
    # their own rates instead of the full input rate.
    token_attrs = {}
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        token_attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        token_attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        token_attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        token_attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        token_attrs["llm.token_count.total"] = prompt_total + completion_tokens
    if model:
        token_attrs["llm.model_name"] = model

    # Close the deferred root first: it is the parent of everything below, and
    # only now is its real end time known.
    safe_gen = sanitize(gen_id) if gen_id else ""
    root_state = state_pop(f"root_{safe_gen}") if safe_gen else None
    if root_state and root_state.get("deferred_root"):
        root_conv_id = root_state.get("conversation_id") or conversation_id
        root_attrs = {
            "openinference.span.kind": "CHAIN",
            "input.value": redact_content(env.log_prompts, root_state.get("prompt", "")),
            "output.value": root_state.get("response", ""),
            "session.id": root_conv_id,
        }
        if root_conv_id:
            root_attrs["cursor.conversation.id"] = root_conv_id
        root_user = root_state.get("user_id") or user_id
        if root_user:
            root_attrs["user.id"] = root_user
        root_login = root_state.get("login_id") or login_id
        if root_login:
            root_attrs["user.login_id"] = root_login
        root_model = root_state.get("model") or model
        if root_model:
            root_attrs["llm.model_name"] = root_model
        root_effort = root_state.get("effort") or effort
        if root_effort:
            root_attrs[LLM_EFFORT_ATTR] = root_effort
        if root_state.get("thinking") or thinking:
            root_attrs[LLM_THINKING_ATTR] = "true"

        _send_span_async(
            build_span(
                "User Prompt",
                "CHAIN",
                root_state.get("span_id", ""),
                root_state.get("trace_id", trace_id),
                "",
                root_state.get("start_ms", now_ms),
                now_ms,
                root_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )
        log(f"stop: closed root span {root_state.get('span_id')}")

    # Drain deferred LLM stack for this generation (LIFO: first pop = most recent).
    llm_entries = []
    if gen_id:
        llm_key = f"llm_{sanitize(gen_id)}"
        while True:
            entry = state_pop(llm_key)
            if entry is None:
                break
            llm_entries.append(entry)

    # Flush deferred LLM span(s) before Agent Stop so strict OTLP backends see parent first.
    for idx, entry in enumerate(llm_entries):
        entry_conv_id = entry.get("conversation_id")
        llm_attrs = {
            "openinference.span.kind": "LLM",
            "input.value": entry.get("input", ""),
            "output.value": entry.get("output", ""),
        }
        if entry_conv_id:
            llm_attrs["session.id"] = entry_conv_id
            llm_attrs["cursor.conversation.id"] = entry_conv_id
        entry_user = entry.get("user_id")
        if entry_user:
            llm_attrs["user.id"] = entry_user
        entry_login = entry.get("login_id")
        if entry_login:
            llm_attrs["user.login_id"] = entry_login
        entry_model = entry.get("model", "")
        if entry_model:
            llm_attrs["llm.model_name"] = entry_model
        entry_effort = entry.get("effort") or effort
        if entry_effort:
            llm_attrs[LLM_EFFORT_ATTR] = entry_effort
        if entry.get("thinking") or thinking:
            llm_attrs[LLM_THINKING_ATTR] = "true"
        # Tokens are cumulative per turn — attribute only to the most recent LLM span.
        if idx == 0:
            llm_attrs.update(token_attrs)

        llm_start = int(entry.get("start_ms") or now_ms)
        llm_span = build_span(
            "Agent Response",
            "LLM",
            entry.get("span_id", ""),
            entry.get("trace_id", trace_id),
            entry.get("parent", ""),
            llm_start,
            now_ms,
            llm_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        _send_span_async(llm_span)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
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
    if model and not llm_entries:
        attrs["llm.model_name"] = model
    if not llm_entries:
        if effort:
            attrs[LLM_EFFORT_ATTR] = effort
        if thinking:
            attrs[LLM_THINKING_ATTR] = "true"

    # Fallback (no afterAgentResponse, e.g. CLI): keep token attrs on Agent Stop.
    if not llm_entries:
        attrs.update(token_attrs)

    if parent or root_state:
        span = build_span(
            "Agent Stop",
            "CHAIN",
            sid,
            trace_id,
            parent,
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        _send_span_async(span)
    else:
        # No prompt ever opened this turn, so there is nothing for the marker to
        # close. Sent anyway it becomes its own trace: a row carrying token
        # counts with no prompt and no response.
        log("stop: no root span for this generation - Agent Stop dropped")

    if gen_id:
        state_cleanup_generation(gen_id)
    log(f"stop: span {sid}, cleaned up gen={gen_id}")


def _handle_session_start(input_json, conversation_id, gen_id, trace_id, now_ms):
    """CHAIN span for Cursor CLI sessionStart event."""
    sid = span_id_16()

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id

    cwd = _jq_str(input_json, "cwd", "workspace_root")
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
    """CHAIN span for Cursor CLI sessionEnd event — closes the session.

    Reuses tokens/duration from the payload when present.  Always cleans up
    the gen_id keyed root span if one was saved by sessionStart.
    """
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    _dur = input_json.get("duration_ms")
    duration_ms = _to_int(_dur if _dur is not None else input_json.get("durationMs"))
    final_status = _jq_str(input_json, "final_status", "finalStatus", "status")
    reason = _jq_str(input_json, "reason")

    user_id = _resolve_user_id(input_json)
    login_id = _resolve_user_login_id(input_json)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
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

    # Token fields on sessionEnd are session cumulative totals, and every turn
    # in the session has already reported its own share from the stop hook.
    # Under `llm.token_count.*` they would be summed a second time, roughly
    # doubling the session's cost, so they are recorded under a session
    # namespace the cost model does not read.
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
    if prompt_tokens is not None:
        attrs["cursor.session.tokens.prompt"] = prompt_tokens + (cache_read or 0) + (cache_write or 0)
    if completion_tokens is not None:
        attrs["cursor.session.tokens.completion"] = completion_tokens
    if cache_read is not None:
        attrs["cursor.session.tokens.cache_read"] = cache_read
    if cache_write is not None:
        attrs["cursor.session.tokens.cache_write"] = cache_write

    span = build_span(
        "Session End",
        "CHAIN",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    if gen_id:
        state_cleanup_generation(gen_id)
    log(f"sessionEnd: span {sid}, cleaned up gen={gen_id}")


#: Tools that already produce a span from a dedicated before*/after* pair.
#: Compared after `_normalize_tool_name`, so each entry covers every spelling
#: Cursor uses for it — `runTerminalCmd`, `run_terminal_cmd` and `runterminalcmd`
#: all normalize to the same string.
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
        "mcpexecution",
    }
)


def _normalize_tool_name(name: str) -> str:
    """Fold a tool name to letters and digits, lowercased."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _has_dedicated_handler(tool_name: str) -> bool:
    """True when a before*/after* pair already emits a span for this tool.

    Matching on the exact name missed every spelling Cursor actually sends, so
    a single shell command produced both a `Shell` span and a `Tool:
    runTerminalCmd` span on the same parent.
    """
    normalized = _normalize_tool_name(tool_name)
    if not normalized:
        return False
    # Every MCP call arrives here as mcp_<server>_<tool>, and the MCP pair has
    # already emitted it under its real name.
    return normalized in _DEDICATED_TOOL_NAMES or normalized.startswith("mcp")


def _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for Cursor CLI postToolUse event."""
    tool_name = _jq_str(input_json, "tool_name", "toolName", "name", "tool")

    # Dedup: skip tools that have dedicated before*/after* handlers
    if _has_dedicated_handler(tool_name):
        log(f"postToolUse: skipping {tool_name!r} — covered by dedicated handler")
        return

    sid = span_id_16()
    parent = gen_root_span_get(gen_id) if gen_id else ""
    if not parent:
        # No root registered for this generation, so there is no turn to hang this on.
        # Emitting it anyway makes it a root in its own trace: a Traces-page row with no
        # prompt, no response and no tokens. Same rule the Claude Code tool hooks use.
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

    span_name = f"Tool: {tool_name}" if tool_name else "Tool Use"

    span = build_span(
        span_name,
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    log(f"postToolUse: span {sid} (tool={tool_name})")


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
