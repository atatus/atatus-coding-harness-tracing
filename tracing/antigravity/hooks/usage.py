"""Token usage for the Antigravity harness.

Antigravity surfaces no token counts in its hook payload or its transcript, but it
does record them: ``conversations/<conversationId>.db`` holds one ``gen_metadata``
row per model call, and inside each row's protobuf blob is a usage block.

Field layout, established empirically (there is no public schema) and consistent
across 48 conversations and five model variants::

    1.4.2   fresh input tokens, i.e. the part of the prompt that was not cached
    1.4.3   output tokens
    1.4.5   cached input tokens; absent on a call that read no cache
    1.9.10.1  running context size — not usage, and deliberately not read here

Rows are appended one per model call, in order, so the i-th row is the i-th
``PLANNER_RESPONSE`` in the transcript. That ordering was verified by correlating
each row's output-token count against the length of the response it was matched
to: aligned it scores 0.56, and shifting by one call in either direction collapses
it to 0.28 and 0.10. Counts agree exactly in 37 of 48 conversations and are within
two in all but one, so a call with no row contributes nothing rather than
borrowing its neighbour's numbers.

Everything here degrades to "no usage" rather than to a wrong number: a schema
change, a locked database, or a short row all yield an empty result, and the
caller omits the token attributes entirely. Zero is not a safe stand-in for
unknown — it reads downstream as a free turn.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

from tracing.antigravity.hooks.model import conversation_db_path

#: Path to the usage block: top-level field 1, then field 4.
_USAGE_PATH = (1, 4)

_INPUT_FIELD = 2
_OUTPUT_FIELD = 3
_CACHE_READ_FIELD = 5

#: A single call cannot plausibly exceed this. A value above it means the field
#: no longer holds what we think it holds, so the row is discarded.
_MAX_PLAUSIBLE_TOKENS = 50_000_000


class CallUsage(NamedTuple):
    """Token usage for one model call. Zero means "recorded as zero"."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int

    @property
    def prompt_tokens(self) -> int:
        """Total prompt, which OpenInference defines as including the cache."""
        return self.input_tokens + self.cache_read_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def is_empty(self) -> bool:
        return not (self.input_tokens or self.output_tokens or self.cache_read_tokens)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint at *pos*. Returns (value, next_pos); -1 on overrun."""
    value = 0
    shift = 0
    while pos < len(buf) and shift <= 63:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return -1, pos


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one protobuf message.

    ``value`` is an int for varints and bytes for length-delimited fields. Stops
    at the first malformed byte rather than raising — these blobs are read from a
    database another process is writing.
    """
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        if key < 0:
            return
        field_number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
            if value < 0:
                return
            yield field_number, wire_type, value
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            if length < 0 or pos + length > len(buf):
                return
            yield field_number, wire_type, buf[pos : pos + length]
            pos += length
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            return


def _submessage(buf: bytes, field_number: int) -> bytes | None:
    """Return the first length-delimited field *field_number* of *buf*."""
    for number, wire_type, value in _fields(buf):
        if number == field_number and wire_type == 2:
            return value
    return None


def _usage_of(blob: bytes) -> CallUsage | None:
    """Extract one call's usage from a ``gen_metadata`` blob."""
    buf: bytes | None = blob
    for field_number in _USAGE_PATH:
        if buf is None:
            return None
        buf = _submessage(buf, field_number)
    if buf is None:
        return None

    counts: dict[int, int] = {}
    for number, wire_type, value in _fields(buf):
        if wire_type == 0 and number in (_INPUT_FIELD, _OUTPUT_FIELD, _CACHE_READ_FIELD):
            counts.setdefault(number, value)

    usage = CallUsage(
        input_tokens=counts.get(_INPUT_FIELD, 0),
        output_tokens=counts.get(_OUTPUT_FIELD, 0),
        cache_read_tokens=counts.get(_CACHE_READ_FIELD, 0),
    )
    if usage.total_tokens > _MAX_PLAUSIBLE_TOKENS:
        return None
    return None if usage.is_empty() else usage


def usage_by_call(conversation_id: str) -> list[CallUsage | None]:
    """Return per-model-call usage for *conversation_id*, in call order.

    Entry ``i`` is the i-th model call of the conversation, or None where no
    usable row exists. Returns [] if the store cannot be read at all — the
    conversation is owned by a running process and failing to read it must never
    be more than a loss of token counts.
    """
    if not conversation_id:
        return []
    path = conversation_db_path(conversation_id)
    if not path.is_file():
        return []

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx").fetchall()
    except (sqlite3.Error, OSError):
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    return [_usage_of(bytes(blob)) if blob else None for (blob,) in rows]


def sum_usage(usages: list[CallUsage | None]) -> CallUsage | None:
    """Total *usages*, or None if none of them carried anything."""
    present = [u for u in usages if u is not None]
    if not present:
        return None
    return CallUsage(
        input_tokens=sum(u.input_tokens for u in present),
        output_tokens=sum(u.output_tokens for u in present),
        cache_read_tokens=sum(u.cache_read_tokens for u in present),
    )
