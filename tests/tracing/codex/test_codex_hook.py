#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.handlers — rollout-driven notify handler."""

from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from tracing.codex.hooks.handlers import (
    _build_and_send_spans,
    _extract_turn_from_rollout,
    _find_rollout_file,
    _handle_notify,
    _handle_session_end,
    _iso_to_ms,
    _llm_identity,
    _send_legacy_single_span,
    notify,
)


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Opt in to raw content so assertions can check redacted text."""
    monkeypatch.setenv("ATATUS_LOG_PROMPTS", "true")
    monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")


@pytest.fixture(autouse=True)
def _isolate_sessions_root(tmp_path, monkeypatch):
    """Point _CODEX_SESSIONS_ROOT at a temp dir so tests don't read real rollouts."""
    import tracing.codex.hooks.handlers as h

    monkeypatch.setattr(h, "_CODEX_SESSIONS_ROOT", tmp_path / "sessions")
    # A CODEX_HOME inherited from the developer's shell would otherwise redirect
    # the lookup out of tmp_path.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _write_rollout(tmp_path, session_id, *records, day="20"):
    """Write a rollout JSONL under the standard YYYY/MM/DD path. Returns the path."""
    folder = tmp_path / "sessions" / "2026" / "05" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-05-{day}T00-00-00-{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _evt(payload):
    return {"timestamp": "2026-05-20T00:00:00.000Z", "type": "event_msg", "payload": payload}


def _resp(payload, ts="2026-05-20T00:00:01.000Z"):
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def _attrs_of_span(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


def _kind(span):
    return _attrs_of_span(span)["openinference.span.kind"]["stringValue"]


def _string_attrs(span):
    return {k: v["stringValue"] for k, v in _attrs_of_span(span).items()}


def _at(seconds):
    return f"2026-05-20T00:00:{seconds:06.3f}Z"


_T0 = _iso_to_ms(_at(0))


def _ms(seconds):
    return _T0 + round(seconds * 1000)


def _item(item, started, completed, turn_id="t1"):
    return {
        "timestamp": _at(completed),
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "s1",
            "turn_id": turn_id,
            "item": item,
            "started_at_ms": _ms(started),
            "completed_at_ms": _ms(completed),
        },
    }


def _exec_call(call_id, at):
    return _resp(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "name": "exec",
            "call_id": call_id,
            "input": "await tools.exec_command({cmd: 'ls'});",
        },
        ts=_at(at),
    )


def _exec_output(call_id, at, output="Script completed"):
    return _resp({"type": "custom_tool_call_output", "call_id": call_id, "output": output}, ts=_at(at))


def _command(item_id, cmd="ls", output="a.txt"):
    return {
        "type": "CommandExecution",
        "id": item_id,
        "command": ["/bin/zsh", "-lc", cmd],
        "cwd": "/repo",
        "source": "unified_exec_startup",
        "status": "completed",
        "aggregated_output": output,
        "exit_code": 0,
    }


def _file_change(item_id, path="/repo/app.py"):
    return {
        "type": "FileChange",
        "id": item_id,
        "changes": {path: {"type": "update", "unified_diff": "@@ -1 +1 @@\n-a\n+b\n", "move_path": None}},
        "status": "completed",
        "stdout": f"Success. Updated the following files:\nM {path}\n",
        "stderr": "",
    }


def _mcp_call(item_id, server="docs", tool="search"):
    return {
        "type": "McpToolCall",
        "id": item_id,
        "server": server,
        "tool": tool,
        "arguments": {"q": "retry policy"},
        "status": "completed",
        "result": {"content": [{"type": "text", "text": "found 2 pages"}], "isError": False},
    }


def _image_view(item_id, path="/repo/shot.png"):
    return {"type": "ImageView", "id": item_id, "path": path}


def _web_search(item_id, query="retry policy"):
    return {"type": "WebSearch", "id": item_id, "query": query, "action": {"type": "search", "query": query}}


def _web_extension(item_id, url="https://example.com/docs"):
    return {
        "type": "Extension",
        "kind": "web.search",
        "id": item_id,
        "query": url,
        "action": {"type": "openPage", "url": url},
        "results": [{"type": "text_result", "title": "Docs", "url": url}],
    }


def _child(item, started, completed):
    return {"item": item, "start_ts": _ms(started), "end_ts": _ms(completed)}


def _turn(tool_calls, cwd=""):
    return {
        "trace_count": 1,
        "turn_start_ms": _ms(0),
        "turn_end_ms": _ms(10),
        "duration_ms": 10000,
        "user_prompt": "hi",
        "assistant_output": "hello",
        "model": "gpt-5.5",
        "cwd": cwd,
        "permission_mode": "",
        "sandbox_mode": "",
        "token_usage": None,
        "tool_calls": tool_calls,
    }


@pytest.fixture
def _identity(monkeypatch):
    monkeypatch.setenv("ATATUS_USER_ID", "user-1")
    monkeypatch.setattr("core.common._IDENTITY_DETECTORS", {"codex": lambda: "dev@example.com"})


@pytest.fixture
def _no_identity(monkeypatch):
    monkeypatch.setenv("ATATUS_USER_ID", "")
    monkeypatch.setattr("core.common._IDENTITY_DETECTORS", {"codex": lambda: ""})


# ---------------------------------------------------------------------------
# _iso_to_ms
# ---------------------------------------------------------------------------


class TestIsoToMs:

    def test_valid_iso_with_Z(self):
        assert _iso_to_ms("2026-05-20T23:42:45.649Z") > 0

    def test_valid_iso_with_offset(self):
        assert _iso_to_ms("2026-05-20T23:42:45.649+00:00") > 0

    def test_empty_returns_zero(self):
        assert _iso_to_ms("") == 0

    def test_bad_format_returns_zero(self):
        assert _iso_to_ms("not-a-date") == 0


# ---------------------------------------------------------------------------
# _find_rollout_file
# ---------------------------------------------------------------------------


class TestFindRolloutFile:

    def test_returns_none_when_root_missing(self, tmp_path):
        nonexistent = tmp_path / "no" / "sessions"
        assert _find_rollout_file("s1", sessions_root=nonexistent) is None

    def test_returns_none_for_empty_session_id(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        assert _find_rollout_file("", sessions_root=tmp_path / "sessions") is None

    def test_matches_by_session_id(self, tmp_path):
        path = _write_rollout(tmp_path, "sess-abc", _evt({"type": "task_started", "turn_id": "t"}))
        found = _find_rollout_file("sess-abc", sessions_root=tmp_path / "sessions")
        assert found == path

    def test_searches_codex_home_when_set(self, tmp_path, monkeypatch):
        """A user running with CODEX_HOME keeps rollouts there, not in ~/.codex."""
        alt = tmp_path / "alt-codex"
        alt.mkdir()
        path = _write_rollout(alt, "sess-alt", _evt({"type": "task_started", "turn_id": "t"}))
        monkeypatch.setenv("CODEX_HOME", str(alt))

        # No explicit sessions_root — resolution must come from CODEX_HOME.
        assert _find_rollout_file("sess-alt") == path

    def test_explicit_root_still_wins_over_codex_home(self, tmp_path, monkeypatch):
        alt = tmp_path / "alt-codex"
        alt.mkdir()
        _write_rollout(alt, "sess-x", _evt({"type": "task_started", "turn_id": "t"}))
        expected = _write_rollout(tmp_path, "sess-x", _evt({"type": "task_started", "turn_id": "t"}))
        monkeypatch.setenv("CODEX_HOME", str(alt))

        assert _find_rollout_file("sess-x", sessions_root=tmp_path / "sessions") == expected

    def test_invalid_codex_home_falls_back_instead_of_raising(self, tmp_path, monkeypatch):
        """A stale CODEX_HOME must never take down the hook — it degrades to the default."""
        path = _write_rollout(tmp_path, "sess-def", _evt({"type": "task_started", "turn_id": "t"}))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "gone"))

        # _CODEX_SESSIONS_ROOT is pinned to tmp_path/"sessions" by the autouse fixture.
        assert _find_rollout_file("sess-def") == path


# ---------------------------------------------------------------------------
# _extract_turn_from_rollout
# ---------------------------------------------------------------------------


class TestExtractTurnFromRollout:

    def test_returns_none_for_missing_file(self, tmp_path):
        from pathlib import Path

        assert _extract_turn_from_rollout(Path(str(tmp_path / "missing.jsonl")), "t1") is None

    def test_returns_none_for_empty_turn_id(self, tmp_path):
        path = _write_rollout(tmp_path, "s1", _evt({"type": "task_started", "turn_id": "t1"}))
        assert _extract_turn_from_rollout(path, "") is None

    def test_returns_none_when_turn_not_found(self, tmp_path):
        path = _write_rollout(tmp_path, "s1", _evt({"type": "task_started", "turn_id": "other"}))
        assert _extract_turn_from_rollout(path, "missing") is None

    def test_extracts_user_prompt_and_assistant_message(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}),
            _evt({"type": "user_message", "message": "hello"}),
            _evt({"type": "agent_message", "message": "world"}),
            _evt(
                {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": "world",
                    "completed_at": 1010,
                    "duration_ms": 10000,
                }
            ),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn is not None
        assert turn["user_prompt"] == "hello"
        assert turn["assistant_output"] == "world"
        assert turn["turn_start_ms"] == 1000 * 1000
        assert turn["turn_end_ms"] == 1010 * 1000
        assert turn["duration_ms"] == 10000

    def test_task_complete_overrides_intermediate_agent_message(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "agent_message", "message": "interim"}),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "final"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["assistant_output"] == "final"

    def test_turn_context_supplies_model_cwd_permission(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "t1",
                    "model": "gpt-5.5",
                    "cwd": "/x",
                    "approval_policy": "on-request",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            },
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["model"] == "gpt-5.5"
        assert turn["cwd"] == "/x"
        assert turn["permission_mode"] == "on-request"
        assert turn["sandbox_mode"] == "workspace-write"

    def test_turn_context_supplies_effort(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "turn_context",
                "payload": {"turn_id": "t1", "model": "gpt-5.5", "effort": "xhigh"},
            },
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        assert _extract_turn_from_rollout(path, "t1")["effort"] == "xhigh"

    def test_another_turns_effort_is_not_borrowed(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "turn_context",
                "payload": {"turn_id": "t0", "model": "gpt-5.5", "effort": "low"},
            },
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        assert _extract_turn_from_rollout(path, "t1")["effort"] == ""

    def test_thread_settings_cover_a_turn_with_no_turn_context(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "thread_settings_applied", "thread_settings": {"reasoning_effort": "medium"}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        assert _extract_turn_from_rollout(path, "t1")["effort"] == "medium"

    def test_token_count_summed(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                            "cached_input_tokens": 80,
                            "reasoning_output_tokens": 5,
                        }
                    },
                }
            ),
            _evt(
                {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}},
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        usage = turn["token_usage"]
        assert usage["prompt_tokens"] == 150
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 180
        assert usage["cached_input_tokens"] == 80
        assert usage["reasoning_output_tokens"] == 5

    def test_non_cached_input_tokens_derived_only_when_both_observed(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 30,
                            "cache_write_input_tokens": 10,
                        }
                    },
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        usage = turn["token_usage"]
        assert usage["non_cached_input_tokens"] == 70
        assert usage["cache_write_input_tokens"] == 10

    def test_token_count_rebroadcast_not_double_counted(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 100},
                        "last_token_usage": {"input_tokens": 100},
                    },
                }
            ),
            # rate-limit-only rebroadcast: same total_token_usage snapshot
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 100},
                        "last_token_usage": {"input_tokens": 100},
                    },
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["token_usage"]["prompt_tokens"] == 100

    def test_session_meta_supplies_model_provider(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "session_meta",
                "payload": {"model_provider": "anthropic"},
            },
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["model_provider"] == "anthropic"

    def test_function_call_pairs_with_output_by_call_id(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
                ts="2026-05-20T00:00:01.000Z",
            ),
            _resp(
                {"type": "function_call_output", "call_id": "c1", "output": "file1\nfile2"},
                ts="2026-05-20T00:00:02.000Z",
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "exec_command"
        assert tc["call_id"] == "c1"
        assert tc["args"] == '{"cmd":"ls"}'
        assert tc["output"] == "file1\nfile2"
        assert tc["end_ts"] > tc["start_ts"]

    def test_custom_tool_call_pairs_with_output_by_call_id(self, tmp_path):
        """Code Mode emits its outer exec as custom_tool_call, not function_call."""
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "exec",
                    "call_id": "custom-1",
                    "input": 'await tools.exec_command({cmd: "ls"});',
                },
                ts="2026-05-20T00:00:01.000Z",
            ),
            _resp(
                {"type": "custom_tool_call_output", "call_id": "custom-1", "output": "file1\nfile2"},
                ts="2026-05-20T00:00:02.000Z",
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "exec"
        assert tc["call_id"] == "custom-1"
        assert tc["args"] == 'await tools.exec_command({cmd: "ls"});'
        assert tc["output"] == "file1\nfile2"
        assert tc["end_ts"] > tc["start_ts"]

    def test_custom_tool_call_without_name_falls_back(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "custom_tool_call", "call_id": "c9"}, ts="2026-05-20T00:00:01.000Z"),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "custom_tool_call"
        assert tc["args"] == ""
        assert tc["output"] == ""

    def test_custom_and_function_calls_do_not_cross_pair(self, tmp_path):
        """Same call_id on both channels must stay in its own bucket."""
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "dup"},
                ts="2026-05-20T00:00:01.000Z",
            ),
            _resp(
                {"type": "custom_tool_call", "name": "exec", "input": "code", "call_id": "dup"},
                ts="2026-05-20T00:00:02.000Z",
            ),
            _resp(
                {"type": "custom_tool_call_output", "call_id": "dup", "output": "from-custom"},
                ts="2026-05-20T00:00:03.000Z",
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 2
        by_tool = {tc["tool"]: tc for tc in turn["tool_calls"]}
        assert by_tool["exec"]["output"] == "from-custom"
        assert by_tool["exec_command"]["output"] == ""  # function channel untouched

    def test_web_search_call_pairs_with_preceding_end(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "web_search_end", "call_id": "ws_1", "query": "weather"}),
            _resp({"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "weather"}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "web_search"
        assert tc["call_id"] == "ws_1"
        assert tc["args"] == "weather"
        assert tc["output"] == "completed"

    def test_web_search_open_page_action(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "web_search_end",
                    "call_id": "ws_2",
                    "action": {"type": "open_page", "url": "https://example.com"},
                }
            ),
            _resp(
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "open_page", "url": "https://example.com"},
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["tool_calls"][0]["tool"] == "open_page"
        assert turn["tool_calls"][0]["args"] == "https://example.com"

    def test_stops_at_next_task_started(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "function_call", "name": "x", "arguments": "", "call_id": "c1"}),
            _evt({"type": "task_started", "turn_id": "t2"}),
            _resp({"type": "function_call", "name": "y", "arguments": "", "call_id": "c2"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        assert turn["tool_calls"][0]["tool"] == "x"

    def test_trace_count_counts_task_started_events(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
            _evt({"type": "task_started", "turn_id": "t2"}),
            _evt({"type": "task_complete", "turn_id": "t2"}),
        )
        # trace_count for t1 is 1; for t2 is 2.
        assert _extract_turn_from_rollout(path, "t1")["trace_count"] == 1
        assert _extract_turn_from_rollout(path, "t2")["trace_count"] == 2

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps(_evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}))
            + "\n"
            + "{also broken\n"
            + json.dumps(_evt({"type": "user_message", "message": "hi"}))
            + "\n"
            + json.dumps(_evt({"type": "task_complete", "turn_id": "t1"}))
            + "\n"
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["user_prompt"] == "hi"

    def test_code_mode_items_become_children_of_their_exec(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _exec_call("e1", 1),
            _item({"type": "Reasoning", "id": "r1"}, 1.05, 1.05),
            _item(_command("exec-cmd"), 1.1, 1.2),
            _exec_output("e1", 1.3),
            _exec_call("e2", 2),
            _item(_file_change("exec-patch"), 2.1, 2.2),
            _item(_mcp_call("exec-mcp"), 2.3, 2.4),
            _item({"type": "ContextCompaction", "id": "k1"}, 2.45, 2.45),
            _exec_output("e2", 2.5),
            _item({"type": "AgentMessage", "id": "m1"}, 3, 3),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert [tc["tool"] for tc in turn["tool_calls"]] == ["exec", "exec"]
        first, second = turn["tool_calls"]
        assert [c["item"]["id"] for c in first["children"]] == ["exec-cmd"]
        assert [c["item"]["id"] for c in second["children"]] == ["exec-patch", "exec-mcp"]
        assert first["children"][0]["start_ts"] == _ms(1.1)
        assert first["children"][0]["end_ts"] == _ms(1.2)

    def test_item_of_a_yielded_exec_stays_with_the_exec_that_started_it(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _exec_call("e1", 1),
            _exec_output("e1", 1.5, output="Script running with cell ID 1"),
            _exec_call("e2", 3),
            _item(_command("exec-slow"), 1.1, 3.5),
            _exec_output("e2", 4),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        first, second = _extract_turn_from_rollout(path, "t1")["tool_calls"]
        assert [c["item"]["id"] for c in first["children"]] == ["exec-slow"]
        assert "children" not in second

    def test_item_started_during_a_wait_belongs_to_the_wait(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _exec_call("e1", 1),
            _exec_output("e1", 1.5, output="Script running with cell ID 1"),
            _resp(
                {"type": "function_call", "name": "wait", "arguments": '{"cell_id":"1"}', "call_id": "w1"},
                ts=_at(2),
            ),
            _item(_command("exec-later"), 2.1, 2.4),
            _resp({"type": "function_call_output", "call_id": "w1", "output": "Script completed"}, ts=_at(2.5)),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        exec_entry, wait_entry = _extract_turn_from_rollout(path, "t1")["tool_calls"]
        assert "children" not in exec_entry
        assert [c["item"]["id"] for c in wait_entry["children"]] == ["exec-later"]

    def test_items_already_owned_by_a_direct_call_are_not_given_to_exec(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _exec_call("e1", 1),
            _exec_output("e1", 1.2),
            _resp({"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c1"}, ts=_at(2)),
            _item(_command("c1"), 2.1, 2.2),
            _resp({"type": "function_call_output", "call_id": "c1", "output": "ok"}, ts=_at(2.3)),
            _item(_web_search("ws_1"), 2.4, 2.5),
            _resp(
                {
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {"type": "search", "query": "retry policy"},
                },
                ts=_at(2.5),
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert [tc["tool"] for tc in turn["tool_calls"]] == ["exec", "exec_command", "web_search"]
        assert all("children" not in tc for tc in turn["tool_calls"])

    def test_items_of_another_turn_are_ignored(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _exec_call("e1", 1),
            _item(_command("exec-old"), 1.1, 1.2, turn_id="t0"),
            _exec_output("e1", 1.3),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        (entry,) = _extract_turn_from_rollout(path, "t1")["tool_calls"]
        assert "children" not in entry

    def test_non_code_mode_rollout_with_items_extracts_the_same_tool_calls(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
                ts=_at(1),
            ),
            _item(_command("c1"), 1.1, 1.2),
            _resp({"type": "function_call_output", "call_id": "c1", "output": "a.txt"}, ts=_at(1.3)),
            _resp(
                {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "apply_patch",
                    "call_id": "p1",
                    "input": "*** Begin Patch",
                },
                ts=_at(2),
            ),
            _item(_file_change("p1"), 2.1, 2.2),
            _resp({"type": "custom_tool_call_output", "call_id": "p1", "output": "Success"}, ts=_at(2.3)),
            _item(_web_search("ws_1"), 3, 3.5),
            _resp(
                {
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {"type": "search", "query": "retry policy"},
                },
                ts=_at(3.5),
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["tool_calls"] == [
            {
                "tool": "exec_command",
                "args": '{"cmd":"ls"}',
                "output": "a.txt",
                "call_id": "c1",
                "start_ts": _ms(1),
                "end_ts": _ms(1.3),
                "decision": None,
            },
            {
                "tool": "apply_patch",
                "args": "*** Begin Patch",
                "output": "Success",
                "call_id": "p1",
                "start_ts": _ms(2),
                "end_ts": _ms(2.3),
                "decision": None,
            },
            {
                "tool": "web_search",
                "args": "retry policy",
                "output": "completed",
                "call_id": "",
                "start_ts": _ms(3.5),
                "end_ts": _ms(3.5),
                "decision": None,
            },
        ]


# ---------------------------------------------------------------------------
# _build_and_send_spans
# ---------------------------------------------------------------------------


class TestBuildAndSendSpans:

    def _send_capture(self):
        sent = []
        return sent, mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        )

    def test_multi_span_with_one_tool(self, monkeypatch):
        monkeypatch.setenv("ATATUS_PROJECT_NAME", "codex")
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 2000,
            "duration_ms": 1000,
            "user_prompt": "hi",
            "assistant_output": "hello",
            "model": "gpt-5.5",
            "effort": "high",
            "cwd": "/x/workspace",
            "permission_mode": "on-request",
            "sandbox_mode": "workspace-write",
            "model_provider": "openai",
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cached_input_tokens": 4,
                "cache_write_input_tokens": 2,
                "reasoning_output_tokens": 1,
                "model": "gpt-5.5",
            },
            "tool_calls": [
                {
                    "tool": "exec_command",
                    "args": '{"cmd":"ls"}',
                    "output": "ok",
                    "call_id": "c1",
                    "start_ts": 1100,
                    "end_ts": 1200,
                    "decision": None,
                }
            ],
        }

        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2  # parent + 1 child

        parent_attrs = _attrs_of_span(spans[0])
        assert parent_attrs["input.value"]["stringValue"] == "hi"
        assert parent_attrs["output.value"]["stringValue"] == "hello"
        assert parent_attrs["llm.model_name"]["stringValue"] == "gpt-5.5"
        assert parent_attrs["llm.reasoning_effort"]["stringValue"] == "high"
        assert parent_attrs["codex.approval_mode"]["stringValue"] == "on-request"
        assert parent_attrs["codex.sandbox_mode"]["stringValue"] == "workspace-write"
        assert parent_attrs["codex.cwd"]["stringValue"] == "/x/workspace"
        assert parent_attrs["codex.workspace"]["stringValue"] == "workspace"
        assert parent_attrs["llm.token_count.total"]["intValue"] == 15
        assert parent_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 4
        assert parent_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 2
        assert parent_attrs["llm.token_count.completion_details.reasoning"]["intValue"] == 1
        assert parent_attrs["llm.system"]["stringValue"] == "openai"
        assert parent_attrs["llm.provider"]["stringValue"] == "openai"
        assert parent_attrs["llm.input_messages.0.message.role"]["stringValue"] == "user"
        assert parent_attrs["llm.input_messages.0.message.content"]["stringValue"] == "hi"
        assert parent_attrs["llm.output_messages.0.message.role"]["stringValue"] == "assistant"
        assert parent_attrs["llm.output_messages.0.message.content"]["stringValue"] == "hello"

        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["tool.name"]["stringValue"] == "exec_command"
        assert child_attrs["tool.id"]["stringValue"] == "c1"
        assert child_attrs["codex.tool.call_id"]["stringValue"] == "c1"
        assert child_attrs["codex.cwd"]["stringValue"] == "/x/workspace"
        assert child_attrs["codex.workspace"]["stringValue"] == "workspace"
        assert child_attrs["input.value"]["stringValue"] == '{"cmd":"ls"}'
        assert child_attrs["output.value"]["stringValue"] == "ok"
        assert child_attrs["tool.command"]["stringValue"] == "ls"

    def test_tool_content_off_redacts_patch_but_keeps_command(self, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "false")
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 2000,
            "duration_ms": 1000,
            "user_prompt": "hi",
            "assistant_output": "hello",
            "model": "gpt-5.5",
            "tool_calls": [
                {"tool": "apply_patch", "args": "*** Begin Patch\n+SECRET=1\n*** End Patch", "output": "ok"},
                {"tool": "exec_command", "args": '{"cmd":"pytest -q"}', "output": "1 passed"},
                {"tool": "web_search", "args": "retry policy", "output": ""},
                {"tool": "open_page", "args": "https://example.com/docs", "output": ""},
            ],
        }

        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        patch_attrs, exec_attrs, search_attrs, page_attrs = (_attrs_of_span(s) for s in spans[1:5])
        assert "SECRET" not in patch_attrs["input.value"]["stringValue"]
        assert "tool.command" not in patch_attrs
        assert exec_attrs["input.value"]["stringValue"].startswith("<redacted (")
        assert exec_attrs["tool.command"]["stringValue"] == "pytest -q"
        assert search_attrs["input.value"]["stringValue"].startswith("<redacted (")
        assert search_attrs["tool.query"]["stringValue"] == "retry policy"
        assert page_attrs["tool.url"]["stringValue"] == "https://example.com/docs"

    def test_single_span_when_no_tool_calls(self):
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 1500,
            "duration_ms": 500,
            "user_prompt": "hi",
            "assistant_output": "hello",
            "model": "",
            "cwd": "",
            "permission_mode": "",
            "sandbox_mode": "",
            "token_usage": None,
            "tool_calls": [],
        }
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1

    def test_no_response_when_assistant_output_empty(self):
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 1500,
            "duration_ms": 0,
            "user_prompt": "hi",
            "assistant_output": "",
            "model": "",
            "cwd": "",
            "permission_mode": "",
            "sandbox_mode": "",
            "token_usage": None,
            "tool_calls": [],
        }
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)
        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["output.value"]["stringValue"] == "(No response)"

    def _spans_for(self, turn):
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)
        return sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]

    def _code_mode_turn(self):
        return _turn(
            [
                {
                    "tool": "exec",
                    "args": "await tools.exec_command({cmd: 'ls'});",
                    "output": "Script completed",
                    "call_id": "e1",
                    "start_ts": _ms(1),
                    "end_ts": _ms(5),
                    "decision": None,
                    "children": [
                        _child(_command("exec-cmd", cmd="git status", output="clean"), 1.1, 1.2),
                        _child(_file_change("exec-patch"), 1.3, 1.4),
                        _child(_mcp_call("exec-mcp"), 1.5, 1.6),
                        _child(_image_view("exec-img"), 1.7, 1.8),
                        _child(_web_search("exec-search"), 1.9, 2.0),
                        _child(_web_extension("exec-page"), 2.1, 2.2),
                    ],
                },
                {
                    "tool": "wait",
                    "args": '{"cell_id":"1"}',
                    "output": "Script completed",
                    "call_id": "w1",
                    "start_ts": _ms(6),
                    "end_ts": _ms(7),
                    "decision": None,
                },
            ],
            cwd="/x/workspace",
        )

    def test_exec_that_ran_tools_is_a_chain_over_one_tool_span_per_tool(self):
        spans = self._spans_for(self._code_mode_turn())

        llm, exec_span, *children, wait_span = spans
        assert _kind(llm) == "LLM"
        assert _kind(exec_span) == "CHAIN"
        assert exec_span["name"] == "exec"
        assert exec_span["parentSpanId"] == llm["spanId"]
        exec_attrs = _string_attrs(exec_span)
        assert "tool.name" not in exec_attrs
        assert exec_attrs["codex.tool.call_id"] == "e1"

        assert [s["name"] for s in children] == [
            "exec_command",
            "apply_patch",
            "mcp__docs__search",
            "view_image",
            "web_search",
            "open_page",
        ]
        assert all(_kind(s) == "TOOL" for s in children)
        assert all(s["parentSpanId"] == exec_span["spanId"] for s in children)
        assert [_string_attrs(s)["tool.name"] for s in children] == [s["name"] for s in children]
        assert [_string_attrs(s)["tool.id"] for s in children] == [
            "exec-cmd",
            "exec-patch",
            "exec-mcp",
            "exec-img",
            "exec-search",
            "exec-page",
        ]
        assert children[0]["startTimeUnixNano"] == f"{_ms(1.1)}000000"
        assert children[0]["endTimeUnixNano"] == f"{_ms(1.2)}000000"
        assert all(_string_attrs(s)["codex.workspace"] == "workspace" for s in children)

        assert _kind(wait_span) == "TOOL"
        assert _string_attrs(wait_span)["tool.name"] == "wait"
        assert sum(_kind(s) == "TOOL" for s in spans) == 7

    def test_exec_that_ran_no_tools_is_still_a_tool_span(self):
        turn = _turn(
            [
                {
                    "tool": "exec",
                    "args": "const x = 1;",
                    "output": "Script completed",
                    "call_id": "e1",
                    "start_ts": _ms(1),
                    "end_ts": _ms(2),
                    "decision": None,
                }
            ]
        )
        _, exec_span = self._spans_for(turn)
        assert _kind(exec_span) == "TOOL"
        attrs = _string_attrs(exec_span)
        assert attrs["tool.name"] == "exec"
        assert attrs["tool.id"] == "e1"
        assert attrs["input.value"] == "const x = 1;"

    def test_code_mode_tool_bodies_stay_hidden_without_tool_content(self, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "false")
        spans = self._spans_for(self._code_mode_turn())
        command, patch, mcp, image, search, page = (_string_attrs(s) for s in spans[2:8])

        assert command["tool.command"] == "git status"
        assert command["input.value"].startswith("<redacted")
        assert command["output.value"].startswith("<redacted")
        assert patch["tool.file_path"] == "/repo/app.py"
        assert patch["input.value"].startswith("<redacted")
        assert patch["output.value"].startswith("<redacted")
        assert mcp["tool.name"] == "mcp__docs__search"
        assert mcp["input.value"].startswith("<redacted")
        assert mcp["output.value"].startswith("<redacted")
        assert image["tool.file_path"] == "/repo/shot.png"
        assert image["input.value"].startswith("<redacted")
        assert search["tool.query"] == "retry policy"
        assert search["input.value"].startswith("<redacted")
        assert page["tool.url"] == "https://example.com/docs"
        assert page["input.value"].startswith("<redacted")
        assert page["output.value"].startswith("<redacted")

    def test_code_mode_mcp_call_names_its_server_and_tool(self):
        spans = self._spans_for(self._code_mode_turn())
        command, _, mcp, *_ = (_string_attrs(s) for s in spans[2:8])

        assert mcp["mcp.server.name"] == "docs"
        assert mcp["mcp.tool.name"] == "search"
        assert "mcp.server.name" not in command

    def test_code_mode_tool_bodies_are_kept_with_tool_content(self):
        spans = self._spans_for(self._code_mode_turn())
        command, patch, mcp, _, _, page = (_string_attrs(s) for s in spans[2:8])

        assert command["output.value"] == "clean"
        assert "@@ -1 +1 @@" in patch["input.value"]
        assert patch["output.value"].startswith("Success.")
        assert json.loads(mcp["input.value"]) == {"q": "retry policy"}
        assert "found 2 pages" in mcp["output.value"]
        assert "https://example.com/docs" in page["output.value"]

    def test_code_mode_details_off_hides_commands_paths_and_mcp_names(self, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        spans = self._spans_for(self._code_mode_turn())
        command, patch, mcp, image, search, page = (_string_attrs(s) for s in spans[2:8])

        assert command["tool.command"].startswith("<redacted")
        assert command["input.value"] == "git status"
        assert patch["tool.file_path"].startswith("<redacted")
        assert mcp["tool.name"] == "mcp"
        assert spans[4]["name"] == "mcp"
        assert "mcp.server.name" not in mcp
        assert "mcp.tool.name" not in mcp
        assert image["tool.file_path"].startswith("<redacted")
        assert search["tool.query"].startswith("<redacted")
        assert page["tool.url"].startswith("<redacted")

    def test_every_span_carries_the_same_user_identity(self, _identity):
        spans = self._spans_for(self._code_mode_turn())
        assert len(spans) == 9
        for span in spans:
            attrs = _attrs_of_span(span)
            assert attrs["user.id"]["stringValue"] == "user-1"
            assert attrs["user.login_id"]["stringValue"] == "dev@example.com"

    def test_unknown_identity_adds_no_user_attributes(self, _no_identity):
        for span in self._spans_for(self._code_mode_turn()):
            attrs = _attrs_of_span(span)
            assert "user.id" not in attrs
            assert "user.login_id" not in attrs


def _selected_skill(name, kinds=True):
    text = f"<skill>\n<name>{name}</name>\n<path>/repo/.agents/skills/{name}/SKILL.md</path>\nskill body\n</skill>"
    payload = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    if kinds:
        payload["internal_chat_message_metadata_passthrough"] = {
            "content_item_kinds": ["skills.selected_skill_instructions"],
            "content_item_metadata": [{"provenance": {"type": "skills"}}],
            "turn_id": "t1",
        }
    return _resp(payload)


def _rollout_turn(tmp_path, *records):
    path = _write_rollout(
        tmp_path,
        "s1",
        _evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}),
        *records,
        _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "done", "completed_at": 1010}),
    )
    return _extract_turn_from_rollout(path, "t1")


class TestUserInvokedSkills:
    def test_a_selected_skill_is_read_off_its_injected_message(self, tmp_path):
        turn = _rollout_turn(
            tmp_path,
            _evt({"type": "user_message", "message": "please use $release-notes"}),
            _selected_skill("release-notes"),
        )
        assert turn["invoked_skills"] == ["release-notes"]
        assert turn["user_prompt"] == "please use $release-notes"

    def test_an_injected_skill_is_never_taken_for_the_prompt(self, tmp_path):
        typed = _resp(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "please use $release-notes"}],
            }
        )
        turn = _rollout_turn(tmp_path, typed, _selected_skill("release-notes"))
        assert turn["invoked_skills"] == ["release-notes"]
        assert turn["user_prompt"] == "please use $release-notes"

    def test_an_older_build_without_content_kinds_is_read_by_its_marker(self, tmp_path):
        turn = _rollout_turn(tmp_path, _selected_skill("release-notes", kinds=False))
        assert turn["invoked_skills"] == ["release-notes"]

    def test_several_skills_keep_their_order(self, tmp_path):
        turn = _rollout_turn(tmp_path, _selected_skill("release-notes"), _selected_skill("changelog"))
        assert turn["invoked_skills"] == ["release-notes", "changelog"]

    def test_a_typed_marker_is_not_an_invocation(self, tmp_path):
        typed = _resp(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<skill>\n<name>release-notes</name>\n<path>x</path>"}],
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
            }
        )
        assert _rollout_turn(tmp_path, typed)["invoked_skills"] == []

    def test_the_skill_catalog_is_not_an_invocation(self, tmp_path):
        catalog = _resp(
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<skills_instructions>\n<skill>\n<name>release-notes</name>\n<path>x</path>\n"
                        "</skill>\n</skills_instructions>",
                    }
                ],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["host_skills.instructions"],
                    "content_item_metadata": [{"provenance": {"type": "skills"}}],
                },
            }
        )
        assert _rollout_turn(tmp_path, catalog)["invoked_skills"] == []

    def _root(self, turn):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: (sent.append(p), True)[1]
        ):
            _build_and_send_spans("sess-1", "turn-1", turn)
        [root] = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        return {key: value.get("stringValue") for key, value in _attrs_of_span(root).items()}

    def test_the_turn_root_names_the_skill_and_the_user_trigger(self):
        attrs = self._root({**_turn([]), "invoked_skills": ["release-notes"]})
        assert attrs["skill.name"] == "release-notes"
        assert attrs["skill.trigger"] == "user"

    def test_several_skills_are_comma_joined_on_the_root(self):
        attrs = self._root({**_turn([]), "invoked_skills": ["release-notes", "changelog"]})
        assert attrs["skill.name"] == "release-notes,changelog"

    def test_a_turn_without_one_has_no_skill(self):
        attrs = self._root({**_turn([]), "invoked_skills": []})
        assert "skill.name" not in attrs
        assert "skill.trigger" not in attrs

    def test_left_out_without_tool_details(self, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        attrs = self._root({**_turn([]), "invoked_skills": ["release-notes"]})
        assert "skill.name" not in attrs
        assert "skill.trigger" not in attrs


_SKILL_READ = "sed -n '1,240p' .agents/skills/release-notes/SKILL.md"


def _call(tool, args, children=None):
    entry = {
        "tool": tool,
        "args": args,
        "output": "ok",
        "call_id": "c1",
        "start_ts": _ms(1),
        "end_ts": _ms(5),
        "decision": None,
    }
    if children:
        entry["children"] = children
    return entry


class TestSkillFileReads:
    def _tools(self, *calls):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: (sent.append(p), True)[1]
        ):
            _build_and_send_spans("sess-1", "turn-1", _turn(list(calls)))
        return [_string_attrs(s) for s in sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][1:]]

    @pytest.mark.parametrize(
        "tool, args",
        [
            ("exec_command", json.dumps({"cmd": _SKILL_READ, "workdir": "/repo", "yield_time_ms": 10000})),
            ("shell", json.dumps({"command": ["bash", "-lc", "cat .agents/skills/release-notes/SKILL.md"]})),
            ("shell_command", json.dumps({"command": "head -80 ~/.codex/skills/release-notes/SKILL.md"})),
            ("exec", 'const r = await tools.exec_command({cmd:"cat .agents/skills/release-notes/SKILL.md"});'),
            ("exec", f"const r = await tools.exec_command({json.dumps({'cmd': _SKILL_READ, 'workdir': '/repo'})});"),
            ("exec", "await tools.exec_command({cmd: 'python3 .agents/skills/release-notes/scripts/draft.py'});"),
            (
                "exec",
                "const r = await Promise.allSettled([\n"
                '  tools.exec_command({"cmd":"git status","workdir":"/repo"}),\n'
                f"  tools.exec_command({json.dumps({'cmd': _SKILL_READ})}),\n"
                "]);",
            ),
            ("exec", 'await tools.exec_command({"cmd":"cat \\".agents/skills/release-notes/SKILL.md\\""});'),
        ],
    )
    def test_a_shell_read_of_a_skill_file_is_an_implicit_activation(self, tool, args):
        [attrs] = self._tools(_call(tool, args))
        assert attrs["tool.skill_name"] == "release-notes"
        assert attrs["skill.trigger"] == "implicit"

    def test_an_exec_that_ran_tools_marks_the_command_not_the_wrapper(self):
        script = f"await tools.exec_command({json.dumps({'cmd': _SKILL_READ})});"
        children = [_child(_command("exec-cmd", cmd=_SKILL_READ), 1.1, 1.2), _child(_command("exec-ls"), 1.3, 1.4)]
        wrapper, read, other = self._tools(_call("exec", script, children))
        assert "tool.skill_name" not in wrapper
        assert read["tool.skill_name"] == "release-notes"
        assert read["skill.trigger"] == "implicit"
        assert "tool.skill_name" not in other

    @pytest.mark.parametrize(
        "tool, args",
        [
            ("exec_command", json.dumps({"cmd": "grep -n name .agents/skills/release-notes/SKILL.md"})),
            ("exec_command", json.dumps({"cmd": "cat .agents/skills/release-notes/references/style.md"})),
            ("apply_patch", "*** Update File: .agents/skills/release-notes/SKILL.md"),
            ("view_image", json.dumps({"path": ".agents/skills/release-notes/SKILL.md"})),
            ("exec", "await tools.exec_command({cmd: 'ls .agents/skills/release-notes'});"),
            ("exec", "const path = '.agents/skills/release-notes/SKILL.md';"),
        ],
    )
    def test_anything_else_is_not_an_activation(self, tool, args):
        [attrs] = self._tools(_call(tool, args))
        assert "tool.skill_name" not in attrs
        assert "skill.trigger" not in attrs

    def test_left_out_without_tool_details(self, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        script = f"await tools.exec_command({json.dumps({'cmd': _SKILL_READ})});"
        spans = self._tools(
            _call("exec_command", json.dumps({"cmd": _SKILL_READ})),
            _call("exec", script),
            _call("exec", script, [_child(_command("exec-cmd", cmd=_SKILL_READ), 1.1, 1.2)]),
        )
        assert not any("tool.skill_name" in attrs or "skill.trigger" in attrs for attrs in spans)


# ---------------------------------------------------------------------------
# _handle_notify end-to-end
# ---------------------------------------------------------------------------


class TestHandleNotify:

    def test_ignores_non_agent_turn_complete(self):
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
            _handle_notify({"type": "session-start"})
        send.assert_not_called()

    def test_falls_back_to_single_span_when_no_rollout(self, _isolate_sessions_root):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "no-rollout-yet",
                    "turn-id": "t1",
                    "input-messages": [{"role": "user", "content": "hi"}],
                    "last-assistant-message": "hello",
                }
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["output.value"]["stringValue"] == "hello"

    def test_extracts_from_rollout_and_ships_multi_span(self, _isolate_sessions_root):
        _write_rollout(
            _isolate_sessions_root,
            "sess-real",
            _evt({"type": "task_started", "turn_id": "turn-1", "started_at": 1000}),
            _evt({"type": "user_message", "message": "do something"}),
            _resp({"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"}),
            _resp({"type": "function_call_output", "call_id": "c1", "output": "file"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}},
                }
            ),
            _evt({"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done", "completed_at": 1010}),
        )

        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify({"type": "agent-turn-complete", "thread-id": "sess-real", "turn-id": "turn-1"})

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2  # parent + 1 tool child
        parent_attrs = _attrs_of_span(spans[0])
        assert parent_attrs["input.value"]["stringValue"] == "do something"
        assert parent_attrs["output.value"]["stringValue"] == "done"
        assert parent_attrs["llm.token_count.total"]["intValue"] == 6
        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["tool.name"]["stringValue"] == "exec_command"

    def test_code_mode_turn_reports_each_real_tool_once(self, _isolate_sessions_root):
        _write_rollout(
            _isolate_sessions_root,
            "sess-code",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": _T0 // 1000}),
            _exec_call("e1", 1),
            _item(_command("exec-cmd"), 1.1, 1.2),
            _item(_mcp_call("exec-mcp", server="tracker", tool="get_issue"), 1.3, 1.4),
            _exec_output("e1", 1.5),
            _exec_call("e2", 2),
            _exec_output("e2", 2.5),
            _exec_call("e3", 3),
            _item(_file_change("exec-patch"), 3.1, 3.2),
            _exec_output("e3", 3.3),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"}),
        )

        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify({"type": "agent-turn-complete", "thread-id": "sess-code", "turn-id": "t1"})

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert [(s["name"], _kind(s)) for s in spans] == [
            ("Turn 1", "LLM"),
            ("exec", "CHAIN"),
            ("exec_command", "TOOL"),
            ("mcp__tracker__get_issue", "TOOL"),
            ("exec", "TOOL"),
            ("exec", "CHAIN"),
            ("apply_patch", "TOOL"),
        ]

    def test_non_code_mode_turn_ships_the_same_spans_when_items_are_present(self, _isolate_sessions_root, _no_identity):
        _write_rollout(
            _isolate_sessions_root,
            "sess-old",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": _T0 // 1000}),
            _resp(
                {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
                ts=_at(1),
            ),
            _item(_command("c1"), 1.1, 1.2),
            _resp({"type": "function_call_output", "call_id": "c1", "output": "a.txt"}, ts=_at(1.3)),
            _resp(
                {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "apply_patch",
                    "call_id": "p1",
                    "input": "*** Begin Patch",
                },
                ts=_at(2),
            ),
            _item(_file_change("p1"), 2.1, 2.2),
            _resp({"type": "custom_tool_call_output", "call_id": "p1", "output": "Success"}, ts=_at(2.3)),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"}),
        )

        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify({"type": "agent-turn-complete", "thread-id": "sess-old", "turn-id": "t1"})

        llm, command, patch = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert (command["name"], command["kind"], command["parentSpanId"]) == ("exec_command", 1, llm["spanId"])
        assert (patch["name"], patch["kind"], patch["parentSpanId"]) == ("apply_patch", 1, llm["spanId"])
        assert (command["startTimeUnixNano"], command["endTimeUnixNano"]) == (f"{_ms(1)}000000", f"{_ms(1.3)}000000")
        assert _string_attrs(command) == {
            "openinference.span.kind": "TOOL",
            "tool.name": "exec_command",
            "input.value": '{"cmd":"ls"}',
            "output.value": "a.txt",
            "session.id": "sess-old",
            "tool.id": "c1",
            "codex.tool.call_id": "c1",
            "tool.command": "ls",
        }
        assert _string_attrs(patch) == {
            "openinference.span.kind": "TOOL",
            "tool.name": "apply_patch",
            "input.value": "*** Begin Patch",
            "output.value": "Success",
            "session.id": "sess-old",
            "tool.id": "p1",
            "codex.tool.call_id": "p1",
        }


# ---------------------------------------------------------------------------
# _handle_session_end
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tracing.codex.hooks.adapter.STATE_DIR", tmp_path / "state")

    def _session_span(self):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_session_end({"session_id": "sess-1"})
        (span,) = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        return span

    def test_session_span_carries_user_identity(self, _identity):
        span = self._session_span()
        attrs = _string_attrs(span)
        assert span["name"] == "Session"
        assert attrs["session.id"] == "sess-1"
        assert attrs["user.id"] == "user-1"
        assert attrs["user.login_id"] == "dev@example.com"

    def test_session_span_without_identity_has_no_user_attributes(self, _no_identity):
        attrs = _attrs_of_span(self._session_span())
        assert "user.id" not in attrs
        assert "user.login_id" not in attrs


# ---------------------------------------------------------------------------
# Notify CLI entry-point
# ---------------------------------------------------------------------------


class TestNotifyEntryPoint:

    def test_tracing_disabled_returns_early(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        with mock.patch.object(sys, "argv", ["hook", "{}"]):
            with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
                notify()
        send.assert_not_called()

    def test_missing_argv_defaults_to_empty(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch.object(sys, "argv", ["hook"]):
            with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
                notify()
        send.assert_not_called()

    def test_malformed_json_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch.object(sys, "argv", ["hook", "not json"]):
            notify()  # caught internally; no raise


# ---------------------------------------------------------------------------
# Legacy single-span fallback
# ---------------------------------------------------------------------------


class TestSendLegacySingleSpan:

    def test_emits_one_span_with_fallback_marker(self):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _send_legacy_single_span(
                "sess-x",
                "turn-x",
                {"last-assistant-message": "hi", "input-messages": [{"role": "user", "content": "yo"}]},
            )
        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["input.value"]["stringValue"] == "yo"
        assert attrs["output.value"]["stringValue"] == "hi"
        assert attrs["llm.system"]["stringValue"] == "codex"
        assert "llm.provider" not in attrs
        assert attrs["llm.input_messages.0.message.content"]["stringValue"] == "yo"
        assert attrs["llm.output_messages.0.message.content"]["stringValue"] == "hi"


class TestLlmIdentity:

    def test_model_family_match_wins_over_provider(self):
        assert _llm_identity("claude-sonnet-5", "some-custom-provider") == ("anthropic", "some-custom-provider")

    def test_o_series_model_id_is_openai(self):
        assert _llm_identity("o3-mini", None) == ("openai", None)
        assert _llm_identity("openrouter/o1-preview", "openrouter") == ("openai", "openrouter")

    def test_no_family_match_falls_back_to_openai_or_azure_provider(self):
        assert _llm_identity("some-unknown-model", "azure") == ("openai", "azure")

    def test_absent_provider_defaults_to_openai(self):
        assert _llm_identity("some-unknown-model", None) == ("openai", None)

    def test_unknown_provider_is_used_as_is_never_fabricated(self):
        assert _llm_identity("some-unknown-model", "together") == ("together", "together")
