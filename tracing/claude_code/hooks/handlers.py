#!/usr/bin/env python3
"""Claude Code hook handlers. One exported function per hook event.

Replaces 9 bash scripts in tracing/claude_code/hooks/. Each function is a CLI
entry point registered in pyproject.toml [project.scripts].
"""
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from core.common import (
    LLM_EFFORT_ATTR,
    StateManager,
    build_multi_span,
    build_span,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    normalize_effort,
    read_stdin_text,
    redact_content,
    send_span,
    send_span_async,
)
from core.event_model import (
    AgentEvent,
    EventStatus,
    GraphDiagnostic,
    ModelCallEvent,
    ToolEvent,
    TurnEndReason,
    TurnEvent,
)
from core.turn_lifecycle import (
    ABANDONED_OUTPUT,
    PROMPT_ID_ATTR,
    close_turn_span,
    event_status_for,
    turn_end_attributes,
    turn_status,
)
from tracing.claude_code.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    discard_agent_state,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_agent_state,
    resolve_session,
    resolve_transcript_path,
)
from tracing.claude_code.hooks.span_renderer import render_event_graph
from tracing.claude_code.hooks.tool_buffer import TRUNCATED_BODY, ToolBuffer, ToolObservation
from tracing.claude_code.hooks.transcript import parse_claude_transcript, transcript_window

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _send_span_async(span_dict: dict, on_success=None, on_success_ref=None) -> None:
    """Detached span send. ``sender`` keeps this module's ``send_span`` binding
    on the synchronous fallback path so test doubles still intercept it."""
    send_span_async(span_dict, sender=send_span, on_success=on_success, on_success_ref=on_success_ref)


def _read_stdin() -> dict:
    """Read UTF-8 JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = read_stdin_text()
        return json.loads(raw) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _has_live_transcript(input_json: dict) -> bool:
    """Return whether this hook can participate in transcript correlation."""
    transcript_path = input_json.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        try:
            return Path(transcript_path).is_file()
        except OSError:
            pass
    session_id = input_json.get("session_id") or ""
    resolved = resolve_transcript_path(input_json, session_id)
    return resolved is not None and resolved.is_file()


def _handle_session_start(input_json: dict) -> None:
    """Handle session_start: initialize session."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    log(f"Session started: {state.get('session_id')}")


def _tool_state(input_json: dict):
    """A subagent's tool hooks carry agent_id; their observations belong to that
    agent's own state file, which is what its export reads."""
    agent_id = input_json.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        return resolve_agent_state(input_json, agent_id)
    return resolve_session(input_json)


def _handle_pre_tool_use(input_json: dict) -> None:
    """Handle pre_tool_use: record tool start time and buffer the start observation."""
    state = _tool_state(input_json)
    tool_id = input_json.get("tool_use_id") or generate_trace_id()
    started_at_ms = get_timestamp_ms()
    state.set(f"tool_{tool_id}_start", str(started_at_ms))
    if _has_live_transcript(input_json) or input_json.get("agent_id"):
        ToolBuffer(state).record_start(
            tool_id,
            tool_name=input_json.get("tool_name"),
            tool_input=input_json.get("tool_input"),
            started_at_ms=started_at_ms,
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PreToolUse")},
        )


def _handle_post_tool_use(input_json: dict) -> None:
    """Handle post_tool_use: build and send a TOOL span."""
    state = _tool_state(input_json)
    session_id = state.get("session_id")
    is_subagent = bool(input_json.get("agent_id"))
    if not (_has_live_transcript(input_json) or is_subagent) and session_id is None:
        return

    if _has_live_transcript(input_json) or is_subagent:
        tool_id = input_json.get("tool_use_id") or generate_trace_id()
        state.increment("tool_count")
        buffer = ToolBuffer(state)
        if buffer.get(tool_id) is None:
            stored_start = state.get(f"tool_{tool_id}_start")
            buffer.record_start(
                tool_id,
                tool_name=input_json.get("tool_name"),
                tool_input=input_json.get("tool_input"),
                started_at_ms=int(stored_start) if stored_start and stored_start.isdigit() else None,
            )
        buffer.record_result(
            tool_id,
            status="success",
            tool_response=input_json.get("tool_response"),
            ended_at_ms=get_timestamp_ms(),
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PostToolUse")},
        )
        state.delete(f"tool_{tool_id}_start")
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    state.increment("tool_count")

    if trace_id is None:
        # A tool ran outside a turn -- between Stop and the next prompt, or in a
        # background session that never submits one. There is no trace to hang it
        # on, and the consumer rejects an entire payload over one span with an
        # empty traceId, so emit nothing. Every other hook already guards this
        # way. The tool is still counted; only the span is skipped.
        return

    # Extract tool info
    tool_name = input_json.get("tool_name", "unknown")
    tool_id = input_json.get("tool_use_id", "")
    tool_input = json.dumps(input_json.get("tool_input", {}))
    tool_response = str(input_json.get("tool_response", ""))

    # Tool-specific metadata
    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""

    if tool_name == "Bash":
        tool_command = input_json.get("tool_input", {}).get("command", "")
        tool_description = tool_command[:200]
    elif tool_name in ("Read", "Write", "Edit", "Glob"):
        tool_file_path = input_json.get("tool_input", {}).get("file_path") or input_json.get("tool_input", {}).get(
            "pattern", ""
        )
        tool_description = tool_file_path[:200]
    elif tool_name == "WebSearch":
        tool_query = input_json.get("tool_input", {}).get("query", "")
        tool_description = tool_query[:200]
    elif tool_name == "WebFetch":
        tool_url = input_json.get("tool_input", {}).get("url", "")
        tool_description = tool_url[:200]
    elif tool_name == "Grep":
        tool_query = input_json.get("tool_input", {}).get("pattern", "")
        tool_file_path = input_json.get("tool_input", {}).get("path", "")
        tool_description = f"grep: {tool_query[:100]}"
    else:
        tool_description = tool_input[:200]

    # Timing
    start_time = state.get(f"tool_{tool_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_id}_start")

    # Redaction: tool input/output may contain raw file contents or command output;
    # tool_command/file_path/url/query/description describe what was requested.
    tool_input = redact_content(env.log_tool_content, tool_input)
    tool_response = redact_content(env.log_tool_content, tool_response)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    # Build attributes
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        **({"tool.call.id": tool_id} if tool_id else {}),
        "input.value": tool_input,
        "output.value": tool_response,
        "tool.description": tool_description,
    }
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query

    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id or "",
        parent_span_id or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_post_tool_use_failure(input_json: dict) -> None:
    """Handle post_tool_use_failure: build and send a TOOL span with error attributes."""
    state = _tool_state(input_json)
    session_id = state.get("session_id")
    is_subagent = bool(input_json.get("agent_id"))
    if not (_has_live_transcript(input_json) or is_subagent) and session_id is None:
        return

    if _has_live_transcript(input_json) or is_subagent:
        tool_id = input_json.get("tool_use_id") or generate_trace_id()
        state.increment("tool_count")
        buffer = ToolBuffer(state)
        if buffer.get(tool_id) is None:
            stored_start = state.get(f"tool_{tool_id}_start")
            buffer.record_start(
                tool_id,
                tool_name=input_json.get("tool_name"),
                tool_input=input_json.get("tool_input"),
                started_at_ms=int(stored_start) if stored_start and stored_start.isdigit() else None,
            )
        buffer.record_result(
            tool_id,
            status="error",
            tool_response=input_json.get("tool_response"),
            error=input_json.get("error", ""),
            ended_at_ms=get_timestamp_ms(),
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PostToolUseFailure")},
        )
        state.delete(f"tool_{tool_id}_start")
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    state.increment("tool_count")

    if trace_id is None:
        # A tool ran outside a turn -- between Stop and the next prompt, or in a
        # background session that never submits one. There is no trace to hang it
        # on, and the consumer rejects an entire payload over one span with an
        # empty traceId, so emit nothing. Every other hook already guards this
        # way. The tool is still counted; only the span is skipped.
        return

    # Extract tool info
    tool_name = input_json.get("tool_name", "unknown")
    tool_id = input_json.get("tool_use_id") or generate_trace_id()
    tool_input = json.dumps(input_json.get("tool_input", {}))
    tool_response = str(input_json.get("tool_response", ""))
    error_text = input_json.get("error", "")

    # Tool-specific metadata
    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""

    if tool_name == "Bash":
        tool_command = input_json.get("tool_input", {}).get("command", "")
        tool_description = tool_command[:200]
    elif tool_name in ("Read", "Write", "Edit", "Glob"):
        tool_file_path = input_json.get("tool_input", {}).get("file_path") or input_json.get("tool_input", {}).get(
            "pattern", ""
        )
        tool_description = tool_file_path[:200]
    elif tool_name == "WebSearch":
        tool_query = input_json.get("tool_input", {}).get("query", "")
        tool_description = tool_query[:200]
    elif tool_name == "WebFetch":
        tool_url = input_json.get("tool_input", {}).get("url", "")
        tool_description = tool_url[:200]
    elif tool_name == "Grep":
        tool_query = input_json.get("tool_input", {}).get("pattern", "")
        tool_file_path = input_json.get("tool_input", {}).get("path", "")
        tool_description = f"grep: {tool_query[:100]}"
    else:
        tool_description = tool_input[:200]

    # Timing
    start_time = state.get(f"tool_{tool_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_id}_start")

    # Use error as output when tool_response is empty
    output_value = tool_response if tool_response else error_text

    # Redaction
    tool_input = redact_content(env.log_tool_content, tool_input)
    output_value = redact_content(env.log_tool_content, output_value)
    redacted_error = redact_content(env.log_tool_content, error_text)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    # Build attributes
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        **({"tool.call.id": tool_id} if tool_id else {}),
        "input.value": tool_input,
        "output.value": output_value,
        "tool.description": tool_description,
        "error.type": "tool_failure",
        "error.message": redacted_error,
    }
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query

    span = build_span(
        f"{tool_name} (failed)",
        "TOOL",
        generate_span_id(),
        trace_id or "",
        parent_span_id or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2,
        status_message=error_text or "tool_failure",
    )
    _send_span_async(span)


def _handle_user_prompt_expansion(input_json: dict) -> None:
    """Handle UserPromptExpansion: stash command metadata for the next Turn span to attach."""
    state = resolve_session(input_json)
    expansion_type = input_json.get("expansion_type", "")
    command_name = input_json.get("command_name", "")
    command_args = input_json.get("command_args", "")
    command_source = input_json.get("command_source", "")
    if expansion_type:
        state.set("pending_expansion_type", expansion_type)
    if command_name:
        state.set("pending_command_name", command_name)
    if command_args:
        state.set("pending_command_args", command_args)
    if command_source:
        state.set("pending_command_source", command_source)


def _handle_user_prompt_submit(input_json: dict) -> None:
    """Handle user_prompt_submit: set up a new trace (close orphaned turn first)."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    prompt_id = input_json.get("prompt_id") or ""
    prompt_id = prompt_id if isinstance(prompt_id, str) else ""

    if state.get("current_trace_id"):
        # Same prompt id means the harness is still inside this turn, whatever the hook says.
        if prompt_id and prompt_id == state.get("current_prompt_id"):
            log("Prompt resubmitted for the live turn; keeping it open")
            return
        # Stop never fires for a turn the user interrupted, so the next prompt is the
        # first chance to close it - with what it actually did, not a stub.
        _close_live_turn(state, input_json, TurnEndReason.CONTINUED)

    # A background agent finishing is delivered as a prompt the harness runs as its own
    # turn. It is not a user turn: the work it triggers belongs to the trace that launched
    # the agent, under the agent's span, so no new trace is minted for it.
    continuation = _background_agent_for_notification(state, input_json)
    if continuation is not None and input_json.get("source", "system") != "user":
        agent_id, descriptor = continuation
        parent_span_id = descriptor.get("tool_span_id")
        state.set("current_trace_id", str(descriptor["trace_id"]))
        state.set("current_trace_span_id", str(parent_span_id))
        state.set("current_trace_start_time", str(get_timestamp_ms()))
        state.set("current_trace_prompt", "")
        state.set("current_prompt_id", prompt_id) if prompt_id else state.delete("current_prompt_id")
        state.set("continuation_agent_id", agent_id)
        state.set("continuation_is_final", "false" if descriptor.get("peer_message") else "true")
        _mark_continuation(state, str(descriptor["trace_id"]), agent_id, in_flight=True)
        _record_trace_start_line(state, input_json)
        log(f"Task notification for {agent_id}; continuing trace {descriptor['trace_id']}")
        return

    # Set up new trace
    state.increment("trace_count")
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    state.delete("continuation_agent_id")
    if prompt_id:
        state.set("current_prompt_id", prompt_id)
    else:
        state.delete("current_prompt_id")
    prompt = input_json.get("prompt", "") or ""
    # Store RAW prompt in state; redact only at span build time so the redaction
    # toggle is read once-per-emit instead of being baked into the state file.
    state.set("current_trace_prompt", prompt)

    _record_trace_start_line(state, input_json)


def _record_trace_start_line(state, input_json: dict) -> None:
    transcript = input_json.get("transcript_path", "")
    if transcript and Path(transcript).is_file():
        # The byte offset lets every later read of this turn seek past the history
        # instead of streaming the whole session file to find its own start. The
        # line index is kept only as the label the parser reports in diagnostics.
        offset = Path(transcript).stat().st_size
        with open(transcript, encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
        state.set("trace_start_line", str(line_count))
        state.set("trace_start_offset", str(offset))
    else:
        state.set("trace_start_line", "0")
        state.set("trace_start_offset", "0")


def _trace_window(state) -> "tuple[int, int | None]":
    start_line = int(state.get("trace_start_line") or "0")
    raw_offset = state.get("trace_start_offset")
    return start_line, (int(raw_offset) if raw_offset and raw_offset.isdigit() else None)


def _usage_int(usage: dict, key: str) -> int:
    """Read an int token count from a usage dict, treating non-ints as 0."""
    val = usage.get(key, 0)
    return val if isinstance(val, int) else 0


@dataclass
class _TokenUsage:
    """Token counts parsed from a transcript.

    ``prompt`` is the OpenInference prompt total: it includes *all* input
    subtypes (uncached input + cache reads + cache writes), per the Atatus/
    Atatus cost model where the base "input" portion is derived as
    ``prompt - cache_read - cache_write``. ``cache_read`` and ``cache_write``
    are therefore subsets of ``prompt``, surfaced separately so the cost
    engine can price them at their own (much cheaper) rates instead of the
    full input rate. Without the breakdown, prompt-cache tokens are billed as
    full-price input, over-reporting cost ~3-4x for heavily-cached agent runs.

    ``cache_write_1h`` is in turn a subset of ``cache_write``: the portion held
    with a one-hour TTL, which is priced above the default five-minute rate.
    The five-minute portion is ``cache_write - cache_write_1h``. Leaving it at
    zero prices the whole write at the cheaper rate, which is the behaviour
    before the split was reported.
    """

    prompt: int = 0
    completion: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0

    def token_count_attrs(self) -> dict:
        """Return OpenInference token-count attributes for span emission.

        Cache detail attributes are only included when non-zero to avoid
        cluttering spans for uncached calls.
        """
        # Omit everything when nothing was scanned. Emitting zeros makes a lost
        # transcript race indistinguishable from a turn that genuinely used no
        # tokens -- both price at $0 and neither is detectable downstream.
        if not (self.prompt or self.completion):
            return {}
        attrs: dict = {
            "llm.token_count.prompt": self.prompt,
            "llm.token_count.completion": self.completion,
            "llm.token_count.total": self.prompt + self.completion,
        }
        if self.cache_read:
            attrs["llm.token_count.prompt_details.cache_read"] = self.cache_read
        if self.cache_write:
            attrs["llm.token_count.prompt_details.cache_write"] = self.cache_write
        if self.cache_write_1h:
            attrs["llm.token_count.prompt_details.cache_write_1h"] = self.cache_write_1h
        return attrs


def _wait_for_transcript_flush(transcript: Path, start_line: int, start_offset: "int | None" = None) -> bool:
    """Poll briefly for an assistant entry at/after *start_line*.

    Claude Code writes the session JSONL asynchronously, so a Stop hook can fire
    before the assistant entry lands on disk. The response text is unaffected --
    it comes from the hook payload (``last_assistant_message``) -- but the model
    name and token counts exist ONLY in the transcript. Losing this race emits a
    span with no model and zero tokens, which prices at $0 and is
    indistinguishable from a turn that genuinely used nothing.

    Bounded, because hooks block Claude Code's UI. Returns True as soon as an
    entry appears, False if the cap expires; callers emit either way so a span is
    never lost. Tune or disable with ``ATATUS_TRANSCRIPT_WAIT_MS`` (0 disables).
    """
    try:
        cap_ms = int(os.environ.get("ATATUS_TRANSCRIPT_WAIT_MS", "300"))
    except ValueError:
        cap_ms = 300
    if cap_ms <= 0:
        return True

    deadline = time.monotonic() + (cap_ms / 1000.0)
    while True:
        try:
            for _, line in transcript_window(transcript, start_line, start_offset):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            log(f"transcript not flushed after {cap_ms}ms - model/tokens omitted")
            return False
        time.sleep(0.025)


def _scan_transcript_for_usage(
    transcript: Path,
    start_line: int,
    start_offset: "int | None" = None,
) -> "tuple[str, _TokenUsage, str]":
    """Walk the transcript JSONL from *start_line* forward and return:
    (combined_text, usage, model_name)
    """
    output = ""
    usage_totals = _TokenUsage()
    model = ""
    # Claude Code writes the same assistant message to the transcript more than
    # once -- distinct entry uuids, identical message.id. Summing every entry
    # double-counts tokens (and duplicates text), inflating reported cost by a
    # clean multiple. Count each API message exactly once.
    seen_message_ids: set = set()

    for _, line in transcript_window(transcript, start_line, start_offset):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue

        message_id = msg.get("id")
        if message_id:
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)

        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(
                item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if text:
            output = f"{output}\n{text}" if output else text

        model = msg.get("model", "") or model

        # Anthropic reports input_tokens (uncached), cache_read_input_tokens,
        # and cache_creation_input_tokens as disjoint buckets. The prompt
        # total is their sum (the OpenInference total), while the two cache
        # buckets are tracked separately and surfaced as prompt_details so
        # downstream cost pricing can apply the cheaper cache-read /
        # cache-write rates instead of the full input rate.
        usage = msg.get("usage", {})
        uncached = _usage_int(usage, "input_tokens")
        cache_read = _usage_int(usage, "cache_read_input_tokens")
        cache_write = _usage_int(usage, "cache_creation_input_tokens")

        # ``cache_creation`` breaks the write down by entry lifetime. The
        # one-hour tier is priced above the five-minute default, so the
        # split has to travel with the count -- without it every write is
        # billed at the cheaper rate.
        cache_creation = usage.get("cache_creation") or {}
        cache_write_1h = _usage_int(cache_creation, "ephemeral_1h_input_tokens")

        usage_totals.prompt += uncached + cache_read + cache_write
        usage_totals.cache_read += cache_read
        usage_totals.cache_write += cache_write
        usage_totals.cache_write_1h += cache_write_1h
        usage_totals.completion += _usage_int(usage, "output_tokens")

    return output, usage_totals, model


def _payload_effort(input_json: dict) -> str:
    """Return the effort level from a hook payload.

    Only tool-context hooks carry it (Stop, SubagentStop, Pre/PostToolUse); the
    session-lifecycle ones never do. It is recomputed from live harness state,
    so it is the fallback for what the transcript recorded on the wire.
    """
    effort = input_json.get("effort")
    return normalize_effort(effort.get("level")) if isinstance(effort, dict) else ""


def _graph_effort(graph) -> str:
    """Return the effort of the turn's last model call."""
    if graph is None:
        return ""
    for event in reversed(graph.events):
        if isinstance(event, ModelCallEvent) and event.effort:
            return event.effort
    return ""


def _has_stable_model_ids(graph) -> bool:
    """Gate high-fidelity rendering on Claude v2 assistant UUIDs."""
    models = [event for event in graph.events if isinstance(event, ModelCallEvent)]
    return bool(models) and all(not event.event_id.startswith("assistant-line-") for event in models)


def _merge_tool_observations(graph, observations) -> list:
    """Overlay each hook observation onto the first correlated tool event."""
    by_call_id = {observation.tool_use_id: observation for observation in observations}
    matched = []
    seen_call_ids: set[str] = set()
    for event in graph.events:
        if not isinstance(event, ToolEvent) or not event.tool_call_id or event.tool_call_id in seen_call_ids:
            continue
        seen_call_ids.add(event.tool_call_id)
        observation = by_call_id.get(event.tool_call_id)
        if observation is None:
            continue
        matched.append(observation)
        if observation.tool_name:
            event.tool_name = observation.tool_name
        if observation.tool_input is not None and observation.tool_input != TRUNCATED_BODY:
            event.input = observation.tool_input
        if observation.tool_response is not None and observation.tool_response != TRUNCATED_BODY:
            if isinstance(event.output, dict) and isinstance(event.output.get("toolUseResult"), dict):
                event.output = {
                    "content": observation.tool_response,
                    "toolUseResult": event.output["toolUseResult"],
                }
            else:
                event.output = observation.tool_response
        _overlay_timestamp(graph, event, "started_at_ms", observation.started_at_ms)
        _overlay_timestamp(graph, event, "ended_at_ms", observation.ended_at_ms)
        if observation.status == "error":
            event.status = EventStatus.FAILED
            response = observation.tool_response if observation.tool_response != TRUNCATED_BODY else None
            event.error = str(observation.error or response or "Tool call failed")
        elif observation.status == "success":
            event.status = EventStatus.COMPLETED
    return matched


def _overlay_timestamp(graph, event, attribute: str, value: object) -> None:
    """Apply one persisted timestamp without letting corrupt state break export."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        graph.diagnostics.append(
            GraphDiagnostic(
                code="invalid_timestamp",
                message=f"tool observation {attribute} is invalid",
                event_id=event.event_id,
                severity="warning",
            )
        )
        return
    setattr(event, attribute, int(value))


def _decode_pending_subagents(raw: object) -> dict[str, dict]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(agent_id): descriptor for agent_id, descriptor in decoded.items() if isinstance(descriptor, dict)}


def _pending_subagents(state) -> dict[str, dict]:
    return _decode_pending_subagents(state.get("pending_subagents") or "")


def _transcript_has_stable_assistant_uuid(path_value: object) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    try:
        path = Path(path_value)
        if not path.is_file():
            return False
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if record.get("type") == "assistant" and isinstance(record.get("uuid"), str) and record["uuid"]:
                    return True
    except OSError:
        return False
    return False


def _buffer_subagent(state, input_json: dict, ended_at_ms: int) -> bool:
    agent_id = input_json.get("agent_id")
    transcript_path = input_json.get("agent_transcript_path")
    main_transcript_path = input_json.get("transcript_path")
    if main_transcript_path == transcript_path or not _transcript_has_stable_assistant_uuid(main_transcript_path):
        return False
    if not isinstance(agent_id, str) or not agent_id:
        return False
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        if not Path(transcript_path).is_file():
            return False
    except OSError:
        return False

    descriptor = {
        "agent_id": agent_id,
        "agent_type": input_json.get("agent_type") or "unknown",
        "transcript_path": transcript_path,
        "agent_state_file": str(resolve_agent_state(input_json, agent_id).state_file),
        "started_at_ms": None,
        "ended_at_ms": ended_at_ms,
        "output": input_json.get("last_assistant_message") or "",
    }
    if state.state_file is None:
        return False
    try:
        with state._lock():
            data = state._read_safe()
            descriptors = _decode_pending_subagents(data.get("pending_subagents", ""))
            stored_start = str(data.get(f"subagent_{agent_id}_start_time", ""))
            descriptor["started_at_ms"] = int(stored_start) if stored_start.isdigit() else None
            descriptors[agent_id] = descriptor
            data["pending_subagents"] = json.dumps(descriptors, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to buffer subagent {agent_id}: {exc}")
        return False
    return True


def _agent_id_from_tool(event: ToolEvent) -> str:
    if not isinstance(event.output, dict):
        return ""
    result = event.output.get("toolUseResult")
    if not isinstance(result, dict):
        return ""
    agent_id = result.get("agentId")
    return agent_id if isinstance(agent_id, str) else ""


def _overlay_agent_observations(subgraph, descriptor: dict) -> None:
    """Apply the timing/status the subagent's own tool hooks recorded, then drop its
    state file: the agent is finished and this export is the only reader."""
    agent_state_file = descriptor.get("agent_state_file")
    if not isinstance(agent_state_file, str) or not agent_state_file:
        return
    path = Path(agent_state_file)
    if not path.is_file():
        return
    agent_state = StateManager(path.parent, path, path.parent / f".lock_{path.stem.replace('state_', '', 1)}")
    try:
        _merge_tool_observations(subgraph, ToolBuffer(agent_state).all())
    finally:
        discard_agent_state(agent_state)


def _merge_pending_subagents(graph, descriptors: dict[str, dict]) -> dict[str, dict]:
    """Insert correlated foreground agent graphs and return exported descriptors."""
    matched: dict[str, dict] = {}
    if not descriptors or not graph.events:
        return matched
    root = graph.events[0]
    for agent_id, descriptor in descriptors.items():
        parent_tool = next(
            (
                event
                for event in graph.events
                if isinstance(event, ToolEvent) and _agent_id_from_tool(event) == agent_id
            ),
            None,
        )
        if parent_tool is None:
            graph.diagnostics.append(
                GraphDiagnostic(
                    code="unmatched_subagent",
                    message="Subagent descriptor had no correlated Agent tool",
                    event_id=f"agent:{agent_id}",
                )
            )
            continue
        parent_event_id = parent_tool.event_id
        agent_type = str(descriptor.get("agent_type") or "unknown")
        agent_input = None
        if parent_tool is not None and isinstance(parent_tool.input, dict):
            agent_input = parent_tool.input.get("prompt")
        started_at_ms = descriptor.get("started_at_ms")
        # SubagentStart fires when the process spawns, before the Agent tool_use record
        # lands; the span cannot begin before the call that made it. With no recorded
        # start at all, the call is the best evidence - never the turn's start.
        tool_start = parent_tool.started_at_ms if isinstance(parent_tool.started_at_ms, int) else None
        if isinstance(started_at_ms, int) and tool_start is not None:
            started_at_ms = max(started_at_ms, tool_start)
        elif not isinstance(started_at_ms, int):
            started_at_ms = tool_start
        agent_event = AgentEvent(
            event_id=f"agent:{agent_id}",
            parent_event_id=parent_event_id,
            session_id=root.session_id,
            turn_id=root.turn_id,
            sequence=(parent_tool.sequence + 1) if parent_tool is not None else len(graph.events),
            started_at_ms=started_at_ms,
            ended_at_ms=descriptor.get("ended_at_ms"),
            status=EventStatus.COMPLETED,
            input=agent_input,
            output=descriptor.get("output"),
            agent_id=agent_id,
            source_id=agent_type,
        )
        transcript_path = Path(str(descriptor.get("transcript_path") or ""))
        subgraph = parse_claude_transcript(transcript_path, agent_event)
        _overlay_agent_observations(subgraph, descriptor)
        insertion_index = graph.events.index(parent_tool) + 1 if parent_tool is not None else len(graph.events)
        graph.events[insertion_index:insertion_index] = subgraph.events
        graph.diagnostics.extend(subgraph.diagnostics)
        matched[agent_id] = descriptor
    graph.validate()
    return matched


def _cleanup_pending_subagents(state, exported: dict[str, dict]) -> set[str]:
    """Acknowledge only descriptors unchanged since the export snapshot."""
    if state.state_file is None:
        return set()
    removed: set[str] = set()
    try:
        with state._lock():
            data = state._read_safe()
            current = _decode_pending_subagents(data.get("pending_subagents", ""))
            for agent_id, descriptor in exported.items():
                if current.get(agent_id) != descriptor:
                    continue
                current.pop(agent_id, None)
                data.pop(f"subagent_{agent_id}_start_time", None)
                data.pop(f"subagent_{agent_id}_prompt", None)
                removed.add(agent_id)
            if current:
                data["pending_subagents"] = json.dumps(current, sort_keys=True)
            else:
                data.pop("pending_subagents", None)
            state._write(data)
    except Exception as exc:
        error(f"Failed to clean up exported subagents: {exc}")
    return removed


def _acknowledge_exported_turn(
    state,
    expected_trace_id: str,
    observations: list[ToolObservation],
    subagents: dict[str, dict],
) -> bool:
    """Atomically acknowledge one exported turn without touching a newer turn."""
    if state.state_file is None:
        return False
    try:
        with state._lock():
            data = state._read_safe()
            if data.get("current_trace_id") != expected_trace_id:
                return False

            current_observations = ToolBuffer._decode(data.get(ToolBuffer.STATE_KEY, ""))
            for observation in observations:
                if current_observations.get(observation.tool_use_id) == observation:
                    current_observations.pop(observation.tool_use_id, None)
            # An observation the turn's transcript never matched will not match a later
            # turn's either; left in place it is re-read and rewritten by every hook.
            turn_started = int(data.get("current_trace_start_time") or 0)
            for tool_use_id, observation in list(current_observations.items()):
                ended = observation.ended_at_ms or observation.started_at_ms or 0
                if ended and turn_started and ended < turn_started:
                    current_observations.pop(tool_use_id, None)
            data[ToolBuffer.STATE_KEY] = ToolBuffer._encode(current_observations)

            current_subagents = _decode_pending_subagents(data.get("pending_subagents", ""))
            for agent_id, descriptor in subagents.items():
                if current_subagents.get(agent_id) != descriptor:
                    continue
                current_subagents.pop(agent_id, None)
                data.pop(f"subagent_{agent_id}_start_time", None)
                data.pop(f"subagent_{agent_id}_prompt", None)
            if current_subagents:
                data["pending_subagents"] = json.dumps(current_subagents, sort_keys=True)
            else:
                data.pop("pending_subagents", None)

            for key in (
                "current_trace_id",
                "current_trace_span_id",
                "current_trace_start_time",
                "current_trace_prompt",
                "current_prompt_id",
                "continuation_agent_id",
                "continuation_is_final",
                "export_attempted_trace_id",
                "trace_start_line",
                "trace_start_offset",
                "pending_expansion_type",
                "pending_command_name",
                "pending_command_args",
                "pending_command_source",
                "high_fidelity_span_ids",
            ):
                data.pop(key, None)
            state._write(data)
        return True
    except Exception as exc:
        error(f"Failed to acknowledge exported turn: {exc}")
        return False


BACKGROUND_AGENTS_KEY = "background_agents"
_NOTIFICATION_TOOL_USE = re.compile(r"<tool-use-id>\s*([A-Za-z0-9_]+)\s*</tool-use-id>")
_AGENT_MESSAGE_FROM = re.compile(r'<agent-message\s+from="([A-Za-z0-9_-]+)"')


def _background_agents(state) -> dict[str, dict]:
    return _decode_pending_subagents(state.get(BACKGROUND_AGENTS_KEY) or "")


def _register_background_agents(
    state, graph, trace_id: str, span_ids: dict[str, str], already_spliced: "set[str] | None" = None
) -> dict[str, dict]:
    """Remember every Agent tool the turn launched in the background and that is still
    running at export, keyed by agent id and by tool_use_id, so a SubagentStop or a task
    notification that lands after this turn has closed can still find its way back into
    this trace. An agent whose SubagentStop already arrived inside the turn is in the
    graph and needs nothing held for it."""
    launched: dict[str, dict] = {}
    spliced = already_spliced or set()
    for event in graph.events:
        if not isinstance(event, ToolEvent) or not isinstance(event.output, dict):
            continue
        result = event.output.get("toolUseResult")
        if not isinstance(result, dict) or result.get("status") != "async_launched":
            continue
        agent_id = result.get("agentId")
        tool_span_id = span_ids.get(event.event_id)
        if not isinstance(agent_id, str) or not agent_id or not tool_span_id:
            continue
        launched[agent_id] = {
            "trace_id": trace_id,
            "tool_span_id": tool_span_id,
            **({"subagent_span_id": span_ids.get(f"agent:{agent_id}", "")} if agent_id in spliced else {}),
            "tool_use_id": event.tool_call_id or "",
            "agent_type": (event.input or {}).get("subagent_type") if isinstance(event.input, dict) else None,
            "prompt": (event.input or {}).get("prompt") if isinstance(event.input, dict) else None,
            "turn_id": event.turn_id,
            "launched_at_ms": event.ended_at_ms or event.started_at_ms,
        }
    if not launched or state.state_file is None:
        return {}
    try:
        with state._lock():
            data = state._read_safe()
            current = _decode_pending_subagents(data.get(BACKGROUND_AGENTS_KEY, ""))
            current.update(launched)
            data[BACKGROUND_AGENTS_KEY] = json.dumps(current, sort_keys=True)
            held = _decode_pending_subagents(data.get(HELD_SPANS_KEY, ""))
            entry = held.get(trace_id) or {"spans": [], "pending": []}
            # Every launch is awaited: the agent's completion comes back as a notification
            # whose reaction is the parent's own work, after this Stop - even for an agent
            # whose SubagentStop already landed inside the turn.
            entry["pending"] = sorted(set(entry.get("pending", [])) | set(launched))
            held[trace_id] = entry
            data[HELD_SPANS_KEY] = json.dumps(held, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to register background agents: {exc}")
        return {}
    return launched


HELD_SPANS_KEY = "held_spans"


def _held_spans(state) -> dict[str, dict]:
    return _decode_pending_subagents(state.get(HELD_SPANS_KEY) or "")


def _hold_ancestor_spans(state, payload: dict, trace_id: str, agent_tool_span_ids: set[str]) -> dict:
    """Split a rendered turn: the spans that enclose a background launch - the Agent tool,
    its model call, the root - are held back so their end can grow to cover the agent's
    real lifetime; everything else ships now. Returns the payload to send."""
    try:
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    except (KeyError, IndexError, TypeError):
        return payload
    by_id = {span["spanId"]: span for span in spans}
    held_ids: set[str] = set()
    for span_id in agent_tool_span_ids:
        current = span_id
        while current and current in by_id and current not in held_ids:
            held_ids.add(current)
            current = by_id[current].get("parentSpanId", "")
    if not held_ids or state.state_file is None:
        return payload
    held = [span for span in spans if span["spanId"] in held_ids]
    remaining = [span for span in spans if span["spanId"] not in held_ids]
    try:
        with state._lock():
            data = state._read_safe()
            current_held = _decode_pending_subagents(data.get(HELD_SPANS_KEY, ""))
            entry = current_held.get(trace_id) or {"spans": [], "pending": []}
            entry["spans"] = [*entry.get("spans", []), *held]
            data[HELD_SPANS_KEY] = json.dumps(current_held | {trace_id: entry}, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to hold spans for trace {trace_id}: {exc}")
        return payload
    if not remaining:
        return {}
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"] = remaining
    return payload


def _extend_held_spans(state, trace_id: str, end_ms: int, agent_id: str = "", release: bool = False) -> None:
    """Grow every held span of the trace to at least ``end_ms``. With ``release`` (or when
    ``agent_id`` was the last pending one) the held spans are sent."""
    if state.state_file is None:
        return
    to_send: list[dict] = []
    try:
        with state._lock():
            data = state._read_safe()
            current_held = _decode_pending_subagents(data.get(HELD_SPANS_KEY, ""))
            entry = current_held.get(trace_id)
            if not entry:
                return
            end_nano = f"{end_ms}000000"
            for span in entry.get("spans", []):
                if int(span.get("endTimeUnixNano", "0")) < int(end_nano):
                    span["endTimeUnixNano"] = end_nano
            pending = [a for a in entry.get("pending", []) if a != agent_id]
            entry["pending"] = pending
            if release or (not pending and not entry.get("in_flight")):
                to_send = entry.get("spans", [])
                current_held.pop(trace_id, None)
            else:
                current_held[trace_id] = entry
            data[HELD_SPANS_KEY] = json.dumps(current_held, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to extend held spans for trace {trace_id}: {exc}")
        return
    if to_send:
        _send_span_async(build_multi_span([{"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]} for span in to_send], SERVICE_NAME, SCOPE_NAME))
        log(f"Released {len(to_send)} held span(s) for trace {trace_id}")


def _mark_continuation(state, trace_id: str, agent_id: str, in_flight: bool) -> None:
    """A notification's reaction is still being rendered: the held ancestors must wait
    for it even when every agent has already reported."""
    if state.state_file is None:
        return
    try:
        with state._lock():
            data = state._read_safe()
            current_held = _decode_pending_subagents(data.get(HELD_SPANS_KEY, ""))
            entry = current_held.get(trace_id)
            if not entry:
                return
            flights = set(entry.get("in_flight", []))
            flights.add(agent_id) if in_flight else flights.discard(agent_id)
            entry["in_flight"] = sorted(flights)
            data[HELD_SPANS_KEY] = json.dumps(current_held, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to mark continuation for trace {trace_id}: {exc}")


def _release_all_held_spans(state) -> None:
    for trace_id in list(_held_spans(state)):
        _extend_held_spans(state, trace_id, get_timestamp_ms(), release=True)


def _update_background_agent(state, agent_id: str, **fields) -> None:
    if state.state_file is None:
        return
    try:
        with state._lock():
            data = state._read_safe()
            current = _decode_pending_subagents(data.get(BACKGROUND_AGENTS_KEY, ""))
            if agent_id not in current:
                return
            current[agent_id].update(fields)
            data[BACKGROUND_AGENTS_KEY] = json.dumps(current, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to update background agent {agent_id}: {exc}")


def _background_agent_for_notification(state, input_json: dict) -> "tuple[str, dict] | None":
    """Resolve a machine-injected prompt to the background agent it comes from: a task
    notification names the Agent tool call, a peer message from a still-running agent
    names the agent itself."""
    prompt = input_json.get("prompt") or ""
    if not isinstance(prompt, str):
        return None
    agents = _background_agents(state)
    match = _NOTIFICATION_TOOL_USE.search(prompt)
    if match is not None:
        tool_use_id = match.group(1)
        for agent_id, descriptor in agents.items():
            if descriptor.get("tool_use_id") == tool_use_id:
                return agent_id, descriptor
    match = _AGENT_MESSAGE_FROM.search(prompt)
    if match is not None and match.group(1) in agents:
        return match.group(1), {**agents[match.group(1)], "peer_message": True}
    return None


def _periodic_gc(trace_count: str) -> None:
    try:
        count = int(trace_count or "0")
    except (ValueError, TypeError):
        count = 0
    if count % 5 == 0:
        gc_stale_state_files()


@dataclass
class _TurnExport:
    payload: dict
    reason: TurnEndReason
    observations: list
    subagents: dict


def _export_turn(state, input_json: dict, reason: TurnEndReason) -> "_TurnExport | None":
    """Render the live turn from its transcript window. Returns None when there is no
    transcript to replay; the caller decides how to send and when to clear state."""
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return None

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    prompt_id = state.get("current_prompt_id") or ""

    # Stop vouches for the turn by itself; any other closer needs the transcript as evidence.
    transcript = resolve_transcript_path(input_json, session_id)
    if transcript is None and reason is not TurnEndReason.COMPLETED:
        return None

    # Claude Code v2 ships the assistant's final text directly.  Earlier versions
    # didn't, so we still scan the transcript when last_assistant_message is empty.
    output = input_json.get("last_assistant_message", "") or ""
    start_line, start_offset = _trace_window(state)
    usage = _TokenUsage()
    model = ""
    if transcript is not None:
        # The flush race only exists on Stop; by the time a later hook closes the turn,
        # everything it wrote is already on disk.
        if reason is TurnEndReason.COMPLETED:
            _wait_for_transcript_flush(transcript, start_line, start_offset)
        scanned_output, usage, model = _scan_transcript_for_usage(transcript, start_line, start_offset)
        if not output:
            output = scanned_output
    if not output:
        output = "(No response)"

    root_event = TurnEvent(
        event_id=f"turn:{trace_count}",
        session_id=session_id,
        turn_id=trace_count,
        sequence=0,
        started_at_ms=int(trace_start_time) if str(trace_start_time).isdigit() else None,
        ended_at_ms=get_timestamp_ms(),
        status=event_status_for(reason),
        input=user_prompt,
        output=output,
        end_reason=reason,
    )
    graph = None
    if transcript is not None:
        graph = parse_claude_transcript(transcript, root_event, start_line=start_line, start_offset=start_offset)
        # The transcript can prove an interrupt the hook payload cannot.
        reason = root_event.end_reason or reason
        root_event.status = event_status_for(reason)

    root_attrs: dict = {"trace.number": trace_count}
    if prompt_id:
        root_attrs[PROMPT_ID_ATTR] = prompt_id
    expansion_type = state.get("pending_expansion_type") or ""
    command_name = state.get("pending_command_name") or ""
    command_args = state.get("pending_command_args") or ""
    command_source = state.get("pending_command_source") or ""
    if expansion_type:
        root_attrs["command.expansion_type"] = expansion_type
    if command_name:
        root_attrs["command.name"] = command_name
    if command_args:
        root_attrs["command.args"] = redact_content(env.log_prompts, command_args)
    if command_source:
        root_attrs["command.source"] = command_source
    turn_effort = _graph_effort(graph) or _payload_effort(input_json)
    if turn_effort:
        root_attrs[LLM_EFFORT_ATTR] = turn_effort

    if graph is not None and _has_stable_model_ids(graph):
        buffer = ToolBuffer(state)
        observations = buffer.all()
        pending_subagents = _pending_subagents(state)
        matched_observations = _merge_tool_observations(graph, observations)
        matched_subagents = _merge_pending_subagents(graph, pending_subagents)
        graph.validate()

        # The turn ends when its last child does. Taken from the graph because Stop
        # fires before the final tool result is always flushed.
        timestamp_candidates = [
            int(event.ended_at_ms)
            for event in graph.events
            if isinstance(event.ended_at_ms, (int, float))
            and not isinstance(event.ended_at_ms, bool)
            and math.isfinite(event.ended_at_ms)
            and event.ended_at_ms >= 0
        ]
        if timestamp_candidates:
            root_event.ended_at_ms = max(root_event.ended_at_ms or 0, *timestamp_candidates)

        # Reused verbatim on a retry so a failed export cannot produce a second copy
        # of the same turn under fresh span IDs.
        span_id_overrides = {root_event.event_id: trace_span_id}
        stored_span_ids = state.get("high_fidelity_span_ids") or ""
        if stored_span_ids:
            try:
                decoded_span_ids = json.loads(stored_span_ids)
                if isinstance(decoded_span_ids, dict):
                    span_id_overrides.update(
                        {
                            str(event_id): str(span_id)
                            for event_id, span_id in decoded_span_ids.items()
                            if isinstance(event_id, str) and isinstance(span_id, str) and span_id
                        }
                    )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                log(f"invalid high_fidelity_span_ids state; regenerating span IDs: {exc}")
        for event in graph.events:
            span_id_overrides.setdefault(event.event_id, generate_span_id())
        state.set("high_fidelity_span_ids", json.dumps(span_id_overrides, sort_keys=True))

        common_attrs = {
            **({"user.id": user_id} if user_id else {}),
            **({"user.login_id": login_id} if login_id else {}),
        }
        # A task-notification continuation has no root of its own: its spans go straight
        # under the subagent (or Agent tool) span already sent in the originating trace.
        continuation = bool(state.get("continuation_agent_id"))
        payload = render_event_graph(
            graph,
            trace_id=trace_id,
            service_name=SERVICE_NAME,
            scope_name=SCOPE_NAME,
            span_id_overrides=span_id_overrides,
            extra_attributes={root_event.event_id: root_attrs},
            common_attributes=common_attrs,
            root_parent_span_id=trace_span_id if continuation else "",
            skip_root=continuation,
        )
        launched = _register_background_agents(
            state, graph, trace_id, span_id_overrides, already_spliced=set(matched_subagents)
        )
        if launched:
            payload = _hold_ancestor_spans(state, payload, trace_id, {d["tool_span_id"] for d in launched.values()})
        if continuation:
            agent_id = str(state.get("continuation_agent_id"))
            _mark_continuation(state, trace_id, agent_id, in_flight=False)
            settled = agent_id if state.get("continuation_is_final") != "false" else ""
            _extend_held_spans(state, trace_id, int(root_event.ended_at_ms or get_timestamp_ms()), agent_id=settled)
        return _TurnExport(payload, reason, matched_observations, matched_subagents)

    if state.get("continuation_agent_id"):
        # No model calls to attach and no root to send; the notification itself is not a turn.
        agent_id = str(state.get("continuation_agent_id"))
        _mark_continuation(state, trace_id, agent_id, in_flight=False)
        settled = agent_id if state.get("continuation_is_final") != "false" else ""
        _extend_held_spans(state, trace_id, get_timestamp_ms(), agent_id=settled)
        return _TurnExport({}, reason, [], {})

    # Legacy fallback: a transcript with no stable assistant UUIDs cannot be resolved into
    # model calls, so the turn stays one flat LLM span.
    # Redact at emit time, not at state-write time.
    redacted_prompt = redact_content(env.log_prompts, user_prompt)
    redacted_output = redact_content(env.log_prompts, output)
    output_messages = [{"message.role": "assistant", "message.content": redacted_output}]
    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "LLM",
        **({"llm.model_name": model} if model else {}),
        **usage.token_count_attrs(),
        "input.value": redacted_prompt,
        "output.value": redacted_output,
        "llm.output_messages": json.dumps(output_messages),
        **root_attrs,
        **turn_end_attributes(reason),
    }
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    status_code, status_message = turn_status(reason)
    span = build_span(
        f"Turn {trace_count}",
        "LLM",
        trace_span_id,
        trace_id,
        "",
        trace_start_time,
        str(root_event.ended_at_ms or get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=status_code,
        status_message=status_message,
    )
    return _TurnExport(span, reason, [], {})


def _emit_abandoned_turn(state) -> None:
    """Last resort when there is no transcript to replay: the root is a stub, and the
    only honest status is an error — nothing proves the turn ran or what it did."""
    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    if not trace_id or not span_id:
        return
    trace_count = state.get("trace_count") or "?"
    attrs = {
        "session.id": state.get("session_id"),
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "LLM",
        "input.value": redact_content(env.log_prompts, state.get("current_trace_prompt") or ""),
        "output.value": ABANDONED_OUTPUT,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    prompt_id = state.get("current_prompt_id") or ""
    if prompt_id:
        attrs[PROMPT_ID_ATTR] = prompt_id
    span = close_turn_span(
        f"Turn {trace_count}",
        "LLM",
        span_id,
        trace_id,
        state.get("current_trace_start_time") or str(get_timestamp_ms()),
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        TurnEndReason.ABANDONED,
    )
    _send_span_async(span)


def _close_live_turn(state, input_json: dict, reason: TurnEndReason) -> None:
    """Close the turn still open in state because its own end-of-turn hook never fired."""
    trace_id = state.get("current_trace_id")
    if not trace_id:
        return
    trace_count = state.get("trace_count") or "?"

    # The root is already on the wire (or refused). Re-sending it under the same ids
    # would put a second, contradictory row on the turn, so the only safe move is to
    # clear it. Losing a turn the collector refused beats corrupting one it accepted.
    if state.get("export_attempted_trace_id") == trace_id:
        _acknowledge_exported_turn(state, trace_id, [], {})
        log(f"Turn {trace_count} was already exported; cleared without re-emitting")
        return

    export = _export_turn(state, input_json, reason)
    if export is None:
        _emit_abandoned_turn(state)
        _acknowledge_exported_turn(state, trace_id, [], {})
        log(f"Closed Turn {trace_count} without a transcript ({TurnEndReason.ABANDONED.value})")
        return

    if export.payload:
        _send_span_async(export.payload)
    _acknowledge_exported_turn(state, trace_id, export.observations, export.subagents)
    log(f"Closed Turn {trace_count} ({export.reason.value})")


def _handle_stop(input_json: dict) -> None:
    """Handle Stop: export the completed turn and clean up trace state."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return
    trace_count = state.get("trace_count") or "0"

    export = _export_turn(state, input_json, TurnEndReason.COMPLETED)
    if export is None:
        _emit_abandoned_turn(state)
        _acknowledge_exported_turn(state, trace_id, [], {})
        _periodic_gc(trace_count)
        return

    # The ack travels into the detached send rather than the delivery decision
    # coming back out, so the hook exits without waiting for the POST. Safe to run
    # late: _acknowledge_exported_turn re-checks current_trace_id under the state
    # lock and bails if a newer turn has started.
    def _settle() -> None:
        _acknowledge_exported_turn(state, trace_id, export.observations, export.subagents)
        _periodic_gc(trace_count)

    if not export.payload:
        _settle()
        return
    state.set("export_attempted_trace_id", trace_id)
    _send_span_async(
        export.payload,
        on_success=_settle,
        on_success_ref=_settle_ref(state, trace_id, trace_count, export.observations, export.subagents),
    )


def _settle_ref(state, trace_id: str, trace_count: str, observations: list, subagents: dict):
    """``_settle`` as an importable reference, for the no-fork sender that cannot
    carry the closure. None when the state is in-memory only (tests)."""
    if state.state_file is None:
        return None
    return (
        f"{__name__}:settle_exported_turn",
        {
            "state_dir": str(state.state_dir),
            "state_file": str(state.state_file),
            "lock_path": str(state._lock_path) if state._lock_path else None,
            "trace_id": trace_id,
            "trace_count": trace_count,
            "observations": ToolBuffer._encode({o.tool_use_id: o for o in observations}),
            "subagents": subagents,
        },
    )


def settle_exported_turn(
    *,
    state_dir: str,
    state_file: str,
    lock_path: "str | None",
    trace_id: str,
    trace_count: str,
    observations: str,
    subagents: dict,
) -> None:
    """Acknowledge an exported turn from a detached sender process."""
    state = StateManager(
        state_dir=Path(state_dir), state_file=Path(state_file), lock_path=Path(lock_path) if lock_path else None
    )
    _acknowledge_exported_turn(state, trace_id, list(ToolBuffer._decode(observations).values()), subagents)
    _periodic_gc(trace_count)


def _handle_subagent_start(input_json: dict) -> None:
    """Handle SubagentStart: record start time + prompt keyed by agent_id."""
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id", "")
    if not agent_id:
        return
    now_ms = str(get_timestamp_ms())
    prompt = input_json.get("prompt", "") or ""
    if state.state_file is not None:
        try:
            with state._lock():
                data = state._read_safe()
                data[f"subagent_{agent_id}_start_time"] = now_ms
                if prompt:
                    data[f"subagent_{agent_id}_prompt"] = prompt
                state._write(data)
        except Exception as exc:
            error(f"Failed to record subagent {agent_id} start: {exc}")
    else:
        state.set(f"subagent_{agent_id}_start_time", now_ms)
        if prompt:
            state.set(f"subagent_{agent_id}_prompt", prompt)
    agent_state = resolve_agent_state(input_json, agent_id)
    agent_state.set("start_time", now_ms)
    if prompt:
        agent_state.set("prompt", prompt)


def _export_background_subagent(state, input_json: dict, agent_id: str, descriptor: dict) -> bool:
    """A background agent finished after the turn that launched it was exported. Render
    its transcript into that trace, under the Agent tool that spawned it."""
    transcript_path = input_json.get("agent_transcript_path") or ""
    if not isinstance(transcript_path, str) or not transcript_path or not Path(transcript_path).is_file():
        return False
    trace_id = str(descriptor.get("trace_id") or "")
    tool_span_id = str(descriptor.get("tool_span_id") or "")
    if not trace_id or not tool_span_id:
        return False

    ended_at_ms = get_timestamp_ms()
    stored_start = state.get(f"subagent_{agent_id}_start_time") or ""
    started_at_ms = int(stored_start) if stored_start.isdigit() else descriptor.get("launched_at_ms")
    agent_type = str(input_json.get("agent_type") or descriptor.get("agent_type") or "unknown")
    agent_event = AgentEvent(
        event_id=f"agent:{agent_id}",
        session_id=state.get("session_id") or "",
        turn_id=str(descriptor.get("turn_id") or state.get("trace_count") or ""),
        sequence=0,
        started_at_ms=started_at_ms if isinstance(started_at_ms, int) else None,
        ended_at_ms=ended_at_ms,
        status=EventStatus.COMPLETED,
        input=descriptor.get("prompt") or state.get(f"subagent_{agent_id}_prompt"),
        output=input_json.get("last_assistant_message") or "",
        agent_id=agent_id,
        source_id=agent_type,
    )
    graph = parse_claude_transcript(Path(transcript_path), agent_event)
    _overlay_agent_observations(graph, {"agent_state_file": str(resolve_agent_state(input_json, agent_id).state_file)})
    if graph.events and graph.events[-1].ended_at_ms:
        agent_event.ended_at_ms = max(int(e.ended_at_ms) for e in graph.events if isinstance(e.ended_at_ms, int))
    subagent_span_id = generate_span_id()
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    payload = render_event_graph(
        graph,
        trace_id=trace_id,
        service_name=SERVICE_NAME,
        scope_name=SCOPE_NAME,
        span_id_overrides={agent_event.event_id: subagent_span_id},
        extra_attributes={agent_event.event_id: {"subagent.background": "true"}},
        common_attributes={
            **({"user.id": user_id} if user_id else {}),
            **({"user.login_id": login_id} if login_id else {}),
        },
        root_parent_span_id=tool_span_id,
    )
    _send_span_async(payload)
    _update_background_agent(state, agent_id, subagent_span_id=subagent_span_id, ended_at_ms=agent_event.ended_at_ms)
    _extend_held_spans(state, trace_id, int(agent_event.ended_at_ms or ended_at_ms))
    state.delete(f"subagent_{agent_id}_start_time")
    state.delete(f"subagent_{agent_id}_prompt")
    log(f"Background subagent {agent_id} attached to trace {trace_id}")
    return True


def _handle_subagent_stop(input_json: dict) -> None:
    """Handle subagent_stop: parse subagent transcript and send CHAIN span."""
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id", "")
    background = _background_agents(state).get(agent_id) if isinstance(agent_id, str) and agent_id else None
    if background is not None and _export_background_subagent(state, input_json, agent_id, background):
        return

    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    agent_id = input_json.get("agent_id", "")
    agent_type = input_json.get("agent_type", "")
    end_time = str(get_timestamp_ms())

    # Claude exposes the subagent's own transcript only here. Buffer it so main Stop can
    # resolve agent_id back to the invoking Agent tool and splice one coherent tree,
    # rather than emitting a sibling CHAIN span detached from the tool that spawned it.
    if _buffer_subagent(state, input_json, int(end_time)):
        return

    if not agent_type or agent_type in ("unknown", "null"):
        return

    span_id = generate_span_id()
    parent = state.get("current_trace_span_id")

    # Claude Code v2 ships the assistant's final text directly.
    last_msg = input_json.get("last_assistant_message", "") or ""
    output = last_msg

    model = ""
    usage = _TokenUsage()

    # Prefer state-stored start time set by SubagentStart; fall back to transcript birth time.
    stored_start = state.get(f"subagent_{agent_id}_start_time")
    if stored_start:
        start_time = stored_start
    else:
        start_time = end_time  # default; may be overwritten below

    transcript = resolve_transcript_path(input_json, session_id or "")
    if transcript is not None:
        if not stored_start:
            st = transcript.stat()
            # st_birthtime is macOS/BSD only; fall back to ctime elsewhere.
            birth = getattr(st, "st_birthtime", st.st_ctime)
            start_time = str(int(birth * 1000))

        _wait_for_transcript_flush(transcript, 0)
        scanned_output, usage, scanned_model = _scan_transcript_for_usage(transcript, 0)
        if not output:
            output = scanned_output
        model = scanned_model

    if not output:
        output = "(No response)"

    subagent_effort = _payload_effort(input_json)

    # Subagent output is a tool-like result — redact unless opted in.
    output = redact_content(env.log_tool_content, output)

    # Build attributes
    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "subagent.id": agent_id,
        "subagent.type": agent_type,
        **({"llm.model_name": model} if model else {}),
        **({LLM_EFFORT_ATTR: subagent_effort} if subagent_effort else {}),
        **usage.token_count_attrs(),
        "output.value": output,
    }
    stored_prompt = state.get(f"subagent_{agent_id}_prompt") or ""
    if stored_prompt:
        attrs["input.value"] = redact_content(env.log_prompts, stored_prompt)
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        f"Subagent: {agent_type}",
        "CHAIN",
        span_id,
        trace_id,
        parent or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    # Clean up per-agent state keys
    if agent_id:
        state.delete(f"subagent_{agent_id}_start_time")
        state.delete(f"subagent_{agent_id}_prompt")


def _handle_stop_failure(input_json: dict) -> None:
    """Handle StopFailure: emit a span describing the failed turn so it doesn't disappear silently."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""

    error_type = input_json.get("error", "") or ""
    error_details = input_json.get("error_details", "") or ""
    last_msg = input_json.get("last_assistant_message", "") or ""

    output_text = last_msg or f"(Stop failed: {error_type})"
    redacted_prompt = redact_content(env.log_prompts, user_prompt)
    redacted_output = redact_content(env.log_prompts, output_text)
    output_messages = [{"message.role": "assistant", "message.content": redacted_output}]

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "trace.number": trace_count,
        "openinference.span.kind": "LLM",
        "input.value": redacted_prompt,
        "output.value": redacted_output,
        "llm.output_messages": json.dumps(output_messages),
        "error.type": error_type,
        "error.message": error_details,
        **turn_end_attributes(TurnEndReason.FAILED),
    }
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id
    prompt_id = state.get("current_prompt_id") or ""
    if prompt_id:
        attrs[PROMPT_ID_ATTR] = prompt_id

    span = build_span(
        f"Turn {trace_count} (failed)",
        "LLM",
        trace_span_id,
        trace_id,
        "",
        trace_start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2,
        status_message=error_type or "turn_failure",
    )
    _send_span_async(span)
    _acknowledge_exported_turn(state, trace_id, [], {})


def _handle_notification(input_json: dict) -> None:
    """Handle notification: send a CHAIN span for the notification event."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    message = redact_content(env.log_prompts, input_json.get("message", ""))
    title = redact_content(env.log_prompts, input_json.get("title", ""))
    notification_type = input_json.get("type", "info")

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "notification.message": message,
        "notification.title": title,
        "notification.type": notification_type,
        "input.value": message,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        f"Notification: {notification_type}",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_elicitation(input_json: dict) -> None:
    """Handle Elicitation: send a CHAIN span for an MCP server's request for user input."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    mcp_server = input_json.get("server_name") or input_json.get("mcp_server") or ""
    message = redact_content(
        env.log_prompts, input_json.get("message") or input_json.get("question") or ""
    )

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "elicitation.server": mcp_server,
        "elicitation.message": message,
        "input.value": message,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        "Elicitation",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_elicitation_result(input_json: dict) -> None:
    """Handle ElicitationResult: send a CHAIN span for the user's response to an elicitation."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    mcp_server = input_json.get("server_name") or input_json.get("mcp_server") or ""
    action = input_json.get("action") or input_json.get("status") or ""
    response = redact_content(
        env.log_prompts, json.dumps(input_json.get("response") or input_json.get("content") or {})
    )

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "elicitation.server": mcp_server,
        "elicitation.action": action,
        "output.value": response,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        f"Elicitation Result: {action}" if action else "Elicitation Result",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_post_tool_batch(input_json: dict) -> None:
    """Handle PostToolBatch: send a CHAIN span summarizing a resolved parallel tool-call batch."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    tool_calls = input_json.get("tool_calls") or input_json.get("tools") or []
    batch_size = len(tool_calls) if isinstance(tool_calls, list) else input_json.get("batch_size", 0)
    duration_ms = input_json.get("duration_ms") or input_json.get("total_duration_ms")

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "tool.batch_size": batch_size,
    }
    if duration_ms is not None:
        attrs["tool.batch_duration_ms"] = duration_ms
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        f"Tool Batch ({batch_size})",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_permission_request(input_json: dict) -> None:
    """Handle permission_request: send a CHAIN span for the permission event."""
    state = resolve_session(input_json)
    log(f"DEBUG permission_request input: {json.dumps(input_json)}")

    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    permission = input_json.get("permission", "")
    tool_name = input_json.get("tool_name", "")
    tool_input = redact_content(env.log_tool_details, json.dumps(input_json.get("tool_input", {})))

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "permission.type": permission,
        "permission.tool": tool_name,
        "input.value": tool_input,
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
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_permission_denied(input_json: dict) -> None:
    """Handle PermissionDenied: emit a CHAIN span recording an auto-mode tool denial."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    permission = input_json.get("permission", "")
    tool_name = input_json.get("tool_name", "")
    tool_input = redact_content(env.log_tool_details, json.dumps(input_json.get("tool_input", {})))

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "permission.type": permission,
        "permission.tool": tool_name,
        "permission.denied": "true",
        "input.value": tool_input,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    now = str(get_timestamp_ms())
    span = build_span(
        "Permission Denied",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


def _handle_session_end(input_json: dict) -> None:
    """Handle session_end: log summary and clean up state file."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    # A turn still open here was cut off by the session ending, not by a failure.
    if state.get("current_trace_id"):
        _close_live_turn(state, input_json, TurnEndReason.INTERRUPTED)
    # Agents that never reported back still need their ancestors on the wire.
    _release_all_held_spans(state)

    trace_count = state.get("trace_count") or "0"
    tool_count = state.get("tool_count") or "0"

    error(f"Session complete: {trace_count} traces, {tool_count} tools")
    error(f"View in Atatus: session.id = {session_id}")

    # Clean up state file and lock
    if state.state_file is not None:
        state.state_file.unlink(missing_ok=True)
    if state._lock_path is not None and state._lock_path.is_dir():
        try:
            state._lock_path.rmdir()
        except OSError:
            pass

    gc_stale_state_files()


def _handle_pre_compact(input_json: dict) -> None:
    """Handle PreCompact: record start time of compaction event."""
    state = resolve_session(input_json)
    state.set("compact_start_time", str(get_timestamp_ms()))
    trigger = input_json.get("trigger", "")
    if trigger:
        state.set("compact_trigger", trigger)


def _handle_post_compact(input_json: dict) -> None:
    """Handle PostCompact: emit a CHAIN span describing the compaction.

    Skip emission when compaction fires between turns (no `current_trace_id`).
    An orphan compact span in its own trace is hard to correlate in Atatus;
    matches the permission_denied/notification guard pattern.
    """
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id = state.get("current_trace_id")
    if trace_id is None:
        # Compaction between turns. Clean up pending state, no span emitted.
        state.delete("compact_start_time")
        state.delete("compact_trigger")
        return

    start_time = state.get("compact_start_time") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    trigger = input_json.get("trigger") or state.get("compact_trigger") or "unknown"

    parent = state.get("current_trace_span_id") or ""

    attrs = {
        "session.id": session_id,
        **({"turn.id": state.get("trace_count")} if state.get("trace_count") else {}),
        "openinference.span.kind": "CHAIN",
        "compact.trigger": trigger,
    }
    user_id = state.get("user_id") or ""
    login_id = state.get("user_login_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    if login_id:
        attrs["user.login_id"] = login_id

    span = build_span(
        f"Compact ({trigger})",
        "CHAIN",
        generate_span_id(),
        trace_id,
        parent,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    state.delete("compact_start_time")
    state.delete("compact_trigger")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def session_start():
    """Entry point for atatus-hook-session-start."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_session_start(input_json)
    except Exception as e:
        error(f"session_start hook failed: {e}")


def pre_tool_use():
    """Entry point for atatus-hook-pre-tool-use."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_pre_tool_use(input_json)
    except Exception as e:
        error(f"pre_tool_use hook failed: {e}")


def post_tool_use():
    """Entry point for atatus-hook-post-tool-use."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_tool_use(input_json)
    except Exception as e:
        error(f"post_tool_use hook failed: {e}")


def user_prompt_submit():
    """Entry point for atatus-hook-user-prompt-submit."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_user_prompt_submit(input_json)
    except Exception as e:
        error(f"user_prompt_submit hook failed: {e}")


def stop():
    """Entry point for atatus-hook-stop."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_stop(input_json)
    except Exception as e:
        error(f"stop hook failed: {e}")


def subagent_stop():
    """Entry point for atatus-hook-subagent-stop."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_subagent_stop(input_json)
    except Exception as e:
        error(f"subagent_stop hook failed: {e}")


def stop_failure():
    """Entry point for atatus-hook-stop-failure."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_stop_failure(input_json)
    except Exception as e:
        error(f"stop_failure hook failed: {e}")


def notification():
    """Entry point for atatus-hook-notification."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_notification(input_json)
    except Exception as e:
        error(f"notification hook failed: {e}")


def permission_request():
    """Entry point for atatus-hook-permission-request."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_permission_request(input_json)
    except Exception as e:
        error(f"permission_request hook failed: {e}")


def session_end():
    """Entry point for atatus-hook-session-end."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_session_end(input_json)
    except Exception as e:
        error(f"session_end hook failed: {e}")


def post_tool_use_failure():
    """Entry point for atatus-hook-post-tool-use-failure."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_tool_use_failure(input_json)
    except Exception as e:
        error(f"post_tool_use_failure hook failed: {e}")


def subagent_start():
    """Entry point for atatus-hook-subagent-start."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_subagent_start(input_json)
    except Exception as e:
        error(f"subagent_start hook failed: {e}")


def user_prompt_expansion():
    """Entry point for atatus-hook-user-prompt-expansion."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_user_prompt_expansion(input_json)
    except Exception as e:
        error(f"user_prompt_expansion hook failed: {e}")


def pre_compact():
    """Entry point for atatus-hook-pre-compact."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_pre_compact(input_json)
    except Exception as e:
        error(f"pre_compact hook failed: {e}")


def post_compact():
    """Entry point for atatus-hook-post-compact."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_compact(input_json)
    except Exception as e:
        error(f"post_compact hook failed: {e}")


def permission_denied():
    """Entry point for atatus-hook-permission-denied."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_permission_denied(input_json)
    except Exception as e:
        error(f"permission_denied hook failed: {e}")


def elicitation():
    """Entry point for atatus-hook-elicitation."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_elicitation(input_json)
    except Exception as e:
        error(f"elicitation hook failed: {e}")


def elicitation_result():
    """Entry point for atatus-hook-elicitation-result."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_elicitation_result(input_json)
    except Exception as e:
        error(f"elicitation_result hook failed: {e}")


def post_tool_batch():
    """Entry point for atatus-hook-post-tool-batch."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_tool_batch(input_json)
    except Exception as e:
        error(f"post_tool_batch hook failed: {e}")
