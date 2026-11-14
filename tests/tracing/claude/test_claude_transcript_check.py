#!/usr/bin/env python3
"""The transcript check flags a transcript format the parser no longer reads in full."""

import json
import os
from pathlib import Path

from tracing.claude_code.transcript_check import check_transcript, main


def _write(path: Path, records: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _assistant(uuid, message_id, blocks, version="2.1.323"):
    record = {
        "type": "assistant",
        "uuid": uuid,
        "version": version,
        "timestamp": "2026-11-13T13:03:11.000Z",
        "message": {"role": "assistant", "id": message_id, "model": "m", "content": blocks},
    }
    if uuid is None:
        del record["uuid"]
    return record


def _tool_result(tool_use_id):
    return {
        "type": "user",
        "uuid": f"res-{tool_use_id}",
        "timestamp": "2026-11-13T13:03:12.000Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]},
    }


_EDIT = {"type": "tool_use", "id": "toolu_edit", "name": "Edit", "input": {}}


def _clean(path: Path) -> Path:
    return _write(
        path,
        [
            _assistant("u1", "msg_A", [_EDIT]),
            _tool_result("toolu_edit"),
            {"type": "api-request-blob", "message": {"role": "assistant", "content": [_EDIT]}},
            {"type": "api-request", "id": "r1"},
            _assistant("u2", "msg_B", [{"type": "text", "text": "done"}]),
        ],
    )


def _drifted(path: Path) -> Path:
    return _write(
        path,
        [
            _assistant("u1", "msg_A", [_EDIT]),
            _tool_result("toolu_edit"),
            _assistant(None, "msg_copy", [_EDIT], version="9.9.9"),
        ],
    )


def test_a_transcript_the_parser_reads_in_full_is_clean(tmp_path):
    report = check_transcript(_clean(tmp_path / "t.jsonl"))
    assert not report.drifted
    assert report.model_calls == 2
    assert report.tools == 1
    assert report.skipped_record_types == {"api-request-blob": 1}
    assert report.versions == {"2.1.323"}


def test_an_assistant_record_without_a_uuid_is_drift(tmp_path):
    report = check_transcript(_drifted(tmp_path / "t.jsonl"))
    assert report.drifted
    assert report.unidentified_model_calls == 1
    assert report.duplicate_tools == 1
    assert "9.9.9" in report.versions


def test_exit_code_reports_drift(tmp_path, capsys):
    clean = _clean(tmp_path / "clean.jsonl")
    drifted = _drifted(tmp_path / "drifted.jsonl")
    assert main([str(clean)]) == 0
    assert main([str(clean), str(drifted)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out and str(drifted) in out


def test_a_directory_is_searched_newest_first_up_to_the_limit(tmp_path, capsys):
    (tmp_path / "sub").mkdir()
    old = _drifted(tmp_path / "old.jsonl")
    new = _clean(tmp_path / "sub" / "new.jsonl")
    os.utime(old, (1, 1))
    assert main([str(tmp_path), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert str(new) in out and str(old) not in out
