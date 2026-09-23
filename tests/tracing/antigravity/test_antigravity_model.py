#!/usr/bin/env python3
"""Tests for tracing.antigravity.hooks.model — model identification."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tracing.antigravity import constants as _c
from tracing.antigravity.hooks.model import (
    conversation_db_path,
    effort_from_label,
    label_to_id,
    model_id_from_store,
    model_label_from_settings,
)


def _field19(value: bytes) -> bytes:
    """Encode *value* as protobuf field 19, length-delimited."""
    length = len(value)
    assert length < 128, "test values stay inside a one-byte varint"
    return b"\x9a\x01" + bytes([length]) + value


def _write_store(data_dir, conversation_id: str, blobs: list[bytes]) -> None:
    """Write a conversation store containing *blobs* as gen_metadata rows."""
    path = data_dir / "conversations" / f"{conversation_id}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob, size integer DEFAULT 0)")
    for idx, blob in enumerate(blobs):
        conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)", (idx, blob, len(blob)))
    conn.commit()
    conn.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "cli-data"
    d.mkdir()
    monkeypatch.setattr(_c, "CLI_DATA_DIR", d)
    return d


class TestLabelToId:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Gemini 3.7 Flash (High)", "gemini-3.7-flash"),
            ("Claude Sonnet 4.6 (Thinking)", "claude-sonnet-4.6"),
            ("Gemini 3.5 Flash (Low)", "gemini-3.5-flash"),
            ("Gemini 3.6 Flash", "gemini-3.6-flash"),
            ("", ""),
            ("(High)", ""),
        ],
    )
    def test_derives_an_id(self, label, expected):
        assert label_to_id(label) == expected


class TestEffortFromLabel:
    """Gemini labels qualify the level; Claude labels only say thinking is on."""

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Gemini 3.7 Flash (High)", ("high", False)),
            ("Gemini 3.5 Flash (Low)", ("low", False)),
            ("Gemini 3.1 Pro (Medium)", ("medium", False)),
            ("Gemini 3.5 Flash (Extra Low)", ("extra-low", False)),
            ("Claude Sonnet 4.6 (Thinking)", ("", True)),
            ("Claude Opus 4.6 (thinking)", ("", True)),
            ("Gemini 3.6 Flash", ("", False)),
            ("", ("", False)),
        ],
    )
    def test_splits_level_from_thinking(self, label, expected):
        assert effort_from_label(label) == expected


class TestSettingsLabel:
    def test_reads_the_selected_model(self, data_dir):
        (data_dir / "settings.json").write_text(json.dumps({"model": "Gemini 3.7 Flash (High)"}), encoding="utf-8")
        assert model_label_from_settings() == "Gemini 3.7 Flash (High)"

    def test_missing_file_is_not_an_error(self, data_dir):
        assert model_label_from_settings() == ""

    def test_malformed_file_is_not_an_error(self, data_dir):
        (data_dir / "settings.json").write_text("{not json", encoding="utf-8")
        assert model_label_from_settings() == ""

    def test_non_string_model_is_ignored(self, data_dir):
        (data_dir / "settings.json").write_text(json.dumps({"model": 7}), encoding="utf-8")
        assert model_label_from_settings() == ""


class TestStoreExtraction:
    def test_reads_the_model_id(self, data_dir):
        _write_store(data_dir, "c1", [b"prefix" + _field19(b"claude-sonnet-4-6") + b"suffix"])
        assert model_id_from_store("c1") == "claude-sonnet-4-6"

    def test_newest_row_wins(self, data_dir):
        """A session that switched models reports the one in force now."""
        _write_store(
            data_dir,
            "c1",
            [_field19(b"gemini-3.6-flash"), _field19(b"claude-sonnet-4-6")],
        )
        assert model_id_from_store("c1") == "claude-sonnet-4-6"

    def test_value_that_is_not_a_model_id_is_refused(self, data_dir):
        """The store has no public schema. If the field moves we must emit
        nothing rather than put an arbitrary string in llm.model_name."""
        _write_store(data_dir, "c1", [_field19(b"MODEL_PLACEHOLDER_M298")])
        assert model_id_from_store("c1") == ""

    def test_unknown_family_is_refused(self, data_dir):
        _write_store(data_dir, "c1", [_field19(b"trajectory-id-4774f272")])
        assert model_id_from_store("c1") == ""

    def test_missing_store_is_not_an_error(self, data_dir):
        assert model_id_from_store("nope") == ""

    def test_empty_conversation_id_is_not_an_error(self, data_dir):
        assert model_id_from_store("") == ""

    def test_store_without_the_table_is_not_an_error(self, data_dir):
        path = data_dir / "conversations" / "c1.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE steps (idx integer)")
        conn.commit()
        conn.close()
        assert model_id_from_store("c1") == ""

    def test_corrupt_store_is_not_an_error(self, data_dir):
        path = data_dir / "conversations" / "c1.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a database")
        assert model_id_from_store("c1") == ""

    def test_empty_rows_are_skipped(self, data_dir):
        _write_store(data_dir, "c1", [_field19(b"gemini-3.7-flash"), b""])
        assert model_id_from_store("c1") == "gemini-3.7-flash"

    def test_truncated_length_prefix_does_not_read_past_the_blob(self, data_dir):
        _write_store(data_dir, "c1", [b"\x9a\x01\x40short"])
        assert model_id_from_store("c1") == ""

    def test_path_is_built_from_the_conversation_id(self, data_dir):
        assert conversation_db_path("abc").name == "abc.db"
