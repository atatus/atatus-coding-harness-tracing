#!/usr/bin/env python3
"""Tests for tracing.antigravity.hooks.usage — token counts from the store."""

from __future__ import annotations

import sqlite3

import pytest

from tracing.antigravity import constants as _c
from tracing.antigravity.hooks.usage import CallUsage, sum_usage, usage_by_call


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field_number: int, wire_type: int) -> bytes:
    return _varint((field_number << 3) | wire_type)


def _message(field_number: int, body: bytes) -> bytes:
    return _tag(field_number, 2) + _varint(len(body)) + body


def _num(field_number: int, value: int) -> bytes:
    return _tag(field_number, 0) + _varint(value)


def _usage_blob(inp: int | None = None, out: int | None = None, cache: int | None = None) -> bytes:
    """Build a blob shaped like a real gen_metadata row: 1 -> 4 -> {2,3,5}."""
    body = b""
    if inp is not None:
        body += _num(2, inp)
    if out is not None:
        body += _num(3, out)
    if cache is not None:
        body += _num(5, cache)
    return _message(1, _message(4, body))


@pytest.fixture
def store(tmp_path, monkeypatch):
    data_dir = tmp_path / "cli-data"
    monkeypatch.setattr(_c, "CLI_DATA_DIR", data_dir)

    def write(conversation_id: str, blobs: list[bytes | None]):
        path = data_dir / "conversations" / f"{conversation_id}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob, size integer DEFAULT 0)")
        for idx, blob in enumerate(blobs):
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (idx, blob))
        conn.commit()
        conn.close()

    return write


class TestCallUsage:
    def test_prompt_includes_the_cache(self):
        """OpenInference treats prompt as the whole prompt, cache being a subset."""
        usage = CallUsage(input_tokens=5597, output_tokens=103, cache_read_tokens=101747)
        assert usage.prompt_tokens == 107344
        assert usage.total_tokens == 107447

    def test_empty_is_detected(self):
        assert CallUsage(0, 0, 0).is_empty()
        assert not CallUsage(0, 1, 0).is_empty()


class TestUsageByCall:
    def test_reads_one_row_per_call_in_order(self, store):
        store("c1", [_usage_blob(100, 10, 0), _usage_blob(200, 20, 5000)])
        usage = usage_by_call("c1")
        assert usage == [
            CallUsage(100, 10, 0),
            CallUsage(200, 20, 5000),
        ]

    def test_a_call_with_no_cache_read_is_still_counted(self, store):
        store("c1", [_usage_blob(107959, 398)])
        assert usage_by_call("c1") == [CallUsage(107959, 398, 0)]

    def test_row_with_no_usage_block_becomes_none(self, store):
        """A gap must stay a gap: the next call's numbers are not this call's."""
        store("c1", [_usage_blob(100, 10), _message(1, _message(9, _num(1, 500))), _usage_blob(300, 30)])
        usage = usage_by_call("c1")
        assert usage[0] == CallUsage(100, 10, 0)
        assert usage[1] is None
        assert usage[2] == CallUsage(300, 30, 0)

    def test_all_zero_row_is_not_usage(self, store):
        store("c1", [_usage_blob(0, 0, 0)])
        assert usage_by_call("c1") == [None]

    def test_implausible_values_are_refused(self, store):
        """The store has no public schema; a moved field must not become tokens."""
        store("c1", [_usage_blob(10**12, 5)])
        assert usage_by_call("c1") == [None]

    def test_null_blob_is_not_an_error(self, store):
        store("c1", [None, _usage_blob(100, 10)])
        assert usage_by_call("c1") == [None, CallUsage(100, 10, 0)]

    def test_truncated_blob_is_not_an_error(self, store):
        store("c1", [b"\x0a\x40truncated"])
        assert usage_by_call("c1") == [None]

    def test_missing_store_is_not_an_error(self, store):
        assert usage_by_call("nope") == []

    def test_empty_conversation_id_is_not_an_error(self, store):
        assert usage_by_call("") == []

    def test_store_without_the_table_is_not_an_error(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "cli-data"
        monkeypatch.setattr(_c, "CLI_DATA_DIR", data_dir)
        path = data_dir / "conversations" / "c1.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE steps (idx integer)")
        conn.commit()
        conn.close()
        assert usage_by_call("c1") == []

    def test_corrupt_store_is_not_an_error(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "cli-data"
        monkeypatch.setattr(_c, "CLI_DATA_DIR", data_dir)
        path = data_dir / "conversations" / "c1.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a database")
        assert usage_by_call("c1") == []


class TestSumUsage:
    def test_sums_present_entries_only(self):
        assert sum_usage([CallUsage(10, 1, 100), None, CallUsage(20, 2, 200)]) == CallUsage(30, 3, 300)

    def test_all_missing_yields_none(self):
        """None must not collapse to zero — zero reads downstream as a free turn."""
        assert sum_usage([None, None]) is None
        assert sum_usage([]) is None
