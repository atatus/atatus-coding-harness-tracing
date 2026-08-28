#!/usr/bin/env python3
"""Tests for tracing.copilot.hooks.handlers — Copilot hook handlers.

Tests cover response printing, session/prompt/tool/stop handlers,
and all CLI entry points.
"""

import io
import json
import sys
from unittest import mock

import pytest

from core.common import StateManager
from tracing.copilot.hooks.handlers import (
    _ensure_turn,
    _handle_post_tool_use,
    _handle_post_tool_use_failure,
    _handle_pre_tool_use,
    _handle_session_start,
    _handle_subagent_stop,
    _handle_user_prompt_submitted,
    _print_response,
    _read_stdin,
    _tool_key,
    post_tool_use,
    post_tool_use_failure,
    pre_tool_use,
    session_start,
    stop,
    subagent_stop,
    user_prompt_submitted,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_span_attrs(span_payload):
    """Extract attributes dict from OTLP span payload."""
    span = span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {a["key"]: a["value"] for a in span["attributes"]}


def _get_span(span_payload):
    """Extract span object from OTLP span payload."""
    return span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _is_hex_id(value, length):
    """True when *value* is a wire-valid OTLP id of *length* hex characters."""
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    """Create a StateManager with a temp state file, pre-initialized."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "test-session-copilot")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    return sm


@pytest.fixture
def mock_resolve(state):
    """Mock resolve_session to return the test state fixture."""
    with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    """Mock ensure_session_initialized."""
    with mock.patch("tracing.copilot.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all payloads sent."""
    sent = []
    with mock.patch("tracing.copilot.hooks.handlers.send_span", side_effect=lambda s: sent.append(s)):
        yield sent


@pytest.fixture
def transcript_file(tmp_path):
    """Write a Copilot events.jsonl transcript to a temp file and return its path."""
    events = [
        {"type": "session.start", "data": {"copilotVersion": "1.0.40"}},
        {"type": "user.message", "data": {"content": "fix the bug"}},
        {"type": "tool.execution_start", "data": {"toolName": "read"}},
        {
            "type": "assistant.message",
            "data": {"model": "gpt-5-mini", "content": "I found the issue.", "outputTokens": 50},
        },
    ]
    tf = tmp_path / "events.jsonl"
    tf.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return str(tf)


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:
    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin("test") == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin("test") == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"key": "val"}')):
            assert _read_stdin("test") == {"key": "val"}


# ---------------------------------------------------------------------------
# _print_response tests
# ---------------------------------------------------------------------------


class TestPrintResponse:
    """A tracing hook must have no opinion about the session it is watching."""

    def test_prints_empty_object(self, capsys):
        _print_response()
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {}

    def test_never_decides_permissions(self, capsys):
        """Copilot reads `permissionDecision` here.

        Emitting `allow` auto-approved every tool call and bypassed the user's
        own permission settings — a tracing hook must never do that.
        """
        _print_response()
        out = capsys.readouterr().out
        assert "permissionDecision" not in out
        assert "hookSpecificOutput" not in out

    def test_does_not_steer_the_turn(self, capsys):
        """`continue` is a control signal, not telemetry."""
        _print_response()
        payload = json.loads(capsys.readouterr().out.strip())
        assert "continue" not in payload

    def test_takes_no_event_argument(self):
        """The response is identical for every event, so there is nothing to pass."""
        with pytest.raises(TypeError):
            _print_response("PreToolUse")


# ---------------------------------------------------------------------------
# session_start tests
# ---------------------------------------------------------------------------


class TestSessionStart:
    def test_initializes_state_from_snake_case_payload(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        monkeypatch.delenv("ATATUS_PROJECT_NAME", raising=False)

        payload = {
            "cwd": "/some/repo",
            "hook_event_name": "SessionStart",
            "session_id": "sess-123",
            "initial_prompt": "kick off",
            "source": "new",
            "timestamp": "2026-05-04T00:00:00Z",
        }
        _handle_session_start(payload)

        from tracing.copilot.hooks.adapter import resolve_session

        state = resolve_session(payload)
        assert state.get("session_id") == "sess-123"
        assert state.get("trace_count") == "0"

    def test_initializes_state_from_camel_case_payload(self, tmp_path, monkeypatch):
        """Copilot's native payloads are camelCase."""
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        monkeypatch.delenv("ATATUS_PROJECT_NAME", raising=False)

        payload = {
            "cwd": "/some/repo",
            "hookEventName": "SessionStart",
            "sessionId": "sess-camel",
            "initialPrompt": "kick off",
            "source": "new",
        }
        _handle_session_start(payload)

        from tracing.copilot.hooks.adapter import resolve_session

        state = resolve_session(payload)
        assert state.get("session_id") == "sess-camel"


# ---------------------------------------------------------------------------
# user_prompt_submitted tests
# ---------------------------------------------------------------------------


class TestUserPromptSubmitted:
    def test_creates_trace_root(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)

        payload = {
            "cwd": "/some/repo",
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-abc",
            "prompt": "what is the capital of France?",
            "timestamp": "2026-05-04T00:00:00Z",
        }
        _handle_user_prompt_submitted(payload)

        from tracing.copilot.hooks.adapter import resolve_session

        state = resolve_session(payload)
        assert _is_hex_id(state.get("current_trace_id"), 32)
        assert _is_hex_id(state.get("current_trace_span_id"), 16)
        assert state.get("current_trace_prompt") == "what is the capital of France?"
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "0"

    def test_returns_early_without_session_id(self, mock_resolve, mock_ensure, state, captured_spans):
        """Returns early when session_id is None."""
        state.delete("session_id")
        inp = {"cwd": "/tmp/project", "hook_event_name": "UserPromptSubmit", "prompt": "hello"}
        _handle_user_prompt_submitted(inp)
        assert state.get("current_trace_id") is None


# ---------------------------------------------------------------------------
# _tool_key tests
# ---------------------------------------------------------------------------


class TestToolKey:
    """Copilot's payload carries no tool-call id, so pairing falls through."""

    def test_prefers_tool_use_id(self):
        assert _tool_key({"tool_use_id": "tu-1", "tool_call_id": "tc-1", "tool_name": "bash"}) == "tu-1"

    def test_falls_back_to_tool_call_id(self):
        assert _tool_key({"tool_call_id": "tc-1", "tool_name": "bash"}) == "tc-1"

    def test_falls_back_to_tool_name(self):
        assert _tool_key({"tool_name": "bash"}) == "bash"

    def test_falls_back_to_literal_tool(self):
        """A payload with nothing identifying still needs a stable key."""
        assert _tool_key({}) == "tool"

    @pytest.mark.parametrize("key", ["toolUseId", "toolCallId", "toolName"])
    def test_camel_case_spellings_recognized(self, key):
        assert _tool_key({key: "camel-key"}) == "camel-key"


# ---------------------------------------------------------------------------
# _ensure_turn tests
# ---------------------------------------------------------------------------


class TestEnsureTurn:
    """A span with an empty trace id is rejected outright by the receiver."""

    def test_reuses_an_open_turn(self, state):
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        assert _ensure_turn(state) == ("a" * 32, "b" * 16)

    def test_opens_a_turn_when_none_is_open(self, state):
        trace_id, span_id = _ensure_turn(state)
        assert _is_hex_id(trace_id, 32)
        assert _is_hex_id(span_id, 16)
        assert state.get("current_trace_id") == trace_id
        assert state.get("current_trace_span_id") == span_id
        assert int(state.get("current_trace_start_time")) > 0

    def test_second_call_returns_the_same_turn(self, state):
        first = _ensure_turn(state)
        assert _ensure_turn(state) == first

    @pytest.mark.parametrize(
        "seed",
        [{}, {"current_trace_id": "a" * 32}, {"current_trace_span_id": "b" * 16}],
        ids=["nothing-open", "trace-only", "onlyonlyonlyonly"],
    )
    def test_never_yields_an_empty_id(self, state, seed):
        for key, value in seed.items():
            state.set(key, value)
        trace_id, span_id = _ensure_turn(state)
        assert trace_id and span_id
        assert _is_hex_id(trace_id, 32)
        assert _is_hex_id(span_id, 16)

    def test_preserves_an_existing_start_time(self, state):
        state.set("current_trace_start_time", "1000000")
        _ensure_turn(state)
        assert state.get("current_trace_start_time") == "1000000"


# ---------------------------------------------------------------------------
# pre_tool_use tests
# ---------------------------------------------------------------------------


class TestPreToolUse:
    def test_records_tool_start_by_tool_use_id(self, mock_resolve, state):
        """Records tool_{tool_use_id}_start when tool_use_id is present."""
        inp = {
            "cwd": "/repo",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "tool_use_id": "tool-42",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
        }
        _handle_pre_tool_use(inp)
        val = state.get("tool_tool-42_start")
        assert val is not None
        assert int(val) > 0

    def test_records_tool_start_by_tool_name(self, mock_resolve, state):
        """Falls back to tool_name when tool_use_id is absent."""
        inp = {
            "cwd": "/repo",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
        }
        _handle_pre_tool_use(inp)
        val = state.get("tool_bash_start")
        assert val is not None
        assert int(val) > 0

    def test_records_tool_start_by_camel_case_tool_name(self, mock_resolve, state):
        """Copilot spells it toolName natively."""
        inp = {"cwd": "/repo", "hookEventName": "PreToolUse", "sessionId": "sess-1", "toolName": "bash"}
        _handle_pre_tool_use(inp)
        assert state.get("tool_bash_start") is not None

    def test_missing_tool_id_falls_back_to_literal_key(self, mock_resolve, state):
        """With no id and no name, the literal "tool" key still pairs pre with post."""
        inp = {"cwd": "/repo", "hook_event_name": "PreToolUse", "session_id": "sess-1"}
        _handle_pre_tool_use(inp)
        val = state.get("tool_tool_start")
        assert val is not None
        assert int(val) > 0


# ---------------------------------------------------------------------------
# post_tool_use tests
# ---------------------------------------------------------------------------


class TestPostToolUse:
    def test_emits_bash_tool_span(self, mock_resolve, state, captured_spans):
        """Builds a TOOL span for bash with command enrichment."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "ls -la", "description": "list"},
            "tool_result": {"text_result_for_llm": "out", "result_type": "success"},
        }
        _handle_post_tool_use(inp)
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "bash"
        assert "ls -la" in attrs["input.value"]["stringValue"]
        assert attrs["output.value"]["stringValue"] == "out"
        assert attrs["tool.command"]["stringValue"] == "ls -la"
        assert attrs["tool.description"]["stringValue"] == "ls -la"
        assert attrs["tool.result_type"]["stringValue"] == "success"
        assert attrs["session.id"]["stringValue"] == "test-session-copilot"
        assert _get_span(captured_spans[0])["status"]["code"] == 1

    def test_reads_camel_case_payload(self, mock_resolve, state, captured_spans):
        """Copilot's native payload is camelCase throughout, args included."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "sessionId": "sess-1",
            "hookEventName": "PostToolUse",
            "toolName": "bash",
            "toolArgs": {"command": "git status"},
            "toolResult": {"textResultForLlm": "clean", "resultType": "success"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "bash"
        assert attrs["tool.command"]["stringValue"] == "git status"
        assert attrs["output.value"]["stringValue"] == "clean"
        assert attrs["tool.result_type"]["stringValue"] == "success"

    def test_reads_tool_args_field(self, mock_resolve, state, captured_spans):
        """`toolArgs` is Copilot's name for what other harnesses call tool_input."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "tool_name": "read",
            "tool_args": {"file_path": "/foo.py"},
            "tool_result": {"text_result_for_llm": "content"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.file_path"]["stringValue"] == "/foo.py"

    def test_emits_span_for_unknown_tool(self, mock_resolve, state, captured_spans):
        """Builds a TOOL span for an unrecognized tool name."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "report_intent",
            "tool_input": {"intent": "checking copilot"},
            "result_type": "success",
            "tool_result": {"text_result_for_llm": "ack"},
        }
        _handle_post_tool_use(inp)
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "report_intent"
        assert attrs["output.value"]["stringValue"] == "ack"
        assert attrs["tool.description"]["stringValue"]  # non-empty

    def test_extracts_text_result_for_llm(self, mock_resolve, state, captured_spans):
        """Extracts text_result_for_llm from tool_result."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "read",
            "tool_input": {"file_path": "/foo.py"},
            "tool_result": {"text_result_for_llm": "file contents here"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "file contents here"

    def test_result_type_attribute(self, mock_resolve, state, captured_spans):
        """Sets tool.result_type from tool_result.result_type."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "edit",
            "tool_input": {},
            "tool_result": {"text_result_for_llm": "error", "result_type": "failure"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.result_type"]["stringValue"] == "failure"

    def test_no_result_type_when_empty(self, mock_resolve, state, captured_spans):
        """Does not set tool.result_type when result_type is empty."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "echo hi"},
            "tool_result": {"text_result_for_llm": "hi"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert "tool.result_type" not in attrs

    def test_case_insensitive_bash_enrichment(self, mock_resolve, state, captured_spans):
        """Bash tool enrichment works regardless of tool name casing."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_result": {"text_result_for_llm": "clean"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.command"]["stringValue"] == "git status"
        assert attrs["tool.description"]["stringValue"] == "git status"

    def test_no_session_id_returns_early(self, state, captured_spans):
        """If session_id is None, returns without sending span."""
        state.delete("session_id")
        with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state):
            _handle_post_tool_use({"tool_name": "bash", "tool_input": {"command": "ls"}})
        assert len(captured_spans) == 0

    def test_uses_pre_tool_start_time(self, mock_resolve, state, captured_spans):
        """Timing uses pre_tool_use start time if available in state."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        state.set("tool_read_start", "1000000")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "read",
            "tool_input": {"file_path": "/a.py"},
            "tool_result": {"text_result_for_llm": "content"},
        }
        _handle_post_tool_use(inp)
        span = _get_span(captured_spans[0])
        assert span["startTimeUnixNano"] == "1000000000000"
        assert state.get("tool_read_start") is None

    def test_opens_a_turn_when_none_is_open(self, mock_resolve, state, captured_spans):
        """A tool before any prompt must not ship an empty trace id."""
        assert state.get("current_trace_id") is None
        inp = {
            "session_id": "sess-1",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
            "tool_result": {"text_result_for_llm": "out"},
        }
        _handle_post_tool_use(inp)
        span = _get_span(captured_spans[0])
        assert _is_hex_id(span["traceId"], 32)
        # The lazily-opened turn is persisted, so the Stop span joins this trace.
        assert state.get("current_trace_id") == span["traceId"]
        assert span["parentSpanId"] == state.get("current_trace_span_id")

    def test_grep_tool_enrichment(self, mock_resolve, state, captured_spans):
        """Grep tool sets query, file_path, and description."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "grep",
            "tool_input": {"pattern": "TODO", "path": "/src"},
            "tool_result": {"text_result_for_llm": "matches"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "TODO"
        assert attrs["tool.file_path"]["stringValue"] == "/src"
        assert attrs["tool.description"]["stringValue"].startswith("grep: ")

    def test_webfetch_tool_enrichment(self, mock_resolve, state, captured_spans):
        """WebFetch tool sets url (case-insensitive match)."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.com"},
            "tool_result": {"text_result_for_llm": "page"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.url"]["stringValue"] == "https://example.com"

    def test_read_tool_file_path_enrichment(self, mock_resolve, state, captured_spans):
        """Read tool sets file_path and description."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "Read",
            "tool_input": {"file_path": "/foo/bar.py"},
            "result_type": "success",
            "tool_result": {"text_result_for_llm": "file content"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "Read"
        assert attrs["tool.file_path"]["stringValue"] == "/foo/bar.py"
        assert attrs["output.value"]["stringValue"] == "file content"


# ---------------------------------------------------------------------------
# post_tool_use_failure tests
# ---------------------------------------------------------------------------


class TestPostToolUseFailure:
    """A failed tool call is invisible without its own hook, and leaks state."""

    def test_emits_error_tool_span(self, mock_resolve, state, captured_spans):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "bash",
            "tool_input": {"command": "false"},
            "error": "command exited with status 1",
        }
        _handle_post_tool_use_failure(inp)

        assert len(captured_spans) == 1
        span = _get_span(captured_spans[0])
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "bash"
        assert attrs["output.value"]["stringValue"] == "command exited with status 1"
        assert span["status"]["code"] == 2
        assert span["status"]["message"] == "command exited with status 1"

    def test_clears_the_pre_tool_start_time(self, mock_resolve, state, captured_spans):
        """Left behind, the state file grows one dead key per failed tool."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        state.set("tool_bash_start", "1000000")
        _handle_post_tool_use_failure({"session_id": "sess-1", "tool_name": "bash", "error": "boom"})
        assert state.get("tool_bash_start") is None
        assert _get_span(captured_spans[0])["startTimeUnixNano"] == "1000000000000"

    def test_status_message_is_truncated(self, mock_resolve, state, captured_spans):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        message = "x" * 500
        _handle_post_tool_use_failure({"session_id": "sess-1", "tool_name": "bash", "error": message})
        assert _get_span(captured_spans[0])["status"]["message"] == "x" * 200

    def test_empty_error_still_emits_an_error_span(self, mock_resolve, state, captured_spans):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "parentparentpare")
        _handle_post_tool_use_failure({"session_id": "sess-1", "tool_name": "bash"})
        assert _get_span(captured_spans[0])["status"]["code"] == 2

    def test_opens_a_turn_when_none_is_open(self, mock_resolve, state, captured_spans):
        _handle_post_tool_use_failure({"session_id": "sess-1", "tool_name": "bash", "error": "boom"})
        span = _get_span(captured_spans[0])
        assert _is_hex_id(span["traceId"], 32)

    def test_no_session_id_returns_early(self, state, captured_spans):
        state.delete("session_id")
        with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state):
            _handle_post_tool_use_failure({"tool_name": "bash", "error": "boom"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# stop tests
# ---------------------------------------------------------------------------


class TestHandleStop:
    """Stop closes the turn with a `User Prompt` CHAIN root and an LLM child."""

    def _seed_trace(self, tmp_path, monkeypatch, prompt="hi"):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        from tracing.copilot.hooks.handlers import _handle_user_prompt_submitted

        _handle_user_prompt_submitted(
            {"session_id": "sess-1", "hook_event_name": "UserPromptSubmit", "cwd": "/repo", "prompt": prompt}
        )

    def _capture(self, monkeypatch):
        sent = []
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))
        return sent

    def _write_transcript(self, tmp_path, events):
        tpath = tmp_path / "events.jsonl"
        tpath.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return tpath

    def test_emits_root_then_child(self, tmp_path, monkeypatch):
        """The parent is sent first: a strict backend wants it to exist already."""
        self._seed_trace(tmp_path, monkeypatch, prompt="why?")
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop(
            {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": "/repo", "stop_reason": "end_turn"}
        )

        assert len(sent) == 2
        root, child = _get_span(sent[0]), _get_span(sent[1])
        assert root["name"] == "User Prompt"
        assert child["name"] == "Agent Response"
        assert _get_span_attrs(sent[0])["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert _get_span_attrs(sent[1])["openinference.span.kind"]["stringValue"] == "LLM"
        assert root["traceId"] == child["traceId"]
        assert child["parentSpanId"] == root["spanId"]
        assert "parentSpanId" not in root

    def test_both_spans_carry_session_id(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch)
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop({"session_id": "sess-1", "hook_event_name": "Stop", "cwd": "/repo"})

        for payload in sent:
            assert _get_span_attrs(payload)["session.id"]["stringValue"] == "sess-1"

    def test_model_and_answer_come_from_the_transcript(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch, prompt="why?")
        tpath = self._write_transcript(
            tmp_path,
            [
                {"type": "user.message", "data": {"content": "why?"}},
                {
                    "type": "assistant.message",
                    "data": {"model": "gpt-5-mini", "content": "because of the cache", "outputTokens": 64},
                },
            ],
        )
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop(
            {
                "session_id": "sess-1",
                "hook_event_name": "Stop",
                "cwd": "/repo",
                "stop_reason": "end_turn",
                "transcript_path": str(tpath),
            }
        )

        assert len(sent) == 2
        for payload in sent:
            attrs = _get_span_attrs(payload)
            assert attrs["llm.model_name"]["stringValue"] == "gpt-5-mini"
            assert attrs["input.value"]["stringValue"] == "why?"
            assert attrs["output.value"]["stringValue"] == "because of the cache"

        meta = json.loads(_get_span_attrs(sent[0])["metadata"]["stringValue"])
        assert meta["stop_reason"] == "end_turn"

    def test_reads_camel_case_transcript_path(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch, prompt="why?")
        tpath = self._write_transcript(
            tmp_path, [{"type": "assistant.message", "data": {"model": "gpt-5-mini", "content": "ok"}}]
        )
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop({"sessionId": "sess-1", "hookEventName": "Stop", "transcriptPath": str(tpath)})

        assert _get_span_attrs(sent[1])["llm.model_name"]["stringValue"] == "gpt-5-mini"

    def test_completion_tokens_on_the_llm_span_only(self, tmp_path, monkeypatch):
        """Copilot only reports session-cumulative input tokens, so prompt-side
        counts would have to be invented — they are deliberately omitted."""
        self._seed_trace(tmp_path, monkeypatch)
        tpath = self._write_transcript(
            tmp_path,
            [{"type": "assistant.message", "data": {"model": "gpt-5-mini", "content": "hi", "outputTokens": 64}}],
        )
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop({"session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(tpath)})

        llm_attrs = _get_span_attrs(sent[1])
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 64
        assert "llm.token_count.prompt" not in llm_attrs
        assert "llm.token_count.total" not in llm_attrs
        assert "llm.token_count.completion" not in _get_span_attrs(sent[0])

    def test_no_token_attribute_when_transcript_reports_none(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch)
        tpath = self._write_transcript(
            tmp_path, [{"type": "assistant.message", "data": {"model": "gpt-5-mini", "content": "hi"}}]
        )
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop({"session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(tpath)})

        assert "llm.token_count.completion" not in _get_span_attrs(sent[1])

    def test_emits_spans_without_transcript(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch, prompt="ping")
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop(
            {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": "/repo", "stop_reason": "end_turn"}
        )

        assert len(sent) == 2
        for payload in sent:
            attrs = _get_span_attrs(payload)
            assert attrs["input.value"]["stringValue"] == "ping"
            assert "llm.model_name" not in attrs

    def test_falls_back_to_transcript_prompt(self, tmp_path, monkeypatch):
        """A hook installed mid-turn has no stored prompt; the transcript has one."""
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        tpath = self._write_transcript(tmp_path, [{"type": "user.message", "data": {"content": "from transcript"}}])
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_session_start({"session_id": "sess-1", "hook_event_name": "SessionStart"})
        _handlers._handle_stop({"session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(tpath)})

        assert _get_span_attrs(sent[0])["input.value"]["stringValue"] == "from transcript"

    def test_never_emits_an_empty_trace_id(self, tmp_path, monkeypatch):
        """Stop with no turn open opens one rather than shipping traceId ""."""
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_session_start({"session_id": "sess-1", "hook_event_name": "SessionStart"})
        _handlers._handle_stop({"session_id": "sess-1", "hook_event_name": "Stop"})

        assert len(sent) == 2
        root, child = _get_span(sent[0]), _get_span(sent[1])
        assert _is_hex_id(root["traceId"], 32)
        assert root["traceId"] == child["traceId"]
        assert child["parentSpanId"] == root["spanId"]

    def test_tool_count_in_metadata(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch)
        from tracing.copilot.hooks.adapter import resolve_session

        payload = {"session_id": "sess-1", "hook_event_name": "Stop"}
        state = resolve_session(payload)
        state.set("tool_count", "3")

        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop(payload)

        meta = json.loads(_get_span_attrs(sent[0])["metadata"]["stringValue"])
        assert meta["tool_count"] == 3

    def test_full_transcript_end_to_end(self, tmp_path, monkeypatch, transcript_file):
        """A realistic events.jsonl fills model, answer and completion tokens."""
        self._seed_trace(tmp_path, monkeypatch, prompt="fix the bug")
        sent = self._capture(monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        _handlers._handle_stop({"sessionId": "sess-1", "hookEventName": "Stop", "transcriptPath": transcript_file})

        assert len(sent) == 2
        llm_attrs = _get_span_attrs(sent[1])
        assert llm_attrs["llm.model_name"]["stringValue"] == "gpt-5-mini"
        assert llm_attrs["output.value"]["stringValue"] == "I found the issue."
        assert llm_attrs["input.value"]["stringValue"] == "fix the bug"
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 50

    def test_no_session_id_returns_early(self, state, captured_spans):
        state.delete("session_id")
        with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state):
            from tracing.copilot.hooks.handlers import _handle_stop

            _handle_stop({"hook_event_name": "Stop"})
        assert len(captured_spans) == 0

    def test_clears_trace_state_after_emit(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: None)
        from tracing.copilot.hooks.adapter import resolve_session

        payload = {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": "/repo", "stop_reason": "end_turn"}
        _handlers._handle_stop(payload)

        state = resolve_session(payload)
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_prompt") is None
        assert state.get("current_trace_start_time") is None


# ---------------------------------------------------------------------------
# subagent_stop tests
# ---------------------------------------------------------------------------


class TestHandleSubagentStop:
    def test_emits_chain_span_with_agent_metadata(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        from tracing.copilot.hooks.handlers import _handle_user_prompt_submitted

        _handle_user_prompt_submitted(
            {"session_id": "sess-X", "hook_event_name": "UserPromptSubmit", "cwd": "/repo", "prompt": "p"}
        )

        sent = []
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))

        _handlers._handle_subagent_stop(
            {
                "session_id": "sess-X",
                "hook_event_name": "SubagentStop",
                "cwd": "/repo",
                "agent_id": "ag-7",
                "agent_type": "research",
            }
        )

        assert len(sent) == 1
        attrs = _get_span_attrs(sent[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        meta = json.loads(attrs["metadata"]["stringValue"])
        assert meta["agent_id"] == "ag-7"
        assert meta["agent_type"] == "research"

    def test_parented_to_the_open_turn(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        from tracing.copilot.hooks import handlers as _handlers
        from tracing.copilot.hooks.adapter import resolve_session

        payload = {"session_id": "sess-X", "hook_event_name": "UserPromptSubmit", "cwd": "/repo", "prompt": "p"}
        _handlers._handle_user_prompt_submitted(payload)
        state = resolve_session(payload)

        sent = []
        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))
        _handlers._handle_subagent_stop(
            {"session_id": "sess-X", "hook_event_name": "SubagentStop", "agent_type": "research"}
        )

        span = _get_span(sent[0])
        assert span["traceId"] == state.get("current_trace_id")
        assert span["parentSpanId"] == state.get("current_trace_span_id")

    def test_returns_early_without_an_open_turn(self, mock_resolve, state, captured_spans):
        """A subagent outside a turn has no root to hang from.

        Minting a fresh trace for it produced a single-span orphan with no
        prompt and no answer, so nothing is sent instead.
        """
        assert state.get("current_trace_id") is None
        _handle_subagent_stop(
            {"session_id": "sess-X", "hook_event_name": "SubagentStop", "agent_id": "ag-7", "agent_type": "research"}
        )
        assert len(captured_spans) == 0
        # And it must not leave a half-open turn behind either.
        assert state.get("current_trace_id") is None

    def test_no_session_id_returns_early(self, state, captured_spans):
        state.delete("session_id")
        with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state):
            _handle_subagent_stop({"hook_event_name": "SubagentStop", "agent_id": "ag-7"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# Consecutive prompts open fresh traces
# ---------------------------------------------------------------------------


class TestConsecutivePrompts:
    def test_consecutive_prompts_each_open_fresh_trace(self, mock_resolve, mock_ensure, state, captured_spans):
        """Each user_prompt_submitted opens a fresh trace without sending spans."""
        _handle_user_prompt_submitted({"cwd": "/tmp/project", "prompt": "first"})
        first_trace = state.get("current_trace_id")
        _handle_user_prompt_submitted({"cwd": "/tmp/project", "prompt": "second"})
        second_trace = state.get("current_trace_id")
        assert first_trace != second_trace
        assert state.get("current_trace_prompt") == "second"
        assert state.get("trace_count") == "2"
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_entry_point_catches_exception(self, monkeypatch, capsys):
        """Exception in handler → entry point catches, calls error()."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            session_start()
        captured = capsys.readouterr()
        assert "boom" in captured.err

    def test_malformed_stdin_no_crash(self, monkeypatch):
        """Malformed stdin JSON doesn't crash entry point."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("not json")),
            mock.patch("tracing.copilot.hooks.handlers.resolve_session") as rs,
            mock.patch("tracing.copilot.hooks.handlers.ensure_session_initialized"),
        ):
            session_start()
        rs.assert_called_once_with({})


# ---------------------------------------------------------------------------
# Entry point tests (all 7 CLI wrappers)
# ---------------------------------------------------------------------------

ENTRY_POINTS = [
    ("session_start", session_start, "_handle_session_start", "SessionStart"),
    ("user_prompt_submitted", user_prompt_submitted, "_handle_user_prompt_submitted", "UserPromptSubmit"),
    ("pre_tool_use", pre_tool_use, "_handle_pre_tool_use", "PreToolUse"),
    ("post_tool_use", post_tool_use, "_handle_post_tool_use", "PostToolUse"),
    ("post_tool_use_failure", post_tool_use_failure, "_handle_post_tool_use_failure", "PostToolUseFailure"),
    ("stop", stop, "_handle_stop", "Stop"),
    ("subagent_stop", subagent_stop, "_handle_subagent_stop", "SubagentStop"),
]


class TestEntryPoints:
    def test_one_entry_point_per_hook_event(self):
        from tracing.copilot.constants import HOOK_EVENTS

        assert {event for *_, event in ENTRY_POINTS} == set(HOOK_EVENTS)

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_happy_path_calls_handler(self, name, entry_fn, handler_name, event):
        """Entry point calls the corresponding _handle_* with parsed stdin JSON."""
        input_data = {"session_id": "s1", "hook_event_name": event}
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.copilot.hooks.handlers._print_response"),
        ):
            entry_fn()
        handler_mock.assert_called_once_with(input_data)

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_requirements_not_met_skips_handler(self, name, entry_fn, handler_name, event):
        """When check_requirements returns False, handler is NOT called but response is still printed."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=False),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()
        handler_mock.assert_not_called()
        pr_mock.assert_called_once_with()

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_exception_caught_and_logged(self, name, entry_fn, handler_name, event, capsys):
        """Handler exception is caught; error is logged to stderr, response still printed."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}", side_effect=RuntimeError("test-boom")),
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()  # should not raise
        captured = capsys.readouterr()
        assert "test-boom" in captured.err
        pr_mock.assert_called_once_with()

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_prints_response(self, name, entry_fn, handler_name, event):
        """Entry point always answers the agent."""
        input_data = {"session_id": "s1", "hook_event_name": event}
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}"),
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()
        pr_mock.assert_called_once_with()

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_response_is_neutral_on_exception(self, name, entry_fn, handler_name, event, capsys):
        """A crashing hook must not change what the agent is allowed to do."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}", side_effect=RuntimeError("boom")),
        ):
            entry_fn()
        assert json.loads(capsys.readouterr().out.strip()) == {}

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_response_is_neutral_when_disabled(self, name, entry_fn, handler_name, event, capsys):
        """Tracing off still answers, and still expresses no opinion."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=False),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
        ):
            entry_fn()
        out = capsys.readouterr().out
        assert json.loads(out.strip()) == {}
        assert "permissionDecision" not in out
