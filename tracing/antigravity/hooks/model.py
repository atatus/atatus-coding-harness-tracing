"""Model identification for the Antigravity harness.

The transcript names the model only on a turn where the user *switched* models,
and names it as a display label ("Gemini 3.7 Flash (High)") rather than an id.
Three sources are combined so that every span carries a model:

1. The conversation store — ``conversations/<conversationId>.db``, table
   ``gen_metadata`` — records the id the request actually ran against
   (``claude-sonnet-4-6``, ``gemini-3.7-flash``). This is the only per-request
   source and the only one shaped like something a pricing table can match.
2. The transcript's settings-change block, carried forward across turns by the
   caller. A label, but an exact one.
3. The CLI's ``settings.json`` — the currently selected model. A label, and only
   current, so it is the last resort.

The store is a protobuf blob with no public schema, so extraction is guarded:
a value that does not look like a model id is discarded and the label is used
instead. A schema change therefore degrades this to source 2/3 rather than
putting an arbitrary string in ``llm.model_name``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from core.common import normalize_effort
from tracing.antigravity import constants as _c

#: Field 19, length-delimited, inside a ``gen_metadata`` blob. Empirically the
#: model the request ran against, sitting just before the ``model_enum`` key.
_MODEL_FIELD_TAG = b"\x9a\x01"

#: Model ids must look like one. Families are product names and change rarely;
#: anything else falls back to the display label, which is never wrong.
_MODEL_ID_RE = re.compile(r"^(?:gemini|claude|gpt|o\d|grok|llama|deepseek|qwen|mistral)[a-z0-9]*(?:[.\-][a-z0-9]+)*$")

#: A label such as "Gemini 3.7 Flash (High)" carries a qualifier in parentheses
#: that is a reasoning/effort setting, not part of the model's identity.
_LABEL_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _cli_data_dir() -> Path:
    """Return the CLI data dir, re-read each call so tests can redirect it."""
    return _c.CLI_DATA_DIR


def conversation_db_path(conversation_id: str) -> Path:
    """Return the conversation store path for *conversation_id*."""
    return _cli_data_dir() / "conversations" / f"{conversation_id}.db"


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint at *pos*. Returns (value, next_pos)."""
    value = 0
    shift = 0
    while pos < len(buf) and shift <= 28:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return -1, pos


def _extract_model_ids(blob: bytes) -> list[str]:
    """Pull every plausible model id out of one ``gen_metadata`` blob."""
    found: list[str] = []
    pos = 0
    while True:
        at = blob.find(_MODEL_FIELD_TAG, pos)
        if at < 0:
            return found
        length, after_len = _read_varint(blob, at + len(_MODEL_FIELD_TAG))
        pos = at + len(_MODEL_FIELD_TAG)
        if length <= 0 or length > 64 or after_len + length > len(blob):
            continue
        raw = blob[after_len : after_len + length]
        try:
            candidate = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        if _MODEL_ID_RE.match(candidate):
            found.append(candidate)


def model_id_from_store(conversation_id: str) -> str:
    """Return the model id the conversation last ran against, or "".

    Reads the newest ``gen_metadata`` rows only — a session that switched models
    should report the one in force now, not the one it opened with. Never
    raises: the store belongs to a running process and any failure to read it
    just means we fall back to a label.
    """
    if not conversation_id:
        return ""
    path = conversation_db_path(conversation_id)
    if not path.is_file():
        return ""

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5").fetchall()
    except (sqlite3.Error, OSError):
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    for (blob,) in rows:
        if not blob:
            continue
        ids = _extract_model_ids(bytes(blob))
        if ids:
            return ids[-1]
    return ""


def model_label_from_settings() -> str:
    """Return the currently selected model label from the CLI settings, or ""."""
    path = _cli_data_dir() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    model = data.get("model")
    return model.strip() if isinstance(model, str) else ""


def effort_from_label(label: str) -> tuple[str, bool]:
    """Return (effort, thinking) from a display label's parenthesised qualifier.

    "Gemini 3.8 Flash (High)" is an effort level; "Claude Opus 4.6 (Thinking)"
    is extended thinking being on, with no level attached. Reporting the second
    as an effort would put a mode into an enum of levels.
    """
    if not label:
        return "", False
    match = _LABEL_QUALIFIER_RE.search(label)
    if not match:
        return "", False
    qualifier = match.group(0).strip().strip("()").strip()
    if qualifier.lower() == "thinking":
        return "", True
    return normalize_effort(qualifier), False


def label_to_id(label: str) -> str:
    """Derive an id-shaped name from a display label.

    "Gemini 3.7 Flash (High)" -> "gemini-3.7-flash". Used only when the store
    yields nothing, so the model column holds something joinable rather than a
    string with spaces and a qualifier in it.
    """
    if not label:
        return ""
    stripped = _LABEL_QUALIFIER_RE.sub("", label).strip()
    if not stripped:
        return ""
    return re.sub(r"\s+", "-", stripped).lower()
