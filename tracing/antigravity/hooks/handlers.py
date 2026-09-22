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

    Turn N (CHAIN)                       the turn — carries the prompt and final response
    ├── LLM call 1: <model> (LLM)        one per planner response, with its own tokens
    │   ├── run_command (TOOL)           nested under the call that requested it
    │   └── view_file  (TOOL)
    └── LLM call 2: <model> (LLM)
        └── grep_search (TOOL)

This matches the Claude Code harness and Arize's whole fleet: the root is CHAIN and
every model call is its own LLM span carrying its own tokens, so cost is attributable
per call rather than only per turn.

The previous shape inverted these kinds to hold "exactly one LLM-kind span per trace",
because the Traces page counted chat spans as rows. That constraint is retired — the
page is now backed by a trace-grained query — and the reason it ever existed is
recorded in ERRORS/error-found-during-arize-to-atatus-migration.md.

Stdout discipline: each entry point prints exactly ``{}`` (never
``{"decision": "continue"}``, which would force Antigravity's agent loop to
re-enter). All diagnostics go through ``core.common.log``/``error`` (stderr).
"""

from __future__ import annotations

import json
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
    send_span_async,
)
from tracing.antigravity.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
)
from tracing.antigravity.hooks.model import label_to_id, model_id_from_store, model_label_from_settings
from tracing.antigravity.hooks.transcript import parse_transcript, turn_is_waiting
from tracing.antigravity.hooks.usage import CallUsage, usage_by_call

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


def _send_payload_async(payload: dict, on_success=None) -> None:
    """Detached span send. ``sender`` keeps this module's ``send_span`` binding
    on the synchronous fallback path so test doubles still intercept it."""
    send_span_async(payload, sender=send_span, on_success=on_success)


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


def _token_attrs(usage: "CallUsage | None") -> dict:
    """OpenInference token attributes, omitting anything not recorded.

    Never emit a zero: downstream a zero token count is a priced turn that cost
    nothing, where absence is "we do not know". ``prompt`` is the whole prompt
    and the cache bucket is a subset of it, which is the convention the consumer
    reads.
    """
    if usage is None or usage.is_empty():
        return {}
    attrs: dict = {
        "llm.token_count.prompt": usage.prompt_tokens,
        "llm.token_count.completion": usage.output_tokens,
        "llm.token_count.total": usage.total_tokens,
    }
    if usage.cache_read_tokens:
        attrs["llm.token_count.prompt_details.cache_read"] = usage.cache_read_tokens
    return {k: v for k, v in attrs.items() if v}


def _build_turn_spans(
    turn: dict,
    trace_number: int,
    model_id: str,
    model_label: str,
    common: dict,
    call_usage: list | None = None,
) -> list[dict]:
    """Build the CHAIN Turn span plus its per-call LLM and TOOL descendants.

    Each model call is its own LLM span carrying its own tokens, so cost is
    attributable per call. The turn is a CHAIN container holding the prompt and the
    final response. This is the Claude Code shape and Arize's fleet-wide shape.
    """
    trace_id = generate_trace_id()
    root_span_id = generate_span_id()

    user_input = turn.get("user_input", "") or ""
    final_response = turn.get("final_response", "") or ""
    redacted_input = redact_content(env.log_prompts, user_input)
    redacted_output = redact_content(env.log_prompts, final_response)

    root_attrs: dict = dict(common)
    root_attrs.update(
        {
            "openinference.span.kind": "CHAIN",
            "trace.number": str(trace_number),
            "input.value": redacted_input,
            "output.value": redacted_output,
        }
    )
    if user_input:
        root_attrs["llm.input_messages"] = redact_content(
            env.log_prompts,
            json.dumps([{"message.role": "user", "message.content": user_input}]),
        )
    if final_response:
        root_attrs["llm.output_messages"] = redact_content(
            env.log_prompts,
            json.dumps([{"message.role": "assistant", "message.content": final_response}]),
        )
    if model_label and model_label != model_id:
        root_attrs["antigravity.model_label"] = model_label

    steps = [
        step
        for step in (turn.get("llm_steps") or [])
        if (step.get("content") or step.get("thinking") or step.get("tool_calls"))
    ]
    if steps:
        root_attrs["llm.call_count"] = len(steps)

    turn_error = turn.get("error") or ""
    if turn_error:
        root_attrs["error.message"] = turn_error
    turn_error_code = turn.get("error_code") or ""
    if turn_error_code:
        root_attrs["error.code"] = turn_error_code

    spans = [
        build_span(
            f"Turn {trace_number}",
            "LLM",
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

    # A step is worth a span when it produced text, reasoning, or tool calls. A
    # planner response with none of the three is a no-op the model emitted; a
    # span for it says nothing and only lengthens the waterfall.
    step_span_ids: dict[int, str] = {}
    step_number = 0
    for idx, llm in enumerate(turn.get("llm_steps") or []):
        content = llm.get("content", "") or ""
        thinking = llm.get("thinking", "") or ""
        tool_calls = llm.get("tool_calls") or []
        if not content and not thinking and not tool_calls:
            continue

        step_number += 1
        span_id = generate_span_id()
        step_span_ids[idx] = span_id

        attrs: dict = dict(common)
        attrs.update(
            {
                "openinference.span.kind": "LLM",
                "input.value": redacted_input,
                "output.value": redact_content(env.log_prompts, content),
                "llm.output_messages": redact_content(env.log_prompts, _output_messages(content, tool_calls)),
                "antigravity.step": step_number,
            }
        )
        if model_id:
            attrs["llm.model_name"] = model_id
        # Per call, not summed onto the turn: the whole point of the model-call layer is
        # that cost is attributable to the step that spent it. The turn carries none, so
        # a range sum over the trace still totals each turn exactly once.
        call_index = llm.get("model_call_index", -1)
        if isinstance(call_index, int) and 0 <= call_index < len(call_usage or []):
            attrs.update(_token_attrs((call_usage or [])[call_index]))
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
                # The ordinal is required, not cosmetic: the UI span bucketer collapses
                # 3+ adjacent same-name siblings and re-parents their children to depth 0.
                f"LLM call {step_number}: {model_id}" if model_id else f"LLM call {step_number}",
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
        parent_span_id = step_span_ids.get(llm_index, "")
        if parent_span_id:
            parentage = "model_call"
        else:
            parent_span_id = root_span_id
            parentage = "turn_fallback"
        spans.append(_build_tool_span(tool, trace_id, parent_span_id, parentage, common))

    return spans


def _emit_turn_spans(
    turn: dict,
    trace_number: int,
    model_id: str,
    model_label: str,
    common: dict,
    call_usage: list,
) -> None:
    """Emit one turn's spans as one or more batched OTLP payloads."""
    _dispatch_spans(_build_turn_spans(turn, trace_number, model_id, model_label, common, call_usage))


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
    }
    user_id = state.get("user_id") or ""
    if user_id:
        common["user.id"] = user_id
    user_login_id = state.get("user_login_id")
    if user_login_id is None:
        user_login_id = env.get_user_login_id(SERVICE_NAME)
        state.set("user_login_id", user_login_id)
    if user_login_id:
        common["user.login_id"] = user_login_id

    # Read once per hook invocation rather than once per turn: the store is owned
    # by a running process and a backstop run can emit several turns at once.
    call_usage = usage_by_call(conversation_id)

    final_idx = len(turns) - 1
    for i, turn in enumerate(turns):
        if i == final_idx and not include_last:
            continue
        if i <= last_turn:
            continue
        if i == final_idx and turn_is_waiting(turn):
            # The agent yielded because it put a tool in the background, not
            # because the turn ended. Emitting now would freeze the turn at this
            # point: the watermark below would mark it done and everything after
            # the pause would never be emitted. A later stop, once the task has
            # reported back, emits the whole turn. A turn that is no longer the
            # last one is emitted regardless — the user moved on, so it will
            # never settle.
            log(f"antigravity: turn {i} is waiting on a background task; deferring")
            break
        trace_count += 1
        model_id, model_label = _resolve_model(state, turn, conversation_id)
        _emit_turn_spans(turn, trace_count, model_id, model_label, common, call_usage)
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
