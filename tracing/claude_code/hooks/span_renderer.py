"""Render typed coding-agent events as OpenInference-compatible OTLP JSON spans."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from core.common import (
    LLM_EFFORT_ATTR,
    build_multi_span,
    build_span,
    env,
    generate_span_id,
    redact_content,
)
from core.event_model import (
    AgentEvent,
    BaseEvent,
    EventGraph,
    EventStatus,
    ModelCallEvent,
    PromptEvent,
    ToolEvent,
    TurnEvent,
)
from core.turn_lifecycle import TURN_PROMPT_COUNT_ATTR, turn_end_attributes


def render_event_graph(
    graph: EventGraph,
    *,
    trace_id: str,
    service_name: str = "coding-harness-tracing",
    scope_name: str = "coding-harness-tracing",
    span_id_factory: Callable[[], str] = generate_span_id,
    span_id_overrides: Mapping[str, str] | None = None,
    extra_attributes: Mapping[str, Mapping[str, Any]] | None = None,
    common_attributes: Mapping[str, Any] | None = None,
    root_parent_span_id: str = "",
    skip_root: bool = False,
) -> dict:
    """Render every event in graph order under one trace.

    Event relationships are resolved before rendering, so a child may safely
    reference a parent that appears later in a tolerant/partially ordered graph.
    Missing parents fail soft and become root spans; graph diagnostics retain the
    reason for callers that need to report schema drift.

    ``root_parent_span_id`` parents every otherwise-parentless span to a span that
    already exists in the trace. ``skip_root`` renders only the descendants of the
    first event, for a continuation whose root span was sent by an earlier hook.
    """

    overrides = span_id_overrides or {}
    extras = extra_attributes or {}
    common = dict(common_attributes or {})
    event_span_ids: list[str] = []
    first_span_by_event_id: dict[str, str] = {}
    used_span_ids: set[str] = set()
    for event in graph.events:
        span_id = overrides.get(event.event_id) if event.event_id not in first_span_by_event_id else None
        span_id = span_id or span_id_factory()
        attempts = 0
        while span_id in used_span_ids:
            span_id = span_id_factory() if attempts < 100 else generate_span_id()
            attempts += 1
        used_span_ids.add(span_id)
        event_span_ids.append(span_id)
        first_span_by_event_id.setdefault(event.event_id, span_id)
    safe_parent_ids = _safe_parent_event_ids(graph.events)
    subtree_end = _subtree_end_ms(graph.events, safe_parent_ids)
    model_call_number = 0
    # The turn's own prompt is number one; absorbed prompts count on from there.
    prompt_number = 1
    payloads: list[dict] = []
    graph_start = _first_timestamp(graph.events, "started_at_ms")
    graph_end = _last_timestamp(graph.events, "ended_at_ms") or graph_start

    for index, event in enumerate(graph.events):
        if isinstance(event, ModelCallEvent):
            model_call_number += 1
        elif isinstance(event, PromptEvent):
            prompt_number += 1
        if skip_root and index == 0:
            continue
        name, kind, attrs = _span_fields(event, model_call_number, prompt_number)
        for key, value in common.items():
            attrs.setdefault(key, value)
        attrs.update(extras.get(event.event_id, {}))
        parent_span_id = first_span_by_event_id.get(safe_parent_ids[index] or "", "")
        if skip_root and parent_span_id == event_span_ids[0]:
            parent_span_id = root_parent_span_id
        elif not parent_span_id:
            parent_span_id = root_parent_span_id
        start_ms = _safe_timestamp(event.started_at_ms, graph_start)
        end_ms = _safe_timestamp(event.ended_at_ms, start_ms or graph_end)
        # A model call is stamped when its response lands, but the tools it asked for
        # run after that; a parent has to outlast its children or the chart unnests them.
        end_ms = max(end_ms, subtree_end[index])
        if end_ms < start_ms:
            end_ms = start_ms
        status_code, status_message = _status(event)
        payloads.append(
            build_span(
                name=name,
                kind=kind,
                span_id=event_span_ids[index],
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                start_ms=start_ms or 0,
                end_ms=end_ms or start_ms or 0,
                attrs=attrs,
                service_name=service_name,
                scope_name=scope_name,
                status_code=status_code,
                status_message=status_message,
            )
        )

    return build_multi_span(payloads, service_name, scope_name)


def _span_fields(event: BaseEvent, model_call_number: int, prompt_number: int) -> tuple[str, str, dict[str, Any]]:
    attrs: dict[str, Any] = {
        "session.id": event.session_id,
        "turn.id": event.turn_id,
    }

    if isinstance(event, TurnEvent):
        attrs["openinference.span.kind"] = "CHAIN"
        _put_content(attrs, "input.value", _turn_input(event), env.log_prompts)
        _put_content(attrs, "output.value", event.output, env.log_prompts)
        if event.additional_prompts:
            attrs[TURN_PROMPT_COUNT_ATTR] = str(1 + len(event.additional_prompts))
        if event.end_reason is not None:
            attrs.update(turn_end_attributes(event.end_reason))
        return f"Turn {event.turn_id}", "CHAIN", attrs

    if isinstance(event, PromptEvent):
        attrs["openinference.span.kind"] = "CHAIN"
        _put_content(attrs, "input.value", event.input, env.log_prompts)
        return f"User prompt {prompt_number}", "CHAIN", attrs

    if isinstance(event, AgentEvent):
        attrs.update(
            {
                "openinference.span.kind": "AGENT",
                "subagent.id": event.agent_id or "",
                "subagent.type": event.source_id or "unknown",
            }
        )
        _put_content(attrs, "input.value", event.input, env.log_prompts)
        _put_content(attrs, "output.value", event.output, env.log_tool_content)
        return f"Subagent: {event.source_id or event.agent_id or 'unknown'}", "AGENT", attrs

    if isinstance(event, ModelCallEvent):
        attrs["openinference.span.kind"] = "LLM"
        if event.model:
            attrs["llm.model_name"] = event.model
        if event.effort:
            attrs[LLM_EFFORT_ATTR] = event.effort
        if event.source_id:
            attrs["llm.message.id"] = event.source_id
        if event.agent_id:
            attrs["subagent.id"] = event.agent_id
        if event.usage is not None and not event.usage.is_empty():
            prompt_tokens = event.usage.prompt_tokens
            completion_tokens = event.usage.output_tokens
            attrs.update(
                {
                    "llm.token_count.prompt": prompt_tokens,
                    "llm.token_count.completion": completion_tokens,
                    "llm.token_count.total": prompt_tokens + completion_tokens,
                }
            )
            # Sent rather than left to be derived as prompt - cache_read - cache_write.
            # The subtraction is only correct while prompt is cache-inclusive, and a
            # consumer that reads prompt the other way silently derives a negative.
            if event.usage.input_tokens:
                attrs["llm.token_count.prompt_details.input"] = event.usage.input_tokens
            if event.usage.cache_read_tokens:
                attrs["llm.token_count.prompt_details.cache_read"] = event.usage.cache_read_tokens
            if event.usage.cache_write_tokens:
                attrs["llm.token_count.prompt_details.cache_write"] = event.usage.cache_write_tokens
            # Billed at 2x input against 1.25x for the five-minute default.
            if event.usage.cache_write_1h_tokens:
                attrs["llm.token_count.prompt_details.cache_write_1h"] = event.usage.cache_write_1h_tokens
        _put_content(attrs, "input.value", event.input, env.log_prompts)
        _put_content(attrs, "output.value", event.output, env.log_prompts)
        suffix = f": {event.model}" if event.model else ""
        return f"LLM call {model_call_number}{suffix}", "LLM", attrs

    if isinstance(event, ToolEvent):
        attrs.update(
            {
                "openinference.span.kind": "TOOL",
                "tool.name": event.tool_name or "unknown",
                "tool.call.id": event.tool_call_id or "",
            }
        )
        if event.agent_id:
            attrs["subagent.id"] = event.agent_id
        _put_content(attrs, "input.value", event.input, env.log_tool_content)
        _put_content(attrs, "output.value", event.output, env.log_tool_content)
        _put_content(attrs, "error.message", event.error, env.log_tool_content)
        _put_tool_details(attrs, event)
        return event.tool_name or "Tool", "TOOL", attrs

    attrs["openinference.span.kind"] = "CHAIN"
    _put_content(attrs, "input.value", event.input, env.log_prompts)
    _put_content(attrs, "output.value", event.output, env.log_prompts)
    return event.event_id, "CHAIN", attrs


def _put_tool_details(attrs: dict[str, Any], event: ToolEvent) -> None:
    tool_input = event.input if isinstance(event.input, dict) else {}
    details: dict[str, Any] = {}
    if event.tool_name == "Bash":
        details["tool.command"] = tool_input.get("command")
        details["tool.description"] = tool_input.get("description") or tool_input.get("command")
    elif event.tool_name in {"Read", "Write", "Edit", "Glob"}:
        details["tool.file_path"] = tool_input.get("file_path") or tool_input.get("pattern")
        details["tool.description"] = details["tool.file_path"]
    elif event.tool_name == "Grep":
        details["tool.query"] = tool_input.get("pattern")
        details["tool.file_path"] = tool_input.get("path")
    elif event.tool_name == "WebSearch":
        details["tool.query"] = tool_input.get("query")
    elif event.tool_name == "WebFetch":
        details["tool.url"] = tool_input.get("url")

    for key, value in details.items():
        if value is not None:
            attrs[key] = redact_content(env.log_tool_details, _content_string(value))


def _turn_input(event: TurnEvent) -> Any:
    if not event.additional_prompts:
        return event.input
    parts = [_content_string(event.input)] if event.input is not None else []
    parts.extend(event.additional_prompts)
    return "\n\n".join(part for part in parts if part)


def _put_content(attrs: dict[str, Any], key: str, value: Any, allowed: bool) -> None:
    if value is None:
        return
    attrs[key] = redact_content(allowed, _content_string(value))


def _content_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _status(event: BaseEvent) -> tuple[int, str]:
    allowed = env.log_tool_content if isinstance(event, (AgentEvent, ToolEvent)) else env.log_prompts
    message = redact_content(allowed, event.error) if event.error else ""
    if event.status is EventStatus.FAILED:
        return 2, message or "Event failed"
    if event.status in {EventStatus.PENDING, EventStatus.RUNNING, EventStatus.UNKNOWN}:
        return 0, message
    return 1, message


def _first_timestamp(events: list[BaseEvent], attribute: str) -> int:
    values = [value for event in events if (value := _valid_timestamp(getattr(event, attribute))) is not None]
    return min(values) if values else 0


def _last_timestamp(events: list[BaseEvent], attribute: str) -> int:
    values = [value for event in events if (value := _valid_timestamp(getattr(event, attribute))) is not None]
    return max(values) if values else 0


def _subtree_end_ms(events: list[BaseEvent], parent_ids: list[str | None]) -> list[int]:
    """Latest end in each event's subtree, so a parent span can be closed no earlier
    than its last descendant."""
    index_by_id: dict[str, int] = {}
    for index, event in enumerate(events):
        index_by_id.setdefault(event.event_id, index)
    ends = [_valid_timestamp(event.ended_at_ms) or 0 for event in events]
    for index in range(len(events) - 1, -1, -1):
        parent = parent_ids[index]
        parent_index = index_by_id.get(parent) if parent else None
        if parent_index is not None and parent_index != index:
            ends[parent_index] = max(ends[parent_index], ends[index])
    return ends


def _valid_timestamp(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _safe_timestamp(value: Any, fallback: int) -> int:
    valid = _valid_timestamp(value)
    return valid if valid is not None else fallback


def _safe_parent_event_ids(events: list[BaseEvent]) -> list[str | None]:
    """Resolve parents first-wins and break one edge in every parent cycle."""

    first_by_id: dict[str, BaseEvent] = {}
    order: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.event_id not in first_by_id:
            first_by_id[event.event_id] = event
            order[event.event_id] = index

    parent_by_id = {
        event_id: event.parent_event_id
        for event_id, event in first_by_id.items()
        if event.parent_event_id in first_by_id
    }
    break_ids: set[str] = set()
    for start in first_by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current in parent_by_id:
            if current in positions:
                cycle = path[positions[current] :]
                break_ids.add(min(cycle, key=order.__getitem__))
                break
            positions[current] = len(path)
            path.append(current)
            current = parent_by_id[current]

    return [
        (
            None
            if event.event_id in break_ids and first_by_id[event.event_id] is event
            else event.parent_event_id if event.parent_event_id in first_by_id else None
        )
        for event in events
    ]


__all__ = ["render_event_graph"]
