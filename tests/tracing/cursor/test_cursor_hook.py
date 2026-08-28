#!/usr/bin/env python3
"""Tests for tracing.cursor.hooks.handlers — the Cursor hook dispatcher and 15 event handlers."""

import io
import json
import sys
from unittest import mock

import pytest

from tracing.cursor.hooks import adapter
from tracing.cursor.hooks import handlers as handlers_mod
from tracing.cursor.hooks.handlers import (
    _dispatch,
    _event_name,
    _is_cursor_ide_hook_payload,
    _jq_str,
    _print_permissive,
    _trace_id_from_event,
    main,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays while tracking calls."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _patch_cursor_state(tmp_path, monkeypatch):
    """Redirect cursor adapter STATE_DIR to temp."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all payloads sent."""
    sent = []
    with mock.patch("tracing.cursor.hooks.handlers.send_span", side_effect=lambda s: sent.append(s)):
        yield sent


# ---------------------------------------------------------------------------
# _print_permissive tests
# ---------------------------------------------------------------------------


class TestPrintPermissive:

    def test_before_event_returns_permission_allow(self):
        """before* events write {"permission": "allow"} to sys.__stdout__."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("beforeSubmitPrompt")
        assert json.loads(buf.getvalue()) == {"permission": "allow"}

    def test_before_shell_event(self):
        """beforeShellExecution also returns permission allow."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("beforeShellExecution")
        assert json.loads(buf.getvalue()) == {"permission": "allow"}

    def test_after_event_returns_continue_true(self):
        """Non-before events write {"continue": true}."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("afterAgentResponse")
        assert json.loads(buf.getvalue()) == {"continue": True}

    def test_stop_event_returns_continue_true(self):
        """stop event writes {"continue": true}."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("stop")
        assert json.loads(buf.getvalue()) == {"continue": True}

    def test_empty_event_returns_continue_true(self):
        """Empty event string writes {"continue": true}."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("")
        assert json.loads(buf.getvalue()) == {"continue": True}


# ---------------------------------------------------------------------------
# _jq_str tests
# ---------------------------------------------------------------------------


class TestJqStr:

    def test_returns_first_matching_key(self):
        d = {"prompt": "hello", "input": "world"}
        assert _jq_str(d, "prompt", "input") == "hello"

    def test_skips_to_second_key(self):
        d = {"input": "world"}
        assert _jq_str(d, "prompt", "input") == "world"

    def test_returns_default_when_no_match(self):
        assert _jq_str({}, "a", "b", default="fallback") == "fallback"

    def test_returns_empty_default(self):
        assert _jq_str({}, "a") == ""

    def test_skips_none_value(self):
        d = {"a": None, "b": "found"}
        assert _jq_str(d, "a", "b") == "found"

    def test_skips_empty_string_value(self):
        d = {"a": "", "b": "found"}
        assert _jq_str(d, "a", "b") == "found"

    def test_converts_non_string_to_str(self):
        d = {"count": 42}
        assert _jq_str(d, "count") == "42"

    def test_all_none_returns_default(self):
        d = {"a": None, "b": None}
        assert _jq_str(d, "a", "b", default="x") == "x"


class TestIdeCliPayloadDetection:

    def test_hook_event_name_implies_ide(self):
        assert _is_cursor_ide_hook_payload({"hook_event_name": "beforeSubmitPrompt"}) is True

    def test_hook_event_name_empty_defaults_ide_unless_cli_present(self):
        assert _is_cursor_ide_hook_payload({"hook_event_name": ""}) is True
        assert _is_cursor_ide_hook_payload({"hook_event_name": "", "hookEventName": "x"}) is False

    def test_hook_event_name_only_cli_key(self):
        assert _is_cursor_ide_hook_payload({"hookEventName": "beforeSubmitPrompt"}) is False

    def test_neither_key_defaults_ide(self):
        assert _is_cursor_ide_hook_payload({"conversation_id": "c"}) is True


# ---------------------------------------------------------------------------
# _dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:

    def test_routes_to_correct_handler(self, monkeypatch):
        """Known event routes to correct handler function."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_before_submit_prompt") as h,
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {"conversation_id": "c1", "generation_id": "g1"},
            )
            h.assert_called_once()

    def test_routes_after_agent_response(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_after_agent_response") as h,
        ):
            _dispatch("afterAgentResponse", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_called_once()

    def test_routes_stop(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_stop") as h,
        ):
            _dispatch("stop", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_called_once()

    def test_unknown_event_logs_warning(self, monkeypatch):
        """Unknown event logs warning, no crash."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.log") as log_mock,
        ):
            _dispatch("unknownEvent", {"conversation_id": "c1", "generation_id": "g1"})
            log_mock.assert_called_once()
            assert "Unknown" in log_mock.call_args[0][0]

    def test_tracing_disabled_returns_early(self, monkeypatch):
        """Tracing disabled -> returns without dispatching."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        with mock.patch("tracing.cursor.hooks.handlers._handle_before_submit_prompt") as h:
            _dispatch("beforeSubmitPrompt", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_not_called()

    def test_no_backend_send_fails_gracefully(self, monkeypatch):
        """send_span failure doesn't crash — the IDE turn is emitted entirely at stop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.send_span", return_value=False) as send_mock,
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c1", "generation_id": "g1"},
            )
            assert send_mock.call_count == 0
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "response": "done",
                },
            )
            # Nothing yet: the root is held so stop can close it with the real
            # end time, otherwise its children outlive it.
            assert send_mock.call_count == 0
            _dispatch(
                "stop",
                {"hook_event_name": "stop", "conversation_id": "c1", "generation_id": "g1"},
            )
            # stop closes the root, flushes the deferred LLM span, emits Agent Stop.
            assert send_mock.call_count == 3


# ---------------------------------------------------------------------------
# _handle_before_submit_prompt tests
# ---------------------------------------------------------------------------


class TestHandleBeforeSubmitPrompt:

    def test_cli_payload_sends_root_before_submit(self, captured_spans, monkeypatch):
        """CLI-style payload (hookEventName only): root CHAIN at submit; LLM is deferred to stop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="aabb" * 4),
            # wraps, not a stub: the later events read back the id this saves.
            mock.patch(
                "tracing.cursor.hooks.handlers.gen_root_span_save",
                wraps=handlers_mod.gen_root_span_save,
            ) as save_mock,
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hookEventName": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                    "model_name": "claude-4",
                },
            )

        save_mock.assert_called_once_with("gen-1", "aabb" * 4)
        assert len(captured_spans) == 1
        root0 = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert root0["name"] == "User Prompt"
        root_attrs0 = {a["key"]: a["value"] for a in root0["attributes"]}
        assert root_attrs0["input.value"]["stringValue"] == "fix the bug"
        assert "output.value" not in root_attrs0

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000):
            _dispatch(
                "afterAgentResponse",
                {
                    "hookEventName": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "I fixed the bug",
                    "model_name": "claude-4",
                },
            )

        # afterAgentResponse no longer emits the LLM span — it is deferred to stop.
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["User Prompt"]

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=10000):
            _dispatch(
                "stop",
                {"hookEventName": "stop", "conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["User Prompt", "Agent Response", "Agent Stop"]
        llm = captured_spans[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        llm_attrs = {a["key"]: a["value"] for a in llm["attributes"]}
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert llm_attrs["input.value"]["stringValue"] == "fix the bug"
        assert llm_attrs["output.value"]["stringValue"] == "I fixed the bug"
        assert llm_attrs["session.id"]["stringValue"] == "conv-1"

    def test_ide_payload_emits_whole_turn_at_stop(self, captured_spans, monkeypatch):
        """IDE payload (hook_event_name): nothing is sent until stop closes the turn."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="aabb" * 4),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                    "model_name": "claude-4",
                },
            )

        assert len(captured_spans) == 0

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "I fixed the bug",
                    "model_name": "claude-4",
                },
            )

        # Still nothing: the root is held so its end time can cover the spans
        # stop is about to emit, instead of ending before its own children.
        assert len(captured_spans) == 0

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=10000):
            _dispatch(
                "stop",
                {"hook_event_name": "stop", "conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["User Prompt", "Agent Response", "Agent Stop"]

        root = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        root_attrs = {a["key"]: a["value"] for a in root["attributes"]}
        assert root_attrs["input.value"]["stringValue"] == "fix the bug"
        assert root_attrs["output.value"]["stringValue"] == "I fixed the bug"
        assert root["startTimeUnixNano"].startswith("5000")
        assert root["endTimeUnixNano"].startswith("10000")

        llm = captured_spans[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        llm_attrs = {a["key"]: a["value"] for a in llm["attributes"]}
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert llm_attrs["input.value"]["stringValue"] == "fix the bug"
        assert llm_attrs["output.value"]["stringValue"] == "I fixed the bug"

        # Every child must close no later than the root that contains it.
        root_end = int(root["endTimeUnixNano"])
        for captured in captured_spans[1:]:
            child = captured["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            assert int(child["endTimeUnixNano"]) <= root_end


# ---------------------------------------------------------------------------
# _handle_after_agent_response tests
# ---------------------------------------------------------------------------


class TestHandleAfterAgentResponse:

    def test_defers_llm_span_until_stop(self, captured_spans, monkeypatch):
        """afterAgentResponse defers the LLM span; it is emitted only at stop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ccdd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="82e3edf5f5f3a46b"),
        ):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "I found the issue",
                    "model_name": "claude-4",
                },
            )

        # afterAgentResponse no longer sends a span immediately
        assert len(captured_spans) == 0

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="eeff" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="82e3edf5f5f3a46b"),
        ):
            _dispatch(
                "stop",
                {"hook_event_name": "stop", "conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Agent Response", "Agent Stop"]

        llm_span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in llm_span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["output.value"]["stringValue"] == "I found the issue"
        assert llm_span["parentSpanId"] == "82e3edf5f5f3a46b"

    def test_defers_llm_span_with_full_attributes(self, captured_spans, monkeypatch):
        """Deferred LLM span carries input, output, session.id, model_name when flushed at stop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=4000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="f108f23139cc05c7"),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-9",
                    "generation_id": "gen-9",
                    "prompt": "do the thing",
                    "model_name": "claude-4",
                },
            )
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-9",
                    "generation_id": "gen-9",
                    "response": "did the thing",
                    "model_name": "claude-4",
                },
            )

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=8000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="f108f23139cc05c7"),
        ):
            _dispatch("stop", {"conversation_id": "conv-9", "generation_id": "gen-9"})

        llm_span = next(
            s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            for s in captured_spans
            if s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        attrs = {a["key"]: a["value"] for a in llm_span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["input.value"]["stringValue"] == "do the thing"
        assert attrs["output.value"]["stringValue"] == "did the thing"
        assert attrs["session.id"]["stringValue"] == "conv-9"
        assert attrs["cursor.conversation.id"]["stringValue"] == "conv-9"
        assert attrs["llm.model_name"]["stringValue"] == "claude-4"

    def test_defers_llm_span_preserves_after_agent_response_timing(self, captured_spans, monkeypatch):
        """Deferred LLM span uses the start_ms recorded at afterAgentResponse, not stop's now_ms."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2500),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "afterAgentResponse",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "response": "yo"},
            )

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9999),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch("stop", {"conversation_id": "conv-1", "generation_id": "gen-1"})

        llm_span = next(
            s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            for s in captured_spans
            if s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        # start_ms recorded at afterAgentResponse (2500), not at stop (9999).
        assert llm_span["startTimeUnixNano"] == "2500000000"
        # ...and ends at stop, so the span has a real duration. It used to end
        # at its own start, rendering every response as a 0 ms bar.
        assert llm_span["endTimeUnixNano"] == "9999000000"

    def test_no_gen_id_sends_llm_span_immediately(self, captured_spans, monkeypatch):
        """Without gen_id state can't be keyed, so the LLM span is sent immediately (fallback)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ccdd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "afterAgentResponse",
                {"conversation_id": "conv-1", "response": "I found the issue"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "Agent Response"
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["output.value"]["stringValue"] == "I found the issue"


# ---------------------------------------------------------------------------
# _handle_after_shell_execution tests
# ---------------------------------------------------------------------------


class TestHandleAfterShellExecution:

    def test_creates_tool_span_with_popped_state(self, captured_spans, monkeypatch):
        """Creates TOOL span, merges with before state from state_pop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        popped = {"command": "ls -la", "cwd": "/tmp", "start_ms": "1000", "trace_id": "t1", "conversation_id": "c1"}
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="eeff" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterShellExecution",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "output": "total 0", "exit_code": "0"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "shell"
        assert attrs["output.value"]["stringValue"] == "total 0"
        assert attrs["shell.exit_code"]["stringValue"] == "0"
        assert span["name"] == "Shell"

    def test_uses_after_command_when_present(self, captured_spans, monkeypatch):
        """After-event command overrides before-event command."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        popped = {"command": "old_cmd", "start_ms": "1000"}
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterShellExecution",
                {"conversation_id": "c1", "generation_id": "g1", "command": "new_cmd", "output": "ok"},
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "new_cmd"

    def test_no_popped_state_uses_now(self, captured_spans, monkeypatch):
        """Without popped state, start_ms defaults to now_ms."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(
                "afterShellExecution",
                {"conversation_id": "c1", "generation_id": "g1", "output": "ok"},
            )

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        # start_ms = "3000" -> ns = "3000000000"
        assert span["startTimeUnixNano"] == "3000000000"

    def test_uses_fixture(self, captured_spans, monkeypatch, cursor_after_shell_input):
        """Works with cursor_after_shell fixture."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        fixture = cursor_after_shell_input
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(fixture["hook_event_name"], fixture)

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "ls -la"
        assert attrs["output.value"]["stringValue"] == "total 0"
        assert attrs["shell.exit_code"]["stringValue"] == "0"


# ---------------------------------------------------------------------------
# _handle_stop tests
# ---------------------------------------------------------------------------


class TestHandleStop:

    def test_creates_chain_span_and_cleans_up(self, captured_spans, monkeypatch):
        """Creates CHAIN span and calls state_cleanup_generation."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="3de0c6d1959ece55"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch(
                "stop",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "status": "completed", "loop_count": "3"},
            )

        cleanup.assert_called_once_with("gen-1")
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["cursor.stop.status"]["stringValue"] == "completed"
        assert attrs["cursor.stop.loop_count"]["stringValue"] == "3"
        assert span["name"] == "Agent Stop"

    def test_no_gen_id_skips_cleanup(self, captured_spans, monkeypatch):
        """Without gen_id, state_cleanup_generation is not called."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch("stop", {"conversation_id": "c1"})

        cleanup.assert_not_called()
        assert len(captured_spans) == 1

    def test_optional_attrs_omitted(self, captured_spans, monkeypatch):
        """Status and loop_count omitted when empty."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch("stop", {"conversation_id": "c1", "generation_id": "g1"})

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.stop.status" not in attr_keys
        assert "cursor.stop.loop_count" not in attr_keys


# ---------------------------------------------------------------------------
# _handle_before_shell_execution tests
# ---------------------------------------------------------------------------


class TestHandleBeforeShellExecution:

    def test_pushes_state(self, monkeypatch):
        """Pushes command, cwd, start_ms, trace_id, conversation_id to state."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeShellExecution",
                {"conversation_id": "c1", "generation_id": "gen-1", "command": "ls -la", "cwd": "/home"},
            )

        push_mock.assert_called_once()
        key, value = push_mock.call_args[0]
        assert "gen-1" in key or "gen_1" in key
        assert value["command"] == "ls -la"
        assert value["cwd"] == "/home"
        assert value["start_ms"] == "1000"

    def test_no_gen_id_returns_early(self, monkeypatch):
        """Without gen_id, returns without pushing state."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeShellExecution",
                {"conversation_id": "c1", "command": "ls"},
            )

        push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_after_agent_thought tests
# ---------------------------------------------------------------------------


class TestHandleAfterAgentThought:

    def test_creates_chain_span_with_thought(self, captured_spans, monkeypatch):
        """Creates CHAIN span with thought as output.value."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="abcd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c") as get_mock,
        ):
            _dispatch(
                "afterAgentThought",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "thought": "thinking about the problem"},
            )

        get_mock.assert_called_once_with("gen-1")
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["output.value"]["stringValue"] == "thinking about the problem"
        assert attrs["session.id"]["stringValue"] == "conv-1"
        assert span["name"] == "Agent Thinking"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"


# ---------------------------------------------------------------------------
# _handle_before_mcp_execution tests
# ---------------------------------------------------------------------------


class TestHandleBeforeMcpExecution:

    def test_pushes_state(self, monkeypatch):
        """Pushes tool_name, tool_input, url, command, start_ms to state."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1500),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeMCPExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "gen-1",
                    "tool_name": "search",
                    "tool_input": '{"query": "test"}',
                    "url": "http://localhost:3000",
                },
            )

        push_mock.assert_called_once()
        key, value = push_mock.call_args[0]
        assert "gen-1" in key or "gen_1" in key
        assert value["tool_name"] == "search"
        assert value["tool_input"] == '{"query": "test"}'
        assert value["start_ms"] == "1500"

    def test_no_gen_id_returns_early(self, monkeypatch):
        """Without gen_id, returns without pushing state."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeMCPExecution",
                {"conversation_id": "c1", "tool_name": "search"},
            )

        push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_after_mcp_execution tests
# ---------------------------------------------------------------------------


class TestHandleAfterMcpExecution:

    def test_creates_tool_span_with_popped_state(self, captured_spans, monkeypatch):
        """Creates TOOL span, merges with before state from state_pop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        popped = {
            "tool_name": "search",
            "tool_input": '{"query": "test"}',
            "url": "http://localhost:3000",
            "command": "",
            "start_ms": "1000",
            "trace_id": "t1",
            "conversation_id": "c1",
        }
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ffaa" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterMCPExecution",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "result": "found 3 items"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "search"
        assert attrs["input.value"]["stringValue"] == '{"query": "test"}'
        assert attrs["output.value"]["stringValue"] == "found 3 items"
        assert span["name"] == "MCP: search"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"

    def test_no_popped_state_uses_input(self, captured_spans, monkeypatch):
        """Without popped state, span still created from input_json fields."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="bbcc" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(
                "afterMCPExecution",
                {"conversation_id": "c1", "generation_id": "g1", "tool_name": "list_repos", "result": "ok"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.name"]["stringValue"] == "list_repos"
        assert span["name"] == "MCP: list_repos"


# ---------------------------------------------------------------------------
# _handle_before_read_file tests
# ---------------------------------------------------------------------------


class TestHandleBeforeReadFile:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path as input."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="1122" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
        ):
            _dispatch(
                "beforeReadFile",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "file_path": "/foo/bar.py"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "read_file"
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py"
        assert span["name"] == "Read File"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"


# ---------------------------------------------------------------------------
# _handle_after_file_edit tests
# ---------------------------------------------------------------------------


class TestHandleAfterFileEdit:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path and diff."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="3344" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
        ):
            _dispatch(
                "afterFileEdit",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/foo/bar.py",
                    "diff": "+added line",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "edit_file"
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py: +added line"
        assert span["name"] == "File Edit"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"

    def test_no_diff_uses_path_only(self, captured_spans, monkeypatch):
        """Without diff, input.value is just the file path."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="3344" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "afterFileEdit",
                {"conversation_id": "c1", "generation_id": "g1", "file_path": "/foo/bar.py"},
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py"


# ---------------------------------------------------------------------------
# _handle_before_tab_file_read tests
# ---------------------------------------------------------------------------


class TestHandleBeforeTabFileRead:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path as input for tab read."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="5566" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
        ):
            _dispatch(
                "beforeTabFileRead",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "file_path": "/src/main.ts"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "read_file_tab"
        assert attrs["input.value"]["stringValue"] == "/src/main.ts"
        assert span["name"] == "Tab Read File"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"


# ---------------------------------------------------------------------------
# _handle_after_tab_file_edit tests
# ---------------------------------------------------------------------------


class TestHandleAfterTabFileEdit:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path and edits for tab edit."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="7788" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a7e64b1d8f42e11c"),
        ):
            _dispatch(
                "afterTabFileEdit",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/src/main.ts",
                    "edits": "replaced function",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "edit_file_tab"
        assert attrs["input.value"]["stringValue"] == "/src/main.ts: replaced function"
        assert span["name"] == "Tab File Edit"
        assert span["parentSpanId"] == "a7e64b1d8f42e11c"

    def test_no_edits_uses_path_only(self, captured_spans, monkeypatch):
        """Without edits, input.value is just the file path."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="7788" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "afterTabFileEdit",
                {"conversation_id": "c1", "generation_id": "g1", "file_path": "/src/main.ts"},
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "/src/main.ts"


# ---------------------------------------------------------------------------
# main() entry point tests
# ---------------------------------------------------------------------------


class TestMain:

    def test_reads_stdin_dispatches_prints_permissive(self, monkeypatch, tmp_path):
        """main() reads JSON from stdin, dispatches, prints permissive response."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "c1",
            "generation_id": "g1",
            "prompt": "hello",
        }
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("beforeSubmitPrompt", input_data)
        result = json.loads(stdout_buf.getvalue())
        assert result == {"permission": "allow"}

    def test_invalid_json_still_prints_permissive(self, monkeypatch, tmp_path):
        """Invalid JSON on stdin still prints permissive response."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO("not valid json")),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        # event is "" when JSON parse fails, so we get continue response
        assert result == {"continue": True}

    def test_exception_in_dispatch_still_prints_permissive(self, monkeypatch, tmp_path):
        """Exception in _dispatch still prints permissive response."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {"hook_event_name": "afterAgentResponse", "conversation_id": "c1", "generation_id": "g1"}
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch", side_effect=RuntimeError("boom")),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        assert result == {"continue": True}

    def test_check_requirements_false_still_prints_permissive(self, monkeypatch, tmp_path):
        """When check_requirements returns False, still prints permissive."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO('{"hook_event_name":"beforeSubmitPrompt"}')),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=False),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        # event is "" because we return before reading stdin
        assert result == {"continue": True}

    def test_empty_stdin(self, monkeypatch, tmp_path):
        """Empty stdin produces empty dict, still prints permissive."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("", {})
        result = json.loads(stdout_buf.getvalue())
        assert result == {"continue": True}

    def test_stderr_redirected_to_log_file(self, monkeypatch, tmp_path):
        """main() redirects stderr to env.log_file."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))

        stdout_buf = io.StringIO()
        original_stderr = sys.stderr

        with (
            mock.patch("sys.stdin", io.StringIO('{"hook_event_name":"stop"}')),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch"),
        ):
            main()

        # Restore stderr for safety
        sys.stderr = original_stderr


# ---------------------------------------------------------------------------
# _event_name tests
# ---------------------------------------------------------------------------


class TestEventName:

    def test_supports_hook_event_name(self):
        assert _event_name({"hook_event_name": "stop"}) == "stop"

    def test_supports_hookEventName(self):
        assert _event_name({"hookEventName": "sessionStart"}) == "sessionStart"

    def test_supports_event_name(self):
        assert _event_name({"event_name": "postToolUse"}) == "postToolUse"

    def test_supports_eventName(self):
        assert _event_name({"eventName": "afterFileEdit"}) == "afterFileEdit"

    def test_supports_event(self):
        assert _event_name({"event": "beforeSubmitPrompt"}) == "beforeSubmitPrompt"

    def test_prefers_hook_event_name_over_hookEventName(self):
        assert _event_name({"hook_event_name": "stop", "hookEventName": "sessionStart"}) == "stop"

    def test_returns_empty_for_missing(self):
        assert _event_name({}) == ""


# ---------------------------------------------------------------------------
# _trace_id_from_event tests
# ---------------------------------------------------------------------------


class TestTraceIdFromEvent:

    def test_prefers_conversation_id(self):
        result = _trace_id_from_event("gen-1", "conv-1")
        assert result  # non-empty
        from tracing.cursor.hooks.adapter import trace_id_from_seed

        assert result == trace_id_from_seed("conv-1")

    def test_retried_generations_share_one_trace(self):
        """Cursor mints a new generation id per retry/continuation. Keying the
        trace on the generation split a single exchange across several traces."""
        assert _trace_id_from_event("gen-1", "conv-1") == _trace_id_from_event("gen-2", "conv-1")

    def test_falls_back_to_generation_id(self):
        result = _trace_id_from_event("gen-1", "")
        assert result
        from tracing.cursor.hooks.adapter import trace_id_from_seed

        assert result == trace_id_from_seed("gen-1")

    def test_generates_a_valid_id_when_both_empty(self):
        """An empty trace id is rejected by the receiver, and one rejected span
        fails the whole request rather than just itself."""
        result = _trace_id_from_event("", "")
        assert len(result) == 32
        int(result, 16)


# ---------------------------------------------------------------------------
# _dispatch routes sessionStart / postToolUse
# ---------------------------------------------------------------------------


class TestDispatchNewEvents:

    def test_dispatch_routes_session_start(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_session_start") as h,
        ):
            _dispatch(
                "sessionStart",
                {"conversation_id": "c1", "generation_id": "g1"},
            )
            h.assert_called_once()

    def test_dispatch_routes_session_end(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_session_end") as h,
        ):
            _dispatch(
                "sessionEnd",
                {"conversation_id": "c1", "generation_id": "g1"},
            )
            h.assert_called_once()

    def test_dispatch_routes_post_tool_use(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_post_tool_use") as h,
        ):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1"},
            )
            h.assert_called_once()


# ---------------------------------------------------------------------------
# main() dispatches camelCase event key
# ---------------------------------------------------------------------------


class TestMainCamelCase:

    def test_main_dispatches_camel_case_event_key(self, monkeypatch, tmp_path):
        """main() resolves hookEventName from CLI payloads and dispatches correctly."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {"hookEventName": "sessionStart", "conversation_id": "c1", "generation_id": "g1", "cwd": "/tmp"}
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("sessionStart", input_data)


# ---------------------------------------------------------------------------
# _handle_session_start tests
# ---------------------------------------------------------------------------


class TestHandleSessionStart:

    def test_session_start_sends_chain_span(self, captured_spans, monkeypatch):
        """sessionStart produces a CHAIN span with session.id and cwd."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ss11" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save") as save_mock,
        ):
            _dispatch(
                "sessionStart",
                {
                    "conversation_id": "conv-sess",
                    "generation_id": "gen-sess",
                    "cwd": "/Users/alice/code/myrepo",
                    "user_email": "alice@example.com",
                },
            )

        save_mock.assert_called_once_with("gen-sess", "ss11" * 4)
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Session Start"
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["session.id"]["stringValue"] == "conv-sess"
        assert attrs["cursor.session.cwd"]["stringValue"] == "/Users/alice/code/myrepo"

    def test_session_start_no_gen_id_skips_save(self, captured_spans, monkeypatch):
        """Without gen_id, gen_root_span_save is not called."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save") as save_mock,
        ):
            _dispatch(
                "sessionStart",
                {"conversation_id": "conv-sess", "cwd": "/tmp"},
            )

        save_mock.assert_not_called()
        assert len(captured_spans) == 1

    def test_session_start_optional_fields_omitted(self, captured_spans, monkeypatch):
        """Optional fields like cwd and user_email are omitted when absent."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),):
            _dispatch(
                "sessionStart",
                {"conversation_id": "conv-sess"},
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.session.cwd" not in attr_keys


# ---------------------------------------------------------------------------
# _handle_post_tool_use tests
# ---------------------------------------------------------------------------


class TestHandlePostToolUse:

    def test_post_tool_use_sends_tool_span(self, captured_spans, monkeypatch):
        """postToolUse produces a TOOL span with tool.name, input.value, output.value."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="pt11" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a601931ba4e1e4bd"),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "conv-pt",
                    "generation_id": "gen-pt",
                    "toolName": "code_search",
                    "toolInput": '{"query": "main function"}',
                    "result": "<search results>",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Tool: code_search"
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "code_search"
        assert attrs["input.value"]["stringValue"] == '{"query": "main function"}'
        assert attrs["output.value"]["stringValue"] == "<search results>"
        assert attrs["session.id"]["stringValue"] == "conv-pt"
        assert span["parentSpanId"] == "a601931ba4e1e4bd"

    def test_post_tool_use_unknown_tool_uses_command_field_when_present(self, captured_spans, monkeypatch):
        """Non-deduped shell-like tool uses 'command' field as input.value fallback."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": "custom_runner",
                    "command": "ls -la",
                    "stdout": "total 40\ndrwxr-xr-x ...",
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        # custom_runner is not in _SHELL_TOOL_NAMES so command field is NOT used as input
        # tool_input comes from the standard extraction keys
        assert attrs["output.value"]["stringValue"] == "total 40\ndrwxr-xr-x ..."

    def test_post_tool_use_missing_fields_omitted(self, captured_spans, monkeypatch):
        """Missing optional fields are omitted from attributes."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1"},
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "tool.name" not in attr_keys
        assert "input.value" not in attr_keys
        assert "output.value" not in attr_keys

    def test_post_tool_use_skips_shell_tool(self, captured_spans, monkeypatch):
        """postToolUse with tool_name='shell' is skipped (covered by dedicated handler)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1", "toolName": "shell", "command": "ls -la"},
            )

        assert len(captured_spans) == 0

    @pytest.mark.parametrize(
        "tool_name",
        ["shell", "Shell", "TERMINAL", "bash", "read_file", "edit_file", "tab_file_read", "mcp"],
    )
    def test_post_tool_use_skips_each_dedicated_tool_name(self, captured_spans, monkeypatch, tool_name):
        """postToolUse short-circuits for each known dedicated tool name (case-insensitive)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1", "toolName": tool_name},
            )

        assert len(captured_spans) == 0, f"Expected no span for tool_name={tool_name!r}"

    def test_post_tool_use_emits_for_unknown_tool_name(self, captured_spans, monkeypatch):
        """postToolUse emits a span for tools not in the dedup set."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": "glob",
                    "toolInput": '{"pattern": "*.py"}',
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Tool: glob"
        assert attrs["tool.name"]["stringValue"] == "glob"
        assert attrs["input.value"]["stringValue"] == '{"pattern": "*.py"}'


# ---------------------------------------------------------------------------
# _handle_stop token count tests
# ---------------------------------------------------------------------------


class TestHandleStopTokenCounts:

    def test_stop_captures_token_counts(self, captured_spans, monkeypatch):
        """Stop payload with token fields produces llm.token_count.* attributes."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="3de0c6d1959ece55"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                    "input_tokens": 85919,
                    "output_tokens": 1523,
                    "cache_read_tokens": 68000,
                    "cache_write_tokens": 0,
                    "model": "claude-sonnet-4.5",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        # OpenInference: prompt is the total (input 85919 + cache_read 68000 +
        # cache_write 0 = 153919); cache split is reported via prompt_details.*.
        assert attrs["llm.token_count.prompt"]["intValue"] == 153919
        assert attrs["llm.token_count.completion"]["intValue"] == 1523
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 68000
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert attrs["llm.token_count.total"]["intValue"] == 155442
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

    def test_stop_omits_token_attrs_when_payload_has_none(self, captured_spans, monkeypatch):
        """Stop payload without token fields produces no llm.token_count.* attributes."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "status": "completed"},
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "llm.token_count.prompt" not in attr_keys
        assert "llm.token_count.completion" not in attr_keys
        assert "llm.token_count.prompt_details.cache_read" not in attr_keys
        assert "llm.token_count.prompt_details.cache_write" not in attr_keys
        assert "llm.token_count.total" not in attr_keys
        assert "llm.model_name" not in attr_keys

    def test_stop_token_count_handles_string_and_dash_values(self, captured_spans, monkeypatch):
        """String token values are coerced; '--' and None are omitted."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": "100",
                    "output_tokens": "--",
                    "cache_read_tokens": None,
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["llm.token_count.prompt"]["intValue"] == 100
        assert "llm.token_count.completion" not in attrs
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.total" not in attrs


# ---------------------------------------------------------------------------
# _handle_session_end tests
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:

    def test_session_end_emits_chain_span_with_duration_and_status(self, captured_spans, monkeypatch):
        """sessionEnd produces a CHAIN span with duration, status, and reason."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="bdf65c1875530a79"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "conv-end",
                    "generation_id": "gen-end",
                    "duration_ms": 7447445,
                    "final_status": "completed",
                    "reason": "window_close",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Session End"
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["cursor.session.duration_ms"]["intValue"] == 7447445
        assert attrs["cursor.session.final_status"]["stringValue"] == "completed"
        assert attrs["cursor.session.reason"]["stringValue"] == "window_close"
        assert attrs["session.id"]["stringValue"] == "conv-end"
        assert span["parentSpanId"] == "bdf65c1875530a79"

    def test_session_end_cleans_up_generation(self, captured_spans, monkeypatch):
        """sessionEnd calls state_cleanup_generation with the gen_id."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch(
                "sessionEnd",
                {"conversation_id": "conv-end", "generation_id": "g-123"},
            )

        cleanup.assert_called_once_with("g-123")

    def test_session_end_handles_empty_payload(self, captured_spans, monkeypatch):
        """sessionEnd with only conversation_id emits a span without optional attrs."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {"conversation_id": "conv-end"},
            )

        assert len(captured_spans) == 1
        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "session.id" in attr_keys
        assert "cursor.conversation.id" in attr_keys
        assert "cursor.session.duration_ms" not in attr_keys
        assert "cursor.session.final_status" not in attr_keys
        assert "cursor.session.reason" not in attr_keys
        assert "llm.token_count.prompt" not in attr_keys


# ---------------------------------------------------------------------------
# cursor.conversation.id attribute tests
# ---------------------------------------------------------------------------


class TestConversationIdAttribute:

    # Minimal payloads per event that produce at least one span
    _EVENT_PAYLOADS = {
        "beforeSubmitPrompt": {
            "hookEventName": "beforeSubmitPrompt",  # CLI path — sends span immediately
            "conversation_id": "conv-abc",
            "generation_id": "gen-abc",
            "prompt": "test",
        },
        # afterAgentResponse is excluded: under the deferred-LLM design it no longer
        # emits a span on its own — the Agent Response LLM span is flushed at stop.
        # cursor.conversation.id on that deferred span is covered by
        # TestDeferredLlmSpan below.
        "afterAgentThought": {"conversation_id": "conv-abc", "generation_id": "gen-aat", "thought": "thinking"},
        "afterShellExecution": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ase",
            "command": "ls",
            "output": "ok",
        },
        "afterMCPExecution": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ame",
            "tool_name": "my_tool",
            "result": "ok",
        },
        "beforeReadFile": {"conversation_id": "conv-abc", "generation_id": "gen-brf", "file_path": "/tmp/a.py"},
        "afterFileEdit": {"conversation_id": "conv-abc", "generation_id": "gen-afe", "file_path": "/tmp/a.py"},
        "beforeTabFileRead": {"conversation_id": "conv-abc", "generation_id": "gen-btfr", "file_path": "/tmp/a.py"},
        "afterTabFileEdit": {"conversation_id": "conv-abc", "generation_id": "gen-atfe", "file_path": "/tmp/a.py"},
        "stop": {"conversation_id": "conv-abc", "generation_id": "gen-stop", "status": "completed"},
        "sessionStart": {"conversation_id": "conv-abc", "generation_id": "gen-ss", "cwd": "/tmp"},
        "sessionEnd": {"conversation_id": "conv-abc", "generation_id": "gen-se"},
        "postToolUse": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ptu",
            "toolName": "glob",
            "toolInput": "*.py",
        },
    }

    # Events that push state but don't emit a span
    _NO_SPAN_EVENTS = {"beforeShellExecution", "beforeMCPExecution"}

    @pytest.mark.parametrize(
        "event",
        [e for e in _EVENT_PAYLOADS],
    )
    def test_conversation_id_attribute_on_every_handler(self, captured_spans, monkeypatch, event):
        """Every span-producing handler includes cursor.conversation.id."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(event, self._EVENT_PAYLOADS[event])

        assert len(captured_spans) >= 1, f"No spans emitted for {event}"
        for sent in captured_spans:
            span = sent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert (
                attrs.get("cursor.conversation.id", {}).get("stringValue") == "conv-abc"
            ), f"cursor.conversation.id missing or wrong on {event} span {span['name']}"

    def test_conversation_id_attribute_omitted_when_missing(self, captured_spans, monkeypatch):
        """When conversation_id is empty, cursor.conversation.id is not set."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {"generation_id": "gen-1", "status": "completed"},
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.conversation.id" not in attr_keys


# ---------------------------------------------------------------------------
# IDE-safety regression test
# ---------------------------------------------------------------------------


class TestIdeSafety:

    def test_ide_payload_with_no_post_tool_use_unaffected(self, captured_spans, monkeypatch):
        """IDE dispatches before/afterShellExecution without postToolUse — exactly 1 span from after."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="c2432f0e9cbf4c78"),
        ):
            _dispatch(
                "beforeShellExecution",
                {
                    "hook_event_name": "beforeShellExecution",
                    "conversation_id": "conv-ide",
                    "generation_id": "gen-ide",
                    "command": "echo hello",
                    "cwd": "/tmp",
                },
            )

        # beforeShellExecution emits no span
        assert len(captured_spans) == 0

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="c2432f0e9cbf4c78"),
        ):
            _dispatch(
                "afterShellExecution",
                {
                    "hook_event_name": "afterShellExecution",
                    "conversation_id": "conv-ide",
                    "generation_id": "gen-ide",
                    "command": "echo hello",
                    "output": "hello",
                    "exit_code": "0",
                },
            )

        # afterShellExecution emits exactly one span
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "Shell"
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "shell"


# ---------------------------------------------------------------------------
# Deferred LLM span tests (afterAgentResponse stashes; stop flushes)
# ---------------------------------------------------------------------------


def _spans_by_name(captured):
    """Flatten captured_spans into {name: [span_dict, ...]}."""
    out = {}
    for sent in captured:
        s = sent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        out.setdefault(s["name"], []).append(s)
    return out


def _attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


class TestDeferredLlmSpan:
    """Per-turn token counts must land on Agent Response (LLM), not Agent Stop (CHAIN).

    The fix: afterAgentResponse stashes the LLM span; stop pops it, attaches the
    token counts from the stop payload, and sends the LLM span before Agent Stop.
    """

    def test_ide_happy_path_tokens_land_on_llm_span(self, captured_spans, monkeypatch):
        """IDE turn (beforeSubmit → after → stop with tokens): tokens on LLM span, none on Agent Stop."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                    "model_name": "claude-sonnet-4.5",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=7000):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "fixed",
                    "model_name": "claude-sonnet-4.5",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000):
            _dispatch(
                "stop",
                {
                    "hook_event_name": "stop",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                    "input_tokens": 85919,
                    "output_tokens": 1523,
                    "cache_read_tokens": 68000,
                    "cache_write_tokens": 0,
                    "model": "claude-sonnet-4.5",
                },
            )

        spans = _spans_by_name(captured_spans)
        assert "User Prompt" in spans
        assert "Agent Response" in spans
        assert "Agent Stop" in spans

        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        # prompt = input 85919 + cache_read 68000 + cache_write 0 = 153919
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 153919
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 1523
        assert llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 68000
        assert llm_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert llm_attrs["llm.token_count.total"]["intValue"] == 155442
        assert llm_attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

        stop_attrs = _attrs(spans["Agent Stop"][0])
        assert stop_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.prompt_details.cache_read",
            "llm.token_count.prompt_details.cache_write",
            "llm.token_count.total",
        ):
            assert k not in stop_attrs, f"{k} should not be on Agent Stop CHAIN span"

    def test_stop_emits_llm_span_before_agent_stop(self, captured_spans, monkeypatch):
        """Order: Agent Response (LLM) is sent first, then Agent Stop (CHAIN)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "response": "done"},
            )

        assert len(captured_spans) == 0

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=4000),
            # A turn Agent Stop can close: with no root it is dropped rather
            # than sent as its own parentless trace.
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
        ):
            _dispatch(
                "stop",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "input_tokens": 10, "output_tokens": 5},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        # LLM must come before Agent Stop so strict OTLP backends see the parent first.
        assert names.index("Agent Response") < names.index("Agent Stop")

    def test_stop_fallback_keeps_tokens_on_chain_when_no_deferred_llm(self, captured_spans, monkeypatch):
        """No prior afterAgentResponse → CLI/sessionEnd-style behavior: Agent Stop carries tokens."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 25,
                    "cache_write_tokens": 5,
                    "model": "claude-sonnet-4.5",
                    "status": "completed",
                },
            )

        # Only the Agent Stop span is sent (no deferred LLM existed).
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Agent Stop"]
        stop_attrs = _attrs(captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert stop_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        # prompt = input 100 + cache_read 25 + cache_write 5 = 130
        assert stop_attrs["llm.token_count.prompt"]["intValue"] == 130
        assert stop_attrs["llm.token_count.completion"]["intValue"] == 50
        assert stop_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 25
        assert stop_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5
        assert stop_attrs["llm.token_count.total"]["intValue"] == 180
        assert stop_attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

    def test_deferred_llm_dropped_when_stop_never_fires(self, captured_spans, monkeypatch):
        """beforeSubmit + afterAgentResponse without stop: deferred LLM span is never sent."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "p",
                },
            )
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "r",
                },
            )

        # afterAgentResponse sends the deferred root, but no Agent Response LLM yet.
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert "Agent Response" not in names

    def test_stop_without_tokens_still_flushes_deferred_llm_without_token_attrs(self, captured_spans, monkeypatch):
        """If the stop payload has no tokens, the flushed LLM span has no llm.token_count.*."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "response": "done"},
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch(
                "stop",
                {"conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        spans = _spans_by_name(captured_spans)
        assert "Agent Response" in spans
        llm_attrs = _attrs(spans["Agent Response"][0])
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.prompt_details.cache_read",
            "llm.token_count.prompt_details.cache_write",
            "llm.token_count.total",
        ):
            assert k not in llm_attrs

    def test_zero_token_count_not_treated_as_absent(self, captured_spans, monkeypatch):
        """0 is a valid token count and must appear on the LLM span (no truthiness bugs)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "response": "done"},
            )
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )

        spans = _spans_by_name(captured_spans)
        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 0
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 0
        assert llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 0
        assert llm_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert llm_attrs["llm.token_count.total"]["intValue"] == 0

    def test_session_end_tokens_are_not_counted_as_llm_tokens(self, captured_spans, monkeypatch):
        """sessionEnd reports session cumulative totals, which every turn in the
        session has already reported for itself. Under `llm.token_count.*` they
        would be summed a second time and roughly double the session's cost, so
        they go under a session namespace the cost model does not read."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {"conversation_id": "conv-end", "generation_id": "gen-end", "input_tokens": 200, "output_tokens": 75},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Session End"]
        attrs = _attrs(captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert attrs["cursor.session.tokens.prompt"]["intValue"] == 200
        assert attrs["cursor.session.tokens.completion"]["intValue"] == 75
        assert not [k for k in attrs if k.startswith("llm.token_count.")]

    def test_deferred_llm_uses_recorded_parent_and_start_time_at_stop(self, captured_spans, monkeypatch):
        """The flushed LLM span uses the parent and start_ms recorded at afterAgentResponse."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        # beforeSubmitPrompt records the root via gen_root_span_save (real disk).
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "p",
                },
            )

        # Capture the root span id that beforeSubmitPrompt persisted.
        from tracing.cursor.hooks.adapter import gen_root_span_get as real_get

        root_span_id = real_get("gen-1")
        assert root_span_id  # sanity

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2500):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "r",
                },
            )

        # stop runs at a much later timestamp — the LLM span must still use 2500.
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=99999):
            _dispatch(
                "stop",
                {"hook_event_name": "stop", "conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        spans = _spans_by_name(captured_spans)
        llm_span = spans["Agent Response"][0]
        assert llm_span["parentSpanId"] == root_span_id
        assert llm_span["startTimeUnixNano"] == "2500000000"
        # Ends at stop, giving the span a real duration rather than a 0 ms bar.
        assert llm_span["endTimeUnixNano"] == "99999000000"

    def test_multiple_deferred_llms_only_most_recent_gets_token_counts(self, captured_spans, monkeypatch):
        """Two afterAgentResponse events in one generation: each becomes an LLM span;
        tokens only attach to the most recent (last pushed = first popped)."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "first response",
                    "model_name": "claude-4",
                },
            )
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "second response",
                    "model_name": "claude-4",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch(
                "stop",
                {"conversation_id": "conv-1", "generation_id": "gen-1", "input_tokens": 100, "output_tokens": 20},
            )

        spans = _spans_by_name(captured_spans)
        agent_response_spans = spans.get("Agent Response", [])
        assert len(agent_response_spans) == 2

        outputs = [_attrs(s).get("output.value", {}).get("stringValue") for s in agent_response_spans]
        # Identify which span carries tokens; that one must be the most recent
        # (second response). The other (first response) must have no token attrs.
        token_idx = next(i for i, s in enumerate(agent_response_spans) if "llm.token_count.prompt" in _attrs(s))
        no_token_idx = 1 - token_idx
        assert outputs[token_idx] == "second response"
        assert outputs[no_token_idx] == "first response"

        with_tokens = _attrs(agent_response_spans[token_idx])
        assert with_tokens["llm.token_count.prompt"]["intValue"] == 100
        assert with_tokens["llm.token_count.completion"]["intValue"] == 20
        assert with_tokens["llm.token_count.total"]["intValue"] == 120

        without = _attrs(agent_response_spans[no_token_idx])
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.total",
        ):
            assert k not in without

    def test_deferred_llm_carries_conversation_id_and_user_id(self, captured_spans, monkeypatch):
        """The flushed LLM span includes cursor.conversation.id and user.id when present."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        # _resolve_user_id prefers env.user_id over payload user_email; clear env so
        # the test exercises the payload-only branch deterministically across machines.
        monkeypatch.setenv("ATATUS_USER_ID", "")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-abc",
                    "generation_id": "gen-1",
                    "response": "r",
                    "user_email": "alice@example.com",
                },
            )
            _dispatch(
                "stop",
                {"conversation_id": "conv-abc", "generation_id": "gen-1"},
            )

        spans = _spans_by_name(captured_spans)
        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["session.id"]["stringValue"] == "conv-abc"
        assert llm_attrs["cursor.conversation.id"]["stringValue"] == "conv-abc"
        assert llm_attrs["user.id"]["stringValue"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Noise suppression: events that have no turn to attach to
# ---------------------------------------------------------------------------


class TestOrphanSpansAreDropped:
    """A span with no root becomes a trace of its own — a Traces-page row with
    no prompt, no response and no tokens. Every handler must decline instead."""

    @pytest.mark.parametrize(
        "event,payload",
        [
            ("beforeTabFileRead", {"file_path": "/a.py"}),
            ("afterTabFileEdit", {"file_path": "/a.py", "edits": "x"}),
            ("beforeReadFile", {"file_path": "/a.py"}),
            ("afterFileEdit", {"file_path": "/a.py"}),
            ("afterAgentThought", {"thought": "hmm"}),
            ("stop", {"input_tokens": 10, "output_tokens": 5}),
        ],
    )
    def test_no_span_without_a_root(self, captured_spans, monkeypatch, event, payload):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""):
            _dispatch(event, dict(payload))
        assert captured_spans == []

    def test_tab_events_are_not_registered_at_install(self):
        """Cursor Tab is inline autocomplete, not agent activity: it fires while
        the user types and carries no conversation or generation id."""
        from tracing.cursor.constants import HOOK_EVENTS

        assert "beforeTabFileRead" not in HOOK_EVENTS
        assert "afterTabFileEdit" not in HOOK_EVENTS


class TestPostToolUseDeduplication:
    """postToolUse overlaps the dedicated before*/after* pairs. Matching the
    exact name missed every spelling Cursor actually sends, so one shell command
    produced both a `Shell` span and a `Tool: runTerminalCmd` span."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "runTerminalCmd",
            "run_terminal_cmd",
            "search_replace",
            "apply_patch",
            "MultiEdit",
            "str_replace_editor",
            "read_file",
            "mcp_atlassian_getJiraIssue",
        ],
    )
    def test_duplicate_of_a_dedicated_handler_is_skipped(self, captured_spans, monkeypatch, tool_name):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1", "tool_name": tool_name},
            )
        assert captured_spans == []

    @pytest.mark.parametrize("tool_name", ["todo_write", "codebase_search", "list_dir"])
    def test_tool_without_a_dedicated_handler_is_still_traced(self, captured_spans, monkeypatch, tool_name):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="a" * 16):
            _dispatch(
                "postToolUse",
                {"conversation_id": "c1", "generation_id": "g1", "tool_name": tool_name},
            )
        assert len(captured_spans) == 1
        assert _attrs(captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])["tool.name"][
            "stringValue"
        ] == tool_name
