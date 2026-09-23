"""Parser for Copilot session-state transcript (events.jsonl).

The Stop hook payload supplies a transcript path pointing at a JSONL file with
one event per line. Each line is:

    {"type": "<kind>", "data": {...}, "id": "<uuid>",
     "timestamp": "<iso>", "parentId": "<uuid|null>"}

We extract the model, the assistant's answer and its output tokens for the turn
that just ended, so the Stop span carries a real `llm.model_name`,
`output.value`, and token count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.common import normalize_effort

#: `session.model_change` reports the user's *selection*, which is frequently
#: this placeholder rather than a model. The served model only ever appears on
#: `assistant.message`, so a name from that event always wins.
_MODEL_PLACEHOLDERS = frozenset({"auto", "default", ""})


def _turn_scoped_defaults() -> dict[str, Any]:
    return {"output_text": "", "output_tokens": 0, "tool_count": 0}


def parse_transcript(path: str | Path) -> dict[str, Any]:
    """Parse the events.jsonl at *path* and summarise the most recent turn.

    Returns a dict with the following keys (all optional — missing keys mean
    the transcript did not provide that info):

      model_name        -- str: the model that actually served the turn.
      effort            -- str: the reasoning effort in force for that turn.
      copilot_version   -- str: from `session.start`.
      input_text        -- str: the most recent user prompt.
      output_text       -- str: the assistant's answer for the latest turn.
      output_tokens     -- int: output tokens summed across that turn.
      tool_count        -- int: tool executions started during that turn.
      events_seen       -- int: total parsed events (debug aid).

    Everything after the last `user.message` is treated as the latest turn,
    which is the one whose Stop hook is firing.

    On a missing file or unreadable path, returns {} (the caller treats that as
    "no transcript data" and falls back to state-only attributes).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return {}

    summary: dict[str, Any] = {
        "model_name": "",
        "effort": "",
        "copilot_version": "",
        "input_text": "",
        "events_seen": 0,
    }
    summary.update(_turn_scoped_defaults())

    selected_model = ""

    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                summary["events_seen"] += 1
                kind = ev.get("type", "")
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue

                if kind == "session.start":
                    summary["copilot_version"] = data.get("copilotVersion", "") or summary["copilot_version"]

                elif kind == "session.model_change":
                    new_model = str(data.get("newModel", "") or "")
                    if new_model.lower() not in _MODEL_PLACEHOLDERS:
                        selected_model = new_model
                    summary["effort"] = normalize_effort(data.get("reasoningEffort")) or summary["effort"]

                elif kind == "session.resume":
                    summary["effort"] = normalize_effort(data.get("reasoningEffort")) or summary["effort"]

                elif kind == "user.message":
                    content = data.get("content", "")
                    if isinstance(content, str) and content:
                        summary["input_text"] = content
                    # A new prompt starts a new turn; drop the previous one's answer.
                    summary.update(_turn_scoped_defaults())

                elif kind == "assistant.message":
                    model = str(data.get("model", "") or "")
                    if model.lower() not in _MODEL_PLACEHOLDERS:
                        summary["model_name"] = model
                    content = data.get("content", "")
                    if isinstance(content, str) and content:
                        summary["output_text"] = content
                    tokens = data.get("outputTokens")
                    if isinstance(tokens, int):
                        summary["output_tokens"] += tokens

                elif kind == "tool.execution_start":
                    summary["tool_count"] += 1
    except OSError:
        return {}

    if not summary["model_name"]:
        summary["model_name"] = selected_model

    return summary
