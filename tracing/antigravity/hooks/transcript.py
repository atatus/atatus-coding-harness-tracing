"""Parser for Antigravity transcript JSONL.

Antigravity emits a transcript file (one JSON object per line) that captures the
ground-truth conversation: user inputs, planner responses (model turns), tool
calls, and tool results. The Stop / PreInvocation hooks only signal *when* to
read the transcript — the transcript itself is the source of truth.

`parse_transcript` is a pure function: no logging, no span building, no imports
from `core`. It splits the transcript into turns (one per ``USER_INPUT``) and
returns a list of structured dicts.

Record shape worth knowing before changing anything here:

* Every record carries ``source``, one of ``USER_EXPLICIT`` / ``SYSTEM`` /
  ``MODEL``. ``SYSTEM`` marks conversation scaffolding — checkpoints, system
  notices, errors, directory rules — which is never a tool result.
* A tool result is any ``MODEL`` record that is not a ``PLANNER_RESPONSE``. Its
  ``type`` is a screaming-case label that often differs from the tool's own name
  (``replace_file_content`` results arrive as ``CODE_ACTION``, ``manage_task`` as
  ``GENERIC``), so results are paired positionally against the requesting
  planner, never by name.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_ADDITIONAL_METADATA_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
_USER_SETTINGS_CHANGE_RE = re.compile(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", re.DOTALL)
_MODEL_SELECTION_RE = re.compile(
    r"changed setting `Model Selection` from .*? to (.+?)\.(?=\s+[A-Z]|\s*$)",
    re.DOTALL,
)
_CREATED_AT_RE = re.compile(r"^Created At:\s*(\S+)", re.MULTILINE)
_COMPLETED_AT_RE = re.compile(r"^Completed At:\s*(\S+)", re.MULTILINE)

#: Records the model itself did not produce. None of these is a tool result, so
#: none of them may consume a pending tool call. ``GENERIC`` looks like it
#: belongs here and does not — it is ``MODEL``-sourced and carries the results of
#: ``manage_task``, ``schedule`` and ``list_permissions``.
_SYSTEM_SOURCE = "SYSTEM"

#: Model-sourced records that are still not tool results.
_NON_TOOL_MODEL_TYPES = {"PLANNER_RESPONSE", "CONVERSATION_HISTORY"}

_ERROR_MESSAGE_TYPE = "ERROR_MESSAGE"


def _iso_to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp (e.g. ``2026-06-09T16:00:11Z``) to epoch ms.

    Returns ``0`` on any parse failure.
    """
    if not value:
        return 0
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _extract_user_input(content: str) -> str:
    """Pull the user prompt out of a ``USER_INPUT`` record's content."""
    match = _USER_REQUEST_RE.search(content)
    if match:
        return match.group(1).strip()
    stripped = _ADDITIONAL_METADATA_RE.sub("", content)
    stripped = _USER_SETTINGS_CHANGE_RE.sub("", stripped)
    return stripped.strip()


def _extract_model_label(content: str) -> str:
    """Best-effort extraction of the model label from the user-settings block.

    Only present on a turn where the user actually switched models, which is a
    small minority of them. Callers are expected to carry the last known value
    forward across turns rather than treating "" as "no model".
    """
    match = _MODEL_SELECTION_RE.search(content)
    if match:
        return match.group(1).strip()
    return ""


def _is_tool_result(rec: dict[str, Any]) -> bool:
    """True if *rec* is a tool result that should consume a pending tool call."""
    if rec.get("source") == _SYSTEM_SOURCE:
        return False
    rec_type = rec.get("type", "")
    if not rec_type:
        return False
    return rec_type not in _NON_TOOL_MODEL_TYPES and rec_type != "USER_INPUT"


def _result_timing(rec: dict[str, Any]) -> tuple[int, int]:
    """Return (start_ms, end_ms) for a tool result record.

    Tool results embed their own ``Created At:`` / ``Completed At:`` lines, which
    are truer than the record's own ``created_at`` (that one is written when the
    result is appended, not when the tool ran). Both are second-granular, so a
    sub-second tool legitimately yields a zero duration.
    """
    content = rec.get("content", "") or ""
    fallback_ms = _iso_to_ms(rec.get("created_at", "") or "")

    created_match = _CREATED_AT_RE.search(content)
    completed_match = _COMPLETED_AT_RE.search(content)

    start_ms = _iso_to_ms(created_match.group(1)) if created_match else 0
    end_ms = _iso_to_ms(completed_match.group(1)) if completed_match else 0
    if start_ms == 0:
        start_ms = fallback_ms
    if end_ms == 0:
        end_ms = fallback_ms
    return start_ms, end_ms


def _next_planner_ms(records: list[dict[str, Any]], idx: int) -> int:
    """Epoch-ms of the next ``PLANNER_RESPONSE`` after *idx*, or 0 if it is the last."""
    for rec in records[idx + 1 :]:
        if rec.get("type") == "PLANNER_RESPONSE":
            return _iso_to_ms(rec.get("created_at", "") or "")
    return 0


def _new_tool_step(
    call: dict[str, Any],
    rec: dict[str, Any] | None,
    llm_index: int,
) -> dict[str, Any]:
    """Build a tool step from a pending call and the result record that closed it.

    *rec* is None when no result ever arrived — the call is reported with its
    arguments and no output rather than dropped, so the trace still shows what
    the model asked for.
    """
    if rec is None:
        return {
            "name": call["name"],
            "args": call["args"],
            "output": "",
            "step_index": call["step_index"],
            "start_ms": call["planner_ms"],
            "end_ms": call["planner_ms"],
            "exit_code": None,
            "failed": False,
            "running": False,
            "result_type": "",
            "llm_index": llm_index,
            "call_index": call["call_index"],
        }

    start_ms, end_ms = _result_timing(rec)
    exit_code = rec.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = None

    return {
        "name": call["name"],
        "args": call["args"],
        "output": rec.get("content", "") or "",
        "step_index": rec.get("step_index", 0),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "exit_code": exit_code,
        "failed": exit_code is not None and exit_code != 0,
        "running": rec.get("status") == "RUNNING",
        "result_type": rec.get("type", "") or "",
        "llm_index": llm_index,
        "call_index": call["call_index"],
    }


def _build_turn(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a single Turn dict from an in-order list of records.

    The first record is expected to be the ``USER_INPUT`` record. Any
    ``CONVERSATION_HISTORY`` records have already been filtered out by the
    caller.
    """
    user_record = records[0]
    user_content = user_record.get("content", "") or ""

    turn: dict[str, Any] = {
        "user_input": _extract_user_input(user_content),
        "final_response": "",
        "model_label": _extract_model_label(user_content),
        "max_step_index": 0,
        "start_ms": 0,
        "end_ms": 0,
        "llm_steps": [],
        "tool_steps": [],
        "error": "",
        "error_code": "",
    }

    step_indices = [r.get("step_index", 0) for r in records if "step_index" in r]
    if step_indices:
        turn["max_step_index"] = max(step_indices)

    timestamps = [_iso_to_ms(r.get("created_at", "") or "") for r in records]
    timestamps = [t for t in timestamps if t > 0]
    if timestamps:
        turn["start_ms"] = timestamps[0]
        turn["end_ms"] = timestamps[-1]

    # A missing record should only affect its own planner's calls
    pending_calls: list[dict[str, Any]] = []
    pending_owner = -1
    last_planner_content = ""

    def _flush_pending_calls() -> None:
        """Emit any call whose result never arrived: args, no output."""
        for call in pending_calls:
            turn["tool_steps"].append(_new_tool_step(call, None, pending_owner))
        pending_calls.clear()

    for idx, rec in enumerate(records):
        rec_type = rec.get("type", "")

        if rec_type == "PLANNER_RESPONSE":
            _flush_pending_calls()
            start_ms = _iso_to_ms(rec.get("created_at", "") or "")

            # The step runs until the model is invoked again: the tool calls it
            # issued execute inside that window, so its span encloses its own
            # children instead of ending at the first result and leaving them
            # hanging past it. The model's own latency is kept separately.
            end_ms = _next_planner_ms(records, idx) or turn["end_ms"] or start_ms
            if end_ms < start_ms:
                end_ms = start_ms

            latency_ms = 0
            if idx + 1 < len(records):
                next_ms = _iso_to_ms(records[idx + 1].get("created_at", "") or "")
                if next_ms > start_ms:
                    latency_ms = next_ms - start_ms

            content = rec.get("content", "") or ""
            calls = [c for c in (rec.get("tool_calls") or []) if isinstance(c, dict)]
            normalized_calls = []
            for call in calls:
                args = call.get("args", {}) or {}
                if not isinstance(args, dict):
                    args = {}
                normalized_calls.append({"name": call.get("name", "") or "", "args": args})

            turn["llm_steps"].append(
                {
                    "content": content,
                    "thinking": rec.get("thinking", "") or "",
                    "step_index": rec.get("step_index", 0),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "latency_ms": latency_ms,
                    "tool_calls": normalized_calls,
                    "error": "",
                    "error_code": "",
                }
            )
            if content:
                last_planner_content = content

            pending_owner = len(turn["llm_steps"]) - 1
            for call_index, call in enumerate(normalized_calls):
                pending_calls.append(
                    {
                        "name": call["name"],
                        "args": call["args"],
                        "step_index": rec.get("step_index", 0),
                        "planner_ms": start_ms,
                        "call_index": call_index,
                    }
                )
            continue

        if rec_type == "USER_INPUT":
            continue

        if rec.get("source") == _SYSTEM_SOURCE:
            # Scaffolding, not a tool result. An error record is the one piece
            # worth keeping: it is how a rate limit or a rejected tool call
            # shows up at all, and it belongs to the planner it interrupted.
            if rec_type == _ERROR_MESSAGE_TYPE:
                message = str(rec.get("error", "") or rec.get("content", "") or "")
                code = str(rec.get("error_code", "") or "")
                if turn["llm_steps"]:
                    step = turn["llm_steps"][-1]
                    step["error"] = message
                    step["error_code"] = code
                if not turn["error"]:
                    turn["error"] = message
                    turn["error_code"] = code
            continue

        if _is_tool_result(rec):
            if not pending_calls:
                # Drop the phantom result locally rather than let it consume
                # a slot and shift every pairing after it.
                continue
            call = pending_calls.pop(0)
            turn["tool_steps"].append(_new_tool_step(call, rec, pending_owner))

    _flush_pending_calls()
    turn["final_response"] = last_planner_content

    return turn


def parse_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Parse an Antigravity transcript JSONL into a list of Turn dicts.

    Prefers a sibling ``transcript_full.jsonl`` if present (it carries the full
    untruncated record content). Returns ``[]`` on any missing/unreadable file.
    """
    if not path:
        return []
    p = Path(path).expanduser()
    full = p.with_name("transcript_full.jsonl")
    if full.is_file():
        target = full
    elif p.is_file():
        target = p
    else:
        return []

    records: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "CONVERSATION_HISTORY":
                    continue
                records.append(rec)
    except OSError:
        return []

    turns: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("type") == "USER_INPUT":
            if current:
                turns.append(_build_turn(current))
            current = [rec]
        else:
            if current:
                current.append(rec)
    if current:
        turns.append(_build_turn(current))

    return turns
