#!/usr/bin/env python3
"""Copilot hook handlers. One exported function per hook event.

Each entry point reads stdin JSON, resolves session state, and delegates to the
corresponding _handle_* implementation.
"""
import json

from core.common import (
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    read_stdin_text,
    redact_content,
    send_span,
    send_span_async,
)
from core.event_model import TurnEndReason
from core.turn_lifecycle import turn_end_attributes, turn_status
from tracing.copilot.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    payload_get,
    resolve_session,
)
from tracing.copilot.hooks.transcript import parse_transcript

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _send_span_async(span_dict: dict, on_success=None) -> None:
    """Detached span send. ``sender`` keeps this module's ``send_span`` binding
    on the synchronous fallback path so test doubles still intercept it."""
    send_span_async(span_dict, sender=send_span, on_success=on_success)


def _read_stdin(event: str) -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input.

    When ATATUS_TRACE_DEBUG=true, the parsed payload is written to
    ~/.atatus/harness/state/debug/copilot_<event>_<ts>.json so we can
    inspect the actual field schema Copilot is sending.
    """
    try:
        raw = read_stdin_text()
        data = json.loads(raw) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    debug_dump(f"copilot_{event}", data)
    return data


def _print_response() -> None:
    """Emit an empty JSON object — an explicit "no opinion" from this hook.

    Copilot reads a `permissionDecision` here, so anything else would let a
    tracing hook decide whether the user's tool call is allowed to run.
    """
    print("{}")


def _ensure_turn(state) -> tuple[str, str]:
    """Return (trace_id, root_span_id) for the turn in progress, opening one if needed.

    A hook installed mid-session, or a tool that runs before any prompt, arrives
    with no turn open. Opening one lazily keeps the span attached to something
    real: the alternative is an empty trace id, which the receiver rejects
    outright and which takes the whole turn's spans down with it.
    """
    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    if trace_id and span_id:
        return trace_id, span_id

    trace_id = trace_id or generate_trace_id()
    span_id = span_id or generate_span_id()
    state.set("current_trace_id", trace_id)
    state.set("current_trace_span_id", span_id)
    if state.get("current_trace_start_time") is None:
        state.set("current_trace_start_time", str(get_timestamp_ms()))
    return trace_id, span_id


def _tool_key(input_json: dict) -> str:
    """Key used to pair a PreToolUse with its PostToolUse.

    Copilot's hook payload carries no tool-call id, so the tool name is the only
    stable handle; parallel calls to the same tool share a start time.
    """
    return str(payload_get(input_json, "tool_use_id", "tool_call_id", "tool_name", default="tool"))


def _tool_attributes(input_json: dict) -> dict:
    """Extract the tool name, arguments and per-tool enrichment attributes."""
    tool_name = str(payload_get(input_json, "tool_name", default="unknown"))
    tool_input_raw = payload_get(input_json, "tool_args", "tool_input", default=None) or {}
    tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw)

    # Match case-insensitively: Copilot uses lowercase tool names (`bash`,
    # `read`) where other harnesses use TitleCase.
    tool_name_lc = tool_name.lower()
    command = file_path = url = query = ""
    description = ""

    if isinstance(tool_input_raw, dict):
        if tool_name_lc in ("bash", "shell", "run_command"):
            command = tool_input_raw.get("command", "")
            description = command[:200]
        elif tool_name_lc in ("read", "write", "edit", "glob", "str_replace_editor", "create_file"):
            file_path = (
                tool_input_raw.get("file_path") or tool_input_raw.get("path") or tool_input_raw.get("pattern", "")
            )
            description = str(file_path)[:200]
        elif tool_name_lc in ("websearch", "web_search"):
            query = tool_input_raw.get("query", "")
            description = query[:200]
        elif tool_name_lc in ("webfetch", "fetch"):
            url = tool_input_raw.get("url", "")
            description = url[:200]
        elif tool_name_lc in ("grep", "search"):
            query = tool_input_raw.get("pattern") or tool_input_raw.get("query", "")
            file_path = tool_input_raw.get("path", "")
            description = f"grep: {str(query)[:100]}"
        else:
            description = tool_input[:200]
    else:
        description = tool_input[:200]

    tool_input = redact_content(env.log_tool_content, tool_input)
    description = redact_content(env.log_tool_details, description)
    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "tool.description": description,
    }
    for key, value in (
        ("tool.command", command),
        ("tool.file_path", file_path),
        ("tool.url", url),
        ("tool.query", query),
    ):
        if value:
            attrs[key] = redact_content(env.log_tool_details, str(value))
    return {"name": tool_name, "attrs": attrs}


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _handle_session_start(input_json: dict) -> None:
    """Handle session start: initialize session."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    gc_stale_state_files()

    source = payload_get(input_json, "source", default="")
    initial_prompt = payload_get(input_json, "initial_prompt", default="")
    log(f"copilot session_start: source={source!r} prompt_len={len(str(initial_prompt))}")


def _handle_user_prompt_submitted(input_json: dict) -> None:
    """Handle user prompt submission: open a fresh trace."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    prompt = str(payload_get(input_json, "prompt", default=""))

    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    state.set("current_trace_prompt", prompt)
    state.increment("trace_count")
    state.set("tool_count", "0")

    log(f"copilot user_prompt_submitted: prompt_len={len(prompt)}")


def _handle_pre_tool_use(input_json: dict) -> None:
    """Handle pre_tool_use: record tool start time."""
    state = resolve_session(input_json)
    state.set(f"tool_{_tool_key(input_json)}_start", str(get_timestamp_ms()))


def _emit_tool_span(
    input_json: dict,
    *,
    output: str,
    status_code: int,
    status_message: str,
    result_type: str = "",
) -> None:
    """Build and send the TOOL span shared by the success and failure hooks."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id, parent_span_id = _ensure_turn(state)
    state.increment("tool_count")

    tool = _tool_attributes(input_json)
    tool_key = _tool_key(input_json)

    start_time = state.get(f"tool_{tool_key}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_key}_start")

    attrs = dict(tool["attrs"])
    attrs["session.id"] = session_id
    attrs["output.value"] = redact_content(env.log_tool_content, output)

    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    if result_type:
        attrs["tool.result_type"] = str(result_type)

    span = build_span(
        tool["name"],
        "TOOL",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code,
        status_message,
    )
    _send_span_async(span)


def _handle_post_tool_use(input_json: dict) -> None:
    """Handle post_tool_use: build and send a TOOL span."""
    tool_result = payload_get(input_json, "tool_result", default=None) or {}
    if isinstance(tool_result, dict):
        output = str(payload_get(tool_result, "text_result_for_llm", default=""))
        result_type = str(payload_get(tool_result, "result_type", default=""))
    else:
        output = str(tool_result)
        result_type = ""
    _emit_tool_span(input_json, output=output, status_code=1, status_message="", result_type=result_type)


def _handle_post_tool_use_failure(input_json: dict) -> None:
    """Handle post_tool_use_failure: send a TOOL span marked as an error.

    Without this the failed call is invisible and its PreToolUse start time is
    never cleared, so the session state grows one dead key per failed tool.
    """
    message = str(payload_get(input_json, "error", default=""))
    _emit_tool_span(input_json, output=message, status_code=2, status_message=message[:200])


def _handle_stop(input_json: dict, reason: TurnEndReason = TurnEndReason.COMPLETED) -> None:
    """Handle stop: close the turn with a CHAIN root and an LLM child.

    ``reason`` defaults to COMPLETED for a normal Stop hook; SessionEnd passes
    INTERRUPTED when it finds a turn still open at process exit.
    """
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id, root_span_id = _ensure_turn(state)
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""

    transcript_path = payload_get(input_json, "transcript_path", default="")
    summary = parse_transcript(str(transcript_path)) if transcript_path else {}

    model_name = summary.get("model_name", "")
    output_text = summary.get("output_text", "")
    output_tokens = summary.get("output_tokens", 0)
    if not user_prompt:
        user_prompt = summary.get("input_text", "")
    tool_count = state.get("tool_count") or "0"

    end_time = str(get_timestamp_ms())

    user_prompt = redact_content(env.log_prompts, user_prompt)
    output_text = redact_content(env.log_prompts, output_text)

    common = {"session.id": session_id}
    if user_id:
        common["user.id"] = user_id
    if login_id:
        common["user.login_id"] = login_id
    if model_name:
        common["llm.model_name"] = model_name

    root_attrs = dict(common)
    root_attrs.update(
        {
            "openinference.span.kind": "CHAIN",
            "input.value": user_prompt,
            "output.value": output_text,
            "metadata": json.dumps(
                {
                    "stop_reason": str(payload_get(input_json, "stop_reason", default="")),
                    "tool_count": int(tool_count or 0),
                }
            ),
            **turn_end_attributes(reason),
        }
    )
    status_code, status_message = turn_status(reason)

    # Root before child: a strict backend wants the parent to exist first.
    _send_span_async(
        build_span(
            "User Prompt",
            "CHAIN",
            root_span_id,
            trace_id,
            "",
            trace_start_time,
            end_time,
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
            status_code,
            status_message,
        )
    )

    llm_attrs = dict(common)
    llm_attrs.update(
        {
            "openinference.span.kind": "LLM",
            "input.value": user_prompt,
            "output.value": output_text,
        }
    )
    # Only completion tokens are reported per turn; the prompt-side counts are
    # session cumulative totals, and splitting them across turns would invent
    # numbers the transcript never gave us.
    if output_tokens:
        llm_attrs["llm.token_count.completion"] = output_tokens

    _send_span_async(
        build_span(
            "Agent Response",
            "LLM",
            generate_span_id(),
            trace_id,
            root_span_id,
            trace_start_time,
            end_time,
            llm_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    # Clear per-turn state so the next user prompt starts a fresh trace
    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")


def _handle_subagent_stop(input_json: dict) -> None:
    """Handle subagent_stop: build and send CHAIN span for subagent."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        # A subagent outside any turn has no root to hang from; emitting it
        # would mint a single-span trace with no prompt and no answer.
        log("copilot subagent_stop: no open turn, skipping")
        return

    agent_id = str(payload_get(input_json, "agent_id", default=""))
    agent_type = str(payload_get(input_json, "agent_type", "agent_name", default=""))
    transcript_path = payload_get(input_json, "transcript_path", default="")

    summary = parse_transcript(str(transcript_path)) if transcript_path else {}
    model_name = summary.get("model_name", "")

    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    end_time = str(get_timestamp_ms())
    start_time = state.get(f"subagent_{agent_id}_start") or end_time

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "metadata": json.dumps({"agent_type": agent_type, "agent_id": agent_id}),
    }
    response = payload_get(input_json, "response", default="")
    if response:
        attrs["output.value"] = redact_content(env.log_prompts, str(response))
    if model_name:
        attrs["llm.model_name"] = model_name
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span_name = f"Subagent: {agent_type or agent_id}" if (agent_type or agent_id) else "Subagent"

    span = build_span(
        span_name,
        "CHAIN",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_subagent_start(input_json: dict) -> None:
    """Handle subagent_start: record the subagent's start time.

    Without this, subagent_stop's ``state.get(f"subagent_{agent_id}_start")``
    always misses and falls back to its own end time, so every subagent span
    reports zero duration.
    """
    state = resolve_session(input_json)
    agent_id = str(payload_get(input_json, "agent_id", default=""))
    if not agent_id:
        return
    state.set(f"subagent_{agent_id}_start", str(get_timestamp_ms()))


def _handle_permission_request(input_json: dict) -> None:
    """Handle permission_request: send a CHAIN span for the permission event."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id, parent_span_id = _ensure_turn(state)

    tool_name = str(payload_get(input_json, "tool_name", default=""))
    tool_input_raw = payload_get(input_json, "tool_args", "tool_input", default=None) or {}
    tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw)
    permission = str(payload_get(input_json, "permission", "permission_mode", default=""))

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "permission.type": permission,
        "permission.tool": tool_name,
        "input.value": redact_content(env.log_tool_details, tool_input),
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        "Permission Request",
        "CHAIN",
        generate_span_id(),
        trace_id,
        parent_span_id,
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_session_end(input_json: dict) -> None:
    """Handle session_end: close any turn left open, then clean up state.

    Copilot's Stop hook only clears the current-turn keys — it never touches
    session-level state, so an abrupt exit (no Stop) previously left a session
    both untraced-as-interrupted and, for VS Code's string session ids, never
    garbage-collected (gc_stale_state_files only recognizes PID-based files).
    """
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    if state.get("current_trace_id"):
        _handle_stop(input_json, reason=TurnEndReason.INTERRUPTED)

    trace_count = state.get("trace_count") or "0"
    tool_count = state.get("tool_count") or "0"
    log(f"copilot session complete: {trace_count} traces, {tool_count} tools")

    if state.state_file is not None:
        state.state_file.unlink(missing_ok=True)
    if state._lock_path is not None and state._lock_path.is_dir():
        try:
            state._lock_path.rmdir()
        except OSError:
            pass

    gc_stale_state_files()


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _run(event: str, handler) -> None:
    """Read stdin, run *handler*, and always answer the agent."""
    try:
        input_json = _read_stdin(event)
        if check_requirements():
            handler(input_json)
    except Exception as e:
        error(f"copilot {event} hook failed: {e}")
    finally:
        _print_response()


def session_start():
    """Entry point for atatus-hook-copilot-session-start."""
    _run("session_start", _handle_session_start)


def user_prompt_submitted():
    """Entry point for atatus-hook-copilot-user-prompt."""
    _run("user_prompt_submitted", _handle_user_prompt_submitted)


def pre_tool_use():
    """Entry point for atatus-hook-copilot-pre-tool."""
    _run("pre_tool_use", _handle_pre_tool_use)


def post_tool_use():
    """Entry point for atatus-hook-copilot-post-tool."""
    _run("post_tool_use", _handle_post_tool_use)


def post_tool_use_failure():
    """Entry point for atatus-hook-copilot-post-tool-failure."""
    _run("post_tool_use_failure", _handle_post_tool_use_failure)


def stop():
    """Entry point for atatus-hook-copilot-stop."""
    _run("stop", _handle_stop)


def subagent_stop():
    """Entry point for atatus-hook-copilot-subagent-stop."""
    _run("subagent_stop", _handle_subagent_stop)


def subagent_start():
    """Entry point for atatus-hook-copilot-subagent-start."""
    _run("subagent_start", _handle_subagent_start)


def permission_request():
    """Entry point for atatus-hook-copilot-permission-request."""
    _run("permission_request", _handle_permission_request)


def session_end():
    """Entry point for atatus-hook-copilot-session-end."""
    _run("session_end", _handle_session_end)
