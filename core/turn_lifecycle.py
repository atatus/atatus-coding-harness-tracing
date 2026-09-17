"""Shared vocabulary for how a coding-agent turn ends.

Every harness closes its root turn span through here so the reason, the status code and
the attribute names agree across the fleet. A turn the user interrupted or superseded is
not an error; only a real failure, or a turn we lost all sight of, is.
"""

from __future__ import annotations

from typing import Any

from core.common import build_span
from core.event_model import EventStatus, TurnEndReason

TURN_END_REASON_ATTR = "turn.end_reason"
TURN_INCOMPLETE_ATTR = "turn.incomplete"
TURN_PROMPT_COUNT_ATTR = "turn.prompt_count"
PROMPT_ID_ATTR = "prompt.id"

ABANDONED_MESSAGE = "Turn did not complete: no end-of-turn signal"
ABANDONED_OUTPUT = "(Turn closed without an end-of-turn signal)"

_INCOMPLETE = frozenset({TurnEndReason.INTERRUPTED, TurnEndReason.CONTINUED, TurnEndReason.ABANDONED})


def turn_end_attributes(reason: TurnEndReason) -> dict[str, str]:
    attrs = {TURN_END_REASON_ATTR: reason.value}
    if reason in _INCOMPLETE:
        attrs[TURN_INCOMPLETE_ATTR] = "true"
    return attrs


def turn_status(reason: TurnEndReason, message: str = "") -> tuple[int, str]:
    if reason is TurnEndReason.FAILED:
        return 2, message or "Turn failed"
    if reason is TurnEndReason.ABANDONED:
        return 2, message or ABANDONED_MESSAGE
    return 1, ""


def event_status_for(reason: TurnEndReason) -> EventStatus:
    if reason is TurnEndReason.FAILED:
        return EventStatus.FAILED
    if reason in (TurnEndReason.INTERRUPTED, TurnEndReason.CONTINUED):
        return EventStatus.CANCELLED
    return EventStatus.COMPLETED


def close_turn_span(
    name: str,
    kind: str,
    span_id: str,
    trace_id: str,
    start_ms: "int | str",
    end_ms: "int | str",
    attrs: dict[str, Any],
    service_name: str,
    scope_name: str,
    reason: TurnEndReason,
    status_message: str = "",
) -> dict:
    """Build a root turn span for a harness that has no transcript to replay."""
    status_code, message = turn_status(reason, status_message)
    return build_span(
        name,
        kind,
        span_id,
        trace_id,
        "",
        start_ms,
        end_ms,
        {**attrs, **turn_end_attributes(reason)},
        service_name,
        scope_name,
        status_code=status_code,
        status_message=message,
    )


__all__ = [
    "ABANDONED_MESSAGE",
    "ABANDONED_OUTPUT",
    "PROMPT_ID_ATTR",
    "TURN_END_REASON_ATTR",
    "TURN_INCOMPLETE_ATTR",
    "TURN_PROMPT_COUNT_ATTR",
    "TurnEndReason",
    "close_turn_span",
    "event_status_for",
    "turn_end_attributes",
    "turn_status",
]
