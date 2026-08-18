#!/usr/bin/env python3
"""Antigravity hook handlers — transcript-driven span emission.

Antigravity hooks are a control plane: the hook stdin payload carries
``conversationId`` and ``transcriptPath`` but no model or tool content. The
actual conversation lives in the transcript file. So hooks here are only
*triggers* — the transcript parser is the source of truth.

Two hook events drive emission:

* ``PreInvocation`` — fires before each model call. Acts as a backstop: emits
  spans for any earlier turn whose ``Stop`` was missed (crash/kill). The
  in-progress (final) turn is intentionally skipped so we don't double-emit
  once ``Stop`` arrives.
* ``Stop`` — fires when the agent loop ends. Emits the just-finished turn.

Trace shape, one trace per turn::

    Turn N (CHAIN)
    ├── LLM: <model>            one per planner response
    │   ├── run_command (TOOL)  nested under the planner that requested it
    │   └── view_file  (TOOL)
    └── LLM: <model>
        └── grep_search (TOOL)

Stdout discipline: each entry point prints exactly ``{}`` (never
``{"decision": "continue"}``, which would force Antigravity's agent loop to
re-enter). All diagnostics go through ``core.common.log``/``error`` (stderr).
"""

from __future__ import annotations

import json
import os
import sys

from core.common import (
    build_multi_span,
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    log,
    redact_content,
    send_span,
)
from tracing.antigravity.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
)
from tracing.antigravity.hooks.model import (
    label_to_id,
    model_id_from_store,
    model_label_from_settings,
)
from tracing.antigravity.hooks.transcript import parse_transcript

#: The receiver's JSON body limit is 1 MB. Batched payloads are split well under
#: it: one oversized POST loses the whole turn, where two POSTs lose nothing.
_MAX_PAYLOAD_BYTES = 700_000

#: Tool arguments use PascalCase keys, except ``search_web`` whose ``query`` is
#: lowercase. First hit wins, so the more specific keys come first.
_DESCRIPTION_ARG_KEYS = (
    "CommandLine",
    "AbsolutePath",
    "FilePath",
    "DirectoryPath",
    "Url",
    "Query",
    "query",
    "SearchPath",
)

#: Arg key promoted to a first-class attribute, per tool. Mirrors the per-tool
#: attributes the other harnesses emit, so a filter written against one harness
#: means the same thing here.
_TOOL_ATTR_KEYS = {
    "run_command": ("CommandLine", "tool.command"),
    "view_file": ("AbsolutePath", "tool.file_path"),
    "write_to_file": ("AbsolutePath", "tool.file_path"),
    "replace_file_content": ("AbsolutePath", "tool.file_path"),
    "multi_replace_file_content": ("AbsolutePath", "tool.file_path"),
    "list_dir": ("DirectoryPath", "tool.file_path"),
    "grep_search": ("Query", "tool.query"),
    "search_web": ("query", "tool.query"),
    "read_url_content": ("Url", "tool.url"),
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _print_response() -> None:
    """Print exactly ``{}`` to stdout. Never ``{"decision": "continue"}``."""
    print(json.dumps({}))


def _send_payload_async(payload: dict) -> None:
    """Send one OTLP payload without blocking the hook process.

    Antigravity invokes hooks synchronously and waits for the subprocess to
    exit before resuming. The slowest part of the hook is the OTLP POST in
    send_span (up to ~10s). Double-fork detaches a grandchild reparented to
    init/launchd; the parent returns immediately so the hook exits in
    milliseconds.

    Falls back to synchronous send when ``fork()`` is unavailable (Windows)
    or when ``ATATUS_DISABLE_FORK=true`` (used by tests so spans are visible
    to ``captured_spans`` fixtures in the parent process).
    """
    if os.environ.get("ATATUS_DISABLE_FORK", "").lower() == "true":
        send_span(payload)
        return
    if not hasattr(os, "fork"):
        send_span(payload)
        return

    try:
        pid = os.fork()
    except OSError:
        send_span(payload)
        return

    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except OSError:
            # Best-effort reap: if the child is already gone/reaped, continue.
            pass
        return

    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        os._exit(0)

    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                # Best-effort stdio redirection in detached child; continue even if one fd cannot be remapped.
                pass
        os.close(devnull)
    except OSError:
        # Best-effort stdio detachment; continue even if /dev/null setup fails.
        pass
    try:
        send_span(payload)
    except Exception as exc:
        # Detached grandchild must never raise into the hook path; log and exit.
        error(f"[hooks] async span send failed in detached child: {exc}")
    os._exit(0)


def _dispatch_spans(spans: list[dict]) -> None:
    """Batch *spans* into as few payloads as the body limit allows, and send.

    A turn can carry well over a hundred spans, so one span per POST is both
    slow and a fork storm; one POST for all of them can exceed the receiver's
    limit and lose the turn entirely. Batching by serialized size gets both.
    """
    if not spans:
        return

    batch: list[dict] = []
    batch_bytes = 0

    def flush() -> None:
        nonlocal batch, batch_bytes
        if not batch:
            return
        payload = build_multi_span(batch, SERVICE_NAME, SCOPE_NAME) if len(batch) > 1 else batch[0]
        if payload:
            _send_payload_async(payload)
        batch = []
        batch_bytes = 0

    for span in spans:
        try:
            size = len(json.dumps(span))
        except (TypeError, ValueError):
            size = _MAX_PAYLOAD_BYTES
        if batch and batch_bytes + size > _MAX_PAYLOAD_BYTES:
            flush()
        batch.append(span)
        batch_bytes += size

    flush()


def _resolve_model(state, turn: dict, conversation_id: str) -> tuple[str, str]:
    """Return (model_id, model_label) for *turn*, remembering it on the session.

    The transcript names the model only on the turn the user switched it, so the
    last known value is carried forward — that alone is the difference between
    a model on a quarter of turns and a model on all of them.
    """
    label = turn.get("model_label") or ""
    if label:
        state.set("model_label", label)
    else:
        label = state.get("model_label") or ""

    model_id = model_id_from_store(conversation_id)
    if model_id:
        state.set("model_id", model_id)
    else:
        model_id = state.get("model_id") or ""

    if not label:
        label = model_label_from_settings()
        if label:
            state.set("model_label", label)

    if not model_id:
        model_id = label_to_id(label)

    return model_id, label


# ---------------------------------------------------------------------------
# Span assembly
# ---------------------------------------------------------------------------


def _tool_description(name: str, args: dict, args_json: str) -> str:
    """Build a short, scannable description from the most informative argument."""
    if not isinstance(args, dict):
        return args_json[:200]
    for key in _DESCRIPTION_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val:
            return val[:200]
    return args_json[:200]


def _output_messages(content: str, tool_calls: list) -> str:
    """Encode a planner response as OpenInference ``llm.output_messages``.

    A planner response that only issues tool calls has no text at all — the tool
    calls *are* its output, and this is where they belong. Without it those
    spans carry nothing, which is most of them.
    """
    message: dict = {"message.role": "assistant"}
    if content:
        message["message.content"] = content
    calls = []
    for call in tool_calls or []:
        name = call.get("name") or ""
        if not name:
            continue
        try:
            arguments = json.dumps(call.get("args") or {})
        except (TypeError, ValueError):
            arguments = "{}"
        calls.append({"tool_call.function": {"name": name, "arguments": arguments}})
    if calls:
        message["message.tool_calls"] = calls
    return json.dumps([message])


def _build_tool_span(
    tool: dict,
    trace_id: str,
    parent_span_id: str,
    parentage: str,
    common: dict,
) -> dict:
    """Build one TOOL span."""
    tool_name = tool.get("name", "") or ""
    args = tool.get("args") or {}
    output_text = tool.get("output", "") or ""

    try:
        args_json = json.dumps(args)
    except (TypeError, ValueError):
        args_json = str(args)

    attrs: dict = dict(common)
    attrs.update(
        {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": redact_content(env.log_tool_details, args_json),
            "output.value": redact_content(env.log_tool_content, output_text),
            "tool.description": redact_content(env.log_tool_details, _tool_description(tool_name, args, args_json)),
            "tracing.parentage": parentage,
        }
    )

    promoted = _TOOL_ATTR_KEYS.get(tool_name)
    if promoted and isinstance(args, dict):
        arg_key, attr_name = promoted
        value = args.get(arg_key)
        if isinstance(value, str) and value:
            attrs[attr_name] = redact_content(env.log_tool_details, value)

    exit_code = tool.get("exit_code")
    if isinstance(exit_code, int):
        attrs["tool.exit_code"] = exit_code
    if tool.get("running"):
        attrs["tool.status"] = "running"
    result_type = tool.get("result_type") or ""
    if result_type:
        attrs["antigravity.result_type"] = result_type

    failed = bool(tool.get("failed"))
    status_message = ""
    if failed:
        status_message = f"exit code {exit_code}" if isinstance(exit_code, int) else "tool failed"

    return build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id,
        parent_span_id,
        tool.get("start_ms", 0),
        tool.get("end_ms", 0),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2 if failed else 1,
        status_message=status_message,
    )


def _build_turn_spans(
    turn: dict,
    trace_number: int,
    model_id: str,
    model_label: str,
    common: dict,
) -> list[dict]:
    """Build the CHAIN Turn span plus its LLM and TOOL children."""
    trace_id = generate_trace_id()
    root_span_id = generate_span_id()

    user_input = turn.get("user_input", "") or ""
    final_response = turn.get("final_response", "") or ""
    redacted_input = redact_content(env.log_prompts, user_input)

    root_attrs: dict = dict(common)
    root_attrs.update(
        {
            "openinference.span.kind": "CHAIN",
            "trace.number": str(trace_number),
            "input.value": redacted_input,
            "output.value": redact_content(env.log_prompts, final_response),
        }
    )
    if model_id:
        root_attrs["llm.model_name"] = model_id
    if model_label and model_label != model_id:
        root_attrs["antigravity.model_label"] = model_label

    turn_error = turn.get("error") or ""
    if turn_error:
        root_attrs["error.message"] = turn_error
    turn_error_code = turn.get("error_code") or ""
    if turn_error_code:
        root_attrs["error.code"] = turn_error_code

    spans = [
        build_span(
            f"Turn {trace_number}",
            "CHAIN",
            root_span_id,
            trace_id,
            "",
            turn.get("start_ms", 0),
            turn.get("end_ms", 0),
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
            status_code=2 if turn_error else 1,
            status_message=turn_error[:200] if turn_error else "",
        )
    ]

    # An LLM step is worth a span when it produced text, reasoning, or tool
    # calls. A planner response with none of the three is a no-op the model
    # emitted; a span for it says nothing and only lengthens the waterfall.
    llm_span_ids: dict[int, str] = {}
    for idx, llm in enumerate(turn.get("llm_steps") or []):
        content = llm.get("content", "") or ""
        thinking = llm.get("thinking", "") or ""
        tool_calls = llm.get("tool_calls") or []
        if not content and not thinking and not tool_calls:
            continue

        span_id = generate_span_id()
        llm_span_ids[idx] = span_id

        attrs: dict = dict(common)
        attrs.update(
            {
                "openinference.span.kind": "LLM",
                "input.value": redacted_input,
                "output.value": redact_content(env.log_prompts, content),
                "llm.output_messages": redact_content(env.log_prompts, _output_messages(content, tool_calls)),
            }
        )
        if model_id:
            attrs["llm.model_name"] = model_id
        if model_label and model_label != model_id:
            attrs["antigravity.model_label"] = model_label
        if idx == 0 and user_input:
            attrs["llm.input_messages"] = redact_content(
                env.log_prompts,
                json.dumps([{"message.role": "user", "message.content": user_input}]),
            )
        if thinking:
            attrs["llm.reasoning"] = redact_content(env.log_prompts, thinking)
        # The span covers the step including the tools it ran, so the model's
        # own response time would otherwise be unrecoverable from the trace.
        latency_ms = llm.get("latency_ms", 0)
        if isinstance(latency_ms, int) and latency_ms > 0:
            attrs["llm.latency_ms"] = latency_ms

        step_error = llm.get("error") or ""
        if step_error:
            attrs["error.message"] = step_error
        step_error_code = llm.get("error_code") or ""
        if step_error_code:
            attrs["error.code"] = step_error_code

        spans.append(
            build_span(
                f"LLM: {model_id}" if model_id else "LLM",
                "LLM",
                span_id,
                trace_id,
                root_span_id,
                llm.get("start_ms", 0),
                llm.get("end_ms", 0),
                attrs,
                SERVICE_NAME,
                SCOPE_NAME,
                status_code=2 if step_error else 1,
                status_message=step_error[:200] if step_error else "",
            )
        )

    for tool in turn.get("tool_steps") or []:
        llm_index = tool.get("llm_index", -1)
        parent_span_id = llm_span_ids.get(llm_index, "")
        if parent_span_id:
            parentage = "planner_response"
        else:
            parent_span_id = root_span_id
            parentage = "turn_fallback"
        spans.append(_build_tool_span(tool, trace_id, parent_span_id, parentage, common))

    return spans


def _emit_turn_spans(turn: dict, trace_number: int, model_id: str, model_label: str, common: dict) -> None:
    """Emit one turn's spans as one or more batched OTLP payloads."""
    _dispatch_spans(_build_turn_spans(turn, trace_number, model_id, model_label, common))


def _emit_completed_turns(state, turns: list[dict], include_last: bool, conversation_id: str) -> None:
    """Emit spans for every completed turn whose ordinal is past the watermark.

    A turn is "complete" when it is not the most recent turn in ``turns``, or
    when ``include_last`` is True (set by ``Stop``). ``PreInvocation`` calls
    this with ``include_last=False`` so the in-progress final turn isn't
    emitted twice.
    """
    if not turns:
        return

    try:
        last_turn = int(state.get("last_emitted_turn") or "-1")
    except ValueError:
        last_turn = -1

    try:
        trace_count = int(state.get("trace_count") or "0")
    except ValueError:
        trace_count = 0

    common: dict = {
        "session.id": state.get("session_id") or "",
        "project.name": state.get("project_name") or "",
    }
    user_id = state.get("user_id") or ""
    if user_id:
        common["user.id"] = user_id

    final_idx = len(turns) - 1
    for i, turn in enumerate(turns):
        if i == final_idx and not include_last:
            continue
        if i <= last_turn:
            continue
        trace_count += 1
        model_id, model_label = _resolve_model(state, turn, conversation_id)
        _emit_turn_spans(turn, trace_count, model_id, model_label, common)
        last_turn = i

    state.set("last_emitted_turn", str(last_turn))
    state.set("trace_count", str(trace_count))


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _handle_pre_invocation(input_json: dict) -> None:
    """Backstop: emit spans for any earlier turn whose Stop was missed."""
    debug_dump("antigravity_pre_invocation", input_json)
    state = resolve_session(input_json)
    if state is None:
        log("antigravity pre_invocation: no conversationId or transcriptPath; skipping")
        return
    ensure_session_initialized(state, input_json)
    turns = parse_transcript(input_json.get("transcriptPath", "") or "")
    _emit_completed_turns(state, turns, False, input_json.get("conversationId", "") or "")


def _handle_stop(input_json: dict) -> None:
    """Emit spans for the just-finished turn (and any earlier missed turns)."""
    debug_dump("antigravity_stop", input_json)
    state = resolve_session(input_json)
    if state is None:
        log("antigravity stop: no conversationId or transcriptPath; skipping")
        return
    ensure_session_initialized(state, input_json)
    turns = parse_transcript(input_json.get("transcriptPath", "") or "")
    _emit_completed_turns(state, turns, True, input_json.get("conversationId", "") or "")
    gc_stale_state_files()
    session_id = state.get("session_id") or ""
    if session_id:
        log(f"antigravity stop: emitted up to turn {state.get('last_emitted_turn')}")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def pre_invocation() -> None:
    """Entry point for atatus-hook-antigravity-pre-invocation."""
    try:
        input_json = _read_stdin()
        if check_requirements():
            _handle_pre_invocation(input_json)
    except Exception as e:
        error(f"antigravity pre_invocation hook failed: {e}")
    finally:
        _print_response()


def stop() -> None:
    """Entry point for atatus-hook-antigravity-stop."""
    try:
        input_json = _read_stdin()
        if check_requirements():
            _handle_stop(input_json)
    except Exception as e:
        error(f"antigravity stop hook failed: {e}")
    finally:
        _print_response()


def main() -> None:
    """Manual execution dispatcher."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <handler_name>", file=sys.stderr)
        sys.exit(1)

    handler_name = sys.argv[1]
    handlers = {
        "pre_invocation": pre_invocation,
        "stop": stop,
    }

    handler = handlers.get(handler_name)
    if not handler:
        print(f"unknown handler: {handler_name}", file=sys.stderr)
        sys.exit(1)

    handler()


if __name__ == "__main__":
    main()
