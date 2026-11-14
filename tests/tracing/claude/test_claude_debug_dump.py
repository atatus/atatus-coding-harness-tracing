"""With ATATUS_TRACE_DEBUG on, every Claude hook leaves its raw payload on disk."""

import io
import json
import sys
from unittest import mock

import core.constants as constants
import tracing.claude_code.hooks.handlers as handlers


def _read(payload):
    with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))):
        return handlers._read_stdin()


def _dumps(tmp_path):
    return sorted((tmp_path / "debug").glob("*.json"))


def test_payload_is_dumped_under_its_hook_event(tmp_path, monkeypatch):
    monkeypatch.setenv("ATATUS_TRACE_DEBUG", "true")
    monkeypatch.setattr(constants, "STATE_BASE_DIR", tmp_path)
    payload = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"}

    assert _read(payload) == payload

    [dump] = _dumps(tmp_path)
    assert dump.name.startswith("claude-code_SubagentStop_")
    assert json.loads(dump.read_text(encoding="utf-8"))["payload"] == payload


def test_nothing_is_written_without_the_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("ATATUS_TRACE_DEBUG", raising=False)
    monkeypatch.setattr(constants, "STATE_BASE_DIR", tmp_path)

    _read({"hook_event_name": "Stop", "session_id": "s1"})

    assert not (tmp_path / "debug").exists()


def test_an_agent_payload_carries_the_agent_meta_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ATATUS_TRACE_DEBUG", "true")
    monkeypatch.setattr(constants, "STATE_BASE_DIR", tmp_path / "state")
    transcript = tmp_path / "proj" / "s1.jsonl"
    meta = tmp_path / "proj" / "s1" / "subagents" / "agent-a1.meta.json"
    meta.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    meta.write_text(json.dumps({"agentType": "fork", "finished": False}), encoding="utf-8")

    _read({"hook_event_name": "PostToolUse", "session_id": "s1", "agent_id": "a1", "transcript_path": str(transcript)})

    [dump] = _dumps(tmp_path / "state")
    assert json.loads(dump.read_text(encoding="utf-8"))["agent_meta"] == {"agentType": "fork", "finished": False}


def test_an_unsafe_event_name_cannot_escape_the_debug_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ATATUS_TRACE_DEBUG", "true")
    monkeypatch.setattr(constants, "STATE_BASE_DIR", tmp_path)

    _read({"hook_event_name": "../../evil", "session_id": "s1", "agent_id": "../x"})

    [dump] = _dumps(tmp_path)
    assert dump.parent == tmp_path / "debug"
    assert "/" not in dump.name and ".." not in dump.name
