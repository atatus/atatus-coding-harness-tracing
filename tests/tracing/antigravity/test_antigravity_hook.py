#!/usr/bin/env python3
"""Tests for tracing.antigravity.hooks.handlers — Stop and PreInvocation."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager
from tracing.antigravity.hooks import handlers as handlers_mod
from tracing.antigravity.hooks.handlers import _print_response, _read_stdin, pre_invocation, stop

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _get_span(payload):
    return _get_spans(payload)[0]


def _get_span_attrs(payload):
    span = _get_span(payload)
    return {a["key"]: a["value"] for a in span["attributes"]}


def _by_kind(payloads, kind: str) -> list:
    """Every payload whose span is of OpenInference *kind*."""
    return [
        p
        for p in payloads
        if any(
            a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == kind
            for a in _get_span(p)["attributes"]
        )
    ]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    """A StateManager with a temp state file, pre-initialized."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "test-session-antigravity")
    sm.set("project_name", "test-antigravity-project")
    sm.set("user_id", "test-user")
    sm.set("last_emitted_turn", "-1")
    return sm


@pytest.fixture
def mock_resolve(state):
    with mock.patch("tracing.antigravity.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    with mock.patch("tracing.antigravity.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def mock_gc():
    with mock.patch("tracing.antigravity.hooks.handlers.gc_stale_state_files") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock _send_payload_async and collect every span the handlers emitted.

    Patching _send_payload_async (rather than send_span) lets tests run
    synchronously without forking, regardless of the ATATUS_DISABLE_FORK env.
    Handlers batch many spans into one payload, so each is exploded back into a
    single-span payload here: a test asserting on one span should not have to
    know how many other spans shared its POST.
    """
    sent = []

    def collect(payload):
        for span in _get_spans(payload):
            envelope = json.loads(json.dumps(payload))
            envelope["resourceSpans"][0]["scopeSpans"][0]["spans"] = [span]
            sent.append(envelope)

    with mock.patch("tracing.antigravity.hooks.handlers._send_payload_async", side_effect=collect):
        yield sent


@pytest.fixture
def trace_enabled(monkeypatch):
    monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")


@pytest.fixture(autouse=True)
def isolated_cli_data_dir(tmp_path, monkeypatch):
    """Point the CLI data dir at an empty temp dir for every test.

    Model resolution reads the conversation store and settings.json from it. Left
    alone the suite would read the developer's own Antigravity install and pass or
    fail depending on which model they last selected.
    """
    from tracing.antigravity import constants as _c

    monkeypatch.setattr(_c, "CLI_DATA_DIR", tmp_path / "cli-data")


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:
    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin() == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin() == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c1"}')):
            assert _read_stdin() == {"conversationId": "c1"}


# ---------------------------------------------------------------------------
# _print_response tests
# ---------------------------------------------------------------------------


class TestPrintResponse:
    def test_prints_empty_json(self, capsys):
        _print_response()
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {}

    def test_no_continue_field(self, capsys):
        """Must NOT emit 'continue' (would force agent loop to re-enter)."""
        _print_response()
        raw = capsys.readouterr().out
        assert "continue" not in raw


# ---------------------------------------------------------------------------
# Stdout discipline of entry points
# ---------------------------------------------------------------------------


class TestEntryStdoutDiscipline:
    def test_pre_invocation_empty_stdin_prints_empty(self, capsys, trace_enabled, mock_resolve, mock_ensure):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            pre_invocation()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "continue" not in captured.out

    def test_stop_empty_stdin_prints_empty(self, capsys, trace_enabled, mock_resolve, mock_ensure, mock_gc):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            stop()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "continue" not in captured.out

    def test_pre_invocation_exception_still_prints_empty(self, capsys, trace_enabled):
        with (
            mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c"}')),
            mock.patch(
                "tracing.antigravity.hooks.handlers._handle_pre_invocation",
                side_effect=RuntimeError("boom"),
            ),
        ):
            pre_invocation()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "boom" in captured.err

    def test_stop_exception_still_prints_empty(self, capsys, trace_enabled):
        with (
            mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c"}')),
            mock.patch(
                "tracing.antigravity.hooks.handlers._handle_stop",
                side_effect=RuntimeError("kaboom"),
            ),
        ):
            stop()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "kaboom" in captured.err


# ---------------------------------------------------------------------------
# No conversationId and no transcriptPath: hooks no-op instead of emitting
# unkeyed duplicates
# ---------------------------------------------------------------------------


class TestNoSessionKey:
    @pytest.fixture
    def temp_state_dir(self, tmp_path, monkeypatch):
        from tracing.antigravity.hooks import adapter

        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    def test_stop_without_key_emits_nothing(self, capsys, trace_enabled, temp_state_dir, captured_spans):
        with mock.patch.object(sys, "stdin", new=io.StringIO("{}")):
            stop()
        assert captured_spans == []
        assert json.loads(capsys.readouterr().out.strip()) == {}

    def test_pre_invocation_without_key_emits_nothing(self, capsys, trace_enabled, temp_state_dir, captured_spans):
        with mock.patch.object(sys, "stdin", new=io.StringIO("{}")):
            pre_invocation()
        assert captured_spans == []
        assert json.loads(capsys.readouterr().out.strip()) == {}


# ---------------------------------------------------------------------------
# Single-turn emission from the real fixture
# ---------------------------------------------------------------------------


class TestStopSingleTurnFixture:
    @pytest.fixture
    def stop_with_fixture(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return captured_spans

    def test_the_turn_is_the_only_llm_kind_span(self, stop_with_fixture):
        """Consumers count LLM-kind spans as turns. A second one reads as a
        second turn, which is what put 14 rows on the Traces page for one turn."""
        llm_spans = _by_kind(stop_with_fixture, "LLM")
        assert len(llm_spans) == 1
        span = _get_span(llm_spans[0])
        assert span["name"] == "Turn 1"
        assert not span.get("parentSpanId")

    def test_emits_five_tool_spans(self, stop_with_fixture):
        tool_spans = [
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "TOOL"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(tool_spans) == 5
        names = [_get_span(p)["name"] for p in tool_spans]
        assert names == [
            "grep_search",
            "list_dir",
            "view_file",
            "search_web",
            "run_command",
        ]

    def test_emits_one_step_span_per_planner_response(self, stop_with_fixture):
        """The per-call boundaries survive as CHAIN steps (fixture has 6)."""
        steps = _by_kind(stop_with_fixture, "CHAIN")
        assert len(steps) == 6
        assert [_get_span(p)["name"] for p in steps] == [f"Model call {i}" for i in range(1, 7)]

    def test_turn_reports_how_many_calls_it_took(self, stop_with_fixture):
        attrs = _get_span_attrs(_by_kind(stop_with_fixture, "LLM")[0])
        assert attrs["llm.call_count"]["intValue"] == 6

    def test_one_trace_with_a_single_root(self, stop_with_fixture):
        spans = [_get_span(p) for p in stop_with_fixture]
        assert len({s["traceId"] for s in spans}) == 1
        roots = [s for s in spans if not s.get("parentSpanId")]
        assert len(roots) == 1
        assert roots[0]["name"] == "Turn 1"

    def test_step_spans_hang_off_the_turn(self, stop_with_fixture):
        spans = [_get_span(p) for p in stop_with_fixture]
        root = next(s for s in spans if not s.get("parentSpanId"))
        for span in _by_kind(stop_with_fixture, "CHAIN"):
            assert _get_span(span)["parentSpanId"] == root["spanId"]

    def test_tools_nest_under_the_call_that_requested_them(self, stop_with_fixture):
        """A tool belongs to the model call that asked for it, not to the turn."""
        spans = [_get_span(p) for p in stop_with_fixture]
        root = next(s for s in spans if not s.get("parentSpanId"))
        step_ids = {_get_span(p)["spanId"] for p in _by_kind(stop_with_fixture, "CHAIN")}

        tools = _by_kind(stop_with_fixture, "TOOL")
        assert tools, "fixture should contain tool spans"
        for payload in tools:
            span = _get_span(payload)
            assert span["parentSpanId"] in step_ids
            assert span["parentSpanId"] != root["spanId"]
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert attrs["tracing.parentage"]["stringValue"] == "model_call"

    def test_every_span_fits_inside_its_parent(self, stop_with_fixture):
        """A child that outlives its parent renders as a broken waterfall."""
        spans = {_get_span(p)["spanId"]: _get_span(p) for p in stop_with_fixture}
        for span in spans.values():
            parent_id = span.get("parentSpanId")
            if not parent_id:
                continue
            parent = spans[parent_id]
            assert int(span["startTimeUnixNano"]) >= int(parent["startTimeUnixNano"])
            assert int(span["endTimeUnixNano"]) <= int(parent["endTimeUnixNano"])

    def test_turn_input_mentions_codecov(self, stop_with_fixture):
        chain_payload = next(
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(chain_payload)
        assert "codecov" in attrs["input.value"]["stringValue"]

    def test_llm_model_name_is_an_id_not_a_display_label(self, stop_with_fixture):
        """The label is what the transcript carries; the column wants an id."""
        attrs = _get_span_attrs(_by_kind(stop_with_fixture, "LLM")[0])
        assert attrs["llm.model_name"]["stringValue"] == "gemini-3.5-flash"
        assert attrs["antigravity.model_label"]["stringValue"] == "Gemini 3.5 Flash (Medium)"

    def test_only_the_turn_carries_the_model(self, stop_with_fixture):
        """Repeating it on every step would multiply this turn's contribution to
        every model-keyed count downstream."""
        carriers = [p for p in stop_with_fixture if "llm.model_name" in _get_span_attrs(p)]
        assert len(carriers) == 1
        assert _get_span(carriers[0])["name"] == "Turn 1"

    def test_no_token_count_attributes(self, stop_with_fixture):
        """Antigravity withholds tokens — we must not invent them."""
        for payload in stop_with_fixture:
            for attr in _get_span(payload)["attributes"]:
                assert not attr["key"].startswith("llm.token_count"), f"unexpected token attr emitted: {attr['key']}"


# ---------------------------------------------------------------------------
# Tool descriptions come from Antigravity's PascalCase arg keys
# ---------------------------------------------------------------------------


class TestToolDescription:
    def _tool_descriptions(self, spans) -> dict:
        out = {}
        for p in spans:
            span = _get_span(p)
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            kind = attrs.get("openinference.span.kind", {}).get("stringValue", "")
            if kind == "TOOL":
                out[span["name"]] = attrs["tool.description"]["stringValue"]
        return out

    @pytest.fixture
    def fixture_descriptions(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return self._tool_descriptions(captured_spans)

    def test_run_command_uses_commandline_key(self, fixture_descriptions):
        assert fixture_descriptions["run_command"] == (
            "curl -X POST --data-binary @codecov.yml https://api.codecov.io/validate"
        )

    def test_view_file_uses_absolutepath_key(self, fixture_descriptions):
        assert fixture_descriptions["view_file"] == ("/home/user/Documents/code/coding-harness-tracing/codecov.yml")

    def test_list_dir_uses_directorypath_key(self, fixture_descriptions):
        assert fixture_descriptions["list_dir"] == "/home/user/Documents/code/coding-harness-tracing"

    def test_grep_search_uses_pascalcase_query_key(self, fixture_descriptions):
        assert fixture_descriptions["grep_search"] == "codecov.yml"

    def test_search_web_keeps_lowercase_query_key(self, fixture_descriptions):
        assert fixture_descriptions["search_web"] == (
            "codecov yml validation schema spec comment layout behavior require_changes"
        )

    def test_unknown_args_fall_back_to_truncated_json_blob(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        transcript = tmp_path / "transcript.jsonl"
        args = {"SomethingElse": "z" * 300}
        _write_jsonl(
            transcript,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "calling",
                    "tool_calls": [{"name": "mystery_tool", "args": args}],
                },
                {
                    "step_index": 2,
                    "type": "MYSTERY_TOOL",
                    "created_at": "2026-06-09T16:00:02Z",
                    "content": "ok",
                },
            ],
        )
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        desc = self._tool_descriptions(captured_spans)["mystery_tool"]
        assert desc == json.dumps(args)[:200]
        assert len(desc) == 200


# ---------------------------------------------------------------------------
# High-water-mark dedup: re-running Stop emits nothing the second time
# ---------------------------------------------------------------------------


class TestStopIdempotent:
    def test_second_stop_emits_nothing(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        first_count = len(captured_spans)
        assert first_count > 0

        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        assert len(captured_spans) == first_count


# ---------------------------------------------------------------------------
# Watermark is turn-based: missing step_index must not stop tracing
# ---------------------------------------------------------------------------


class TestTurnWatermark:
    def _chain_spans(self, spans):
        return [
            p
            for p in spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        ]

    def test_missing_step_index_does_not_stop_tracing(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        """Records without ``step_index`` (max_step_index 0) must not freeze the
        watermark after the first turn."""
        transcript = tmp_path / "transcript.jsonl"
        turn_one = [
            {
                "type": "USER_INPUT",
                "created_at": "2026-06-09T16:00:00Z",
                "content": "<USER_REQUEST>first</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-06-09T16:00:01Z",
                "content": "first answer",
            },
        ]
        _write_jsonl(transcript, turn_one)
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        assert len(self._chain_spans(captured_spans)) == 1

        turn_two = [
            {
                "type": "USER_INPUT",
                "created_at": "2026-06-09T16:00:10Z",
                "content": "<USER_REQUEST>second</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-06-09T16:00:11Z",
                "content": "second answer",
            },
        ]
        _write_jsonl(transcript, turn_one + turn_two)
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        chains = self._chain_spans(captured_spans)
        assert len(chains) == 2
        outputs = [_get_span_attrs(p)["output.value"]["stringValue"] for p in chains]
        assert outputs == ["first answer", "second answer"]

    def test_state_without_a_watermark_emits_everything(
        self, tmp_path, trace_enabled, mock_ensure, mock_gc, captured_spans
    ):
        """A state file missing ``last_emitted_turn`` starts from the beginning.

        There has never been another watermark key to fall back to, so the only
        safe reading of "no watermark" is "nothing emitted yet".
        """
        sf = tmp_path / "state_fresh.json"
        lp = tmp_path / ".lock_fresh"
        sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
        sm.init_state()
        sm.set("session_id", "fresh-session")
        sm.set("project_name", "fresh-project")
        sm.set("user_id", "fresh-user")

        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with (
            mock.patch("tracing.antigravity.hooks.handlers.resolve_session", return_value=sm),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))),
        ):
            stop()

        assert captured_spans
        assert sm.get("last_emitted_turn") == "0"


# ---------------------------------------------------------------------------
# PreInvocation excludes the final turn
# ---------------------------------------------------------------------------


class TestPreInvocationExcludesFinal:
    def test_single_turn_emits_nothing(self, trace_enabled, mock_resolve, mock_ensure, captured_spans):
        """With one turn in the transcript (which is the *final* turn),
        PreInvocation emits zero spans."""
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            pre_invocation()
        assert captured_spans == []

    def test_two_turn_inline_emits_first_only(self, tmp_path, trace_enabled, mock_resolve, mock_ensure, captured_spans):
        """A two-turn transcript: PreInvocation emits the first turn only."""
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>first</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "first answer",
                },
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:10Z",
                    "content": "<USER_REQUEST>second</USER_REQUEST>",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:11Z",
                    "content": "second answer",
                },
            ],
        )
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(transcript),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            pre_invocation()

        chain_spans = [
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(chain_spans) == 1
        attrs = _get_span_attrs(chain_spans[0])
        assert attrs["input.value"]["stringValue"] == "first"
        assert attrs["output.value"]["stringValue"] == "first answer"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_prompts_redacted(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", "false")
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        chain_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(chain_payload)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]

        llm_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "LLM"
                for a in _get_span(p)["attributes"]
            )
        )
        llm_attrs = _get_span_attrs(llm_payload)
        assert "redacted" in llm_attrs["output.value"]["stringValue"]

    def test_tool_content_redacts_outputs_only(
        self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        """tool_content gates tool *outputs*; tool inputs stay visible."""
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "false")
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        tool_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "TOOL"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(tool_payload)
        assert "redacted" in attrs["output.value"]["stringValue"]
        assert "redacted" not in attrs["input.value"]["stringValue"]
        assert "redacted" not in attrs["tool.description"]["stringValue"]

    def test_tool_details_redacts_inputs_only(
        self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        """tool_details gates tool *inputs* (args, description); outputs stay visible."""
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        tool_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "TOOL"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(tool_payload)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["tool.description"]["stringValue"]
        assert "redacted" not in attrs["output.value"]["stringValue"]


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------


class TestMainDispatcher:
    def test_no_args_exits(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["atatus-hook"])
        with pytest.raises(SystemExit) as exc:
            handlers_mod.main()
        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().err.lower()

    def test_unknown_handler_exits(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["atatus-hook", "nope"])
        with pytest.raises(SystemExit) as exc:
            handlers_mod.main()
        assert exc.value.code == 1
        assert "unknown handler" in capsys.readouterr().err.lower()

    def test_dispatches_pre_invocation(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["atatus-hook", "pre_invocation"])
        with mock.patch.object(handlers_mod, "pre_invocation") as m:
            handlers_mod.main()
        m.assert_called_once()

    def test_dispatches_stop(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["atatus-hook", "stop"])
        with mock.patch.object(handlers_mod, "stop") as m:
            handlers_mod.main()
        m.assert_called_once()


# ---------------------------------------------------------------------------
# The messy fixture: system records, a failing command, a rate limit, and a
# planner that produced nothing. Every one of these occurs in real transcripts.
# ---------------------------------------------------------------------------

MESSY_TRANSCRIPT = FIXTURE_DIR / "messy" / "transcript.jsonl"


class TestMessyTranscript:
    @pytest.fixture
    def spans(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", "true")
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")
        stdin_payload = {"conversationId": "c-messy", "transcriptPath": str(MESSY_TRANSCRIPT)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return captured_spans

    def _tool(self, spans, name):
        return next(p for p in _by_kind(spans, "TOOL") if _get_span(p)["name"] == name)

    def test_system_records_do_not_become_tool_output(self, spans):
        """A CHECKPOINT landing between a call and its result must not be paired.

        This is the defect that put a conversation-truncation summary in a
        list_dir span: any record outside the three known types used to consume
        a pending call and shift every pairing after it.
        """
        for payload in _by_kind(spans, "TOOL"):
            output = _get_span_attrs(payload)["output.value"]["stringValue"]
            assert "CHECKPOINT" not in output
            assert "updated their workspace rules" not in output

    def test_calls_pair_with_their_own_results(self, spans):
        run = _get_span_attrs(self._tool(spans, "run_command"))
        assert "No rule to make target" in run["output.value"]["stringValue"]
        listing = _get_span_attrs(self._tool(spans, "list_dir"))
        assert '"name":"out"' in listing["output.value"]["stringValue"]

    def test_generic_is_a_tool_result_not_scaffolding(self, spans):
        """GENERIC is MODEL-sourced and carries manage_task/schedule results.

        Filtering scaffolding by type name rather than by ``source`` drops it.
        """
        attrs = _get_span_attrs(self._tool(spans, "manage_task"))
        assert "Task created." in attrs["output.value"]["stringValue"]

    def test_failed_command_reports_a_failed_status(self, spans):
        span = _get_span(self._tool(spans, "run_command"))
        assert span["status"]["code"] == 2
        assert "exit code 2" in span["status"]["message"]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.exit_code"]["intValue"] == 2

    def test_successful_command_reports_success(self, spans):
        assert _get_span(self._tool(spans, "list_dir"))["status"]["code"] == 1

    def test_rate_limit_lands_on_the_call_it_interrupted(self, spans):
        """An ERROR_MESSAGE is the only place a 429 appears anywhere."""
        failed = [p for p in _by_kind(spans, "CHAIN") if _get_span(p)["status"]["code"] == 2]
        assert len(failed) == 1
        attrs = _get_span_attrs(failed[0])
        assert attrs["error.code"]["stringValue"] == "429"
        assert "Resource exhausted" in attrs["error.message"]["stringValue"]

    def test_turn_carries_the_error_too(self, spans):
        attrs = _get_span_attrs(_by_kind(spans, "LLM")[0])
        assert attrs["error.code"]["stringValue"] == "429"

    def test_planner_that_produced_nothing_gets_no_span(self, spans):
        """Five planner responses, but one had no text, no reasoning, no calls."""
        assert len(_by_kind(spans, "CHAIN")) == 4

    def test_tool_only_planner_reports_its_calls_as_output(self, spans):
        """A planner with no text is not an empty span — the calls are its output."""
        payload = next(
            p
            for p in _by_kind(spans, "CHAIN")
            if "manage_task" in _get_span_attrs(p)["llm.output_messages"]["stringValue"]
        )
        messages = json.loads(_get_span_attrs(payload)["llm.output_messages"]["stringValue"])
        assert messages[0]["message.role"] == "assistant"
        assert "message.content" not in messages[0]
        call = messages[0]["message.tool_calls"][0]["tool_call.function"]
        assert call["name"] == "manage_task"
        assert json.loads(call["arguments"])["Action"] == "create"

    def test_model_comes_from_the_settings_change_block(self, spans):
        attrs = _get_span_attrs(_by_kind(spans, "LLM")[0])
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.6"
        assert attrs["antigravity.model_label"]["stringValue"] == "Claude Sonnet 4.6 (Thinking)"

    def test_thinking_is_captured(self, spans):
        payload = next(p for p in _by_kind(spans, "CHAIN") if "llm.reasoning" in _get_span_attrs(p))
        assert "Two things to check" in _get_span_attrs(payload)["llm.reasoning"]["stringValue"]

    def test_promoted_tool_arguments(self, spans):
        assert _get_span_attrs(self._tool(spans, "run_command"))["tool.command"]["stringValue"] == "make clean"
        assert _get_span_attrs(self._tool(spans, "view_file"))["tool.file_path"]["stringValue"] == "/w/Makefile"


# ---------------------------------------------------------------------------
# The model must survive turns that never mention it
# ---------------------------------------------------------------------------


class TestModelCarryForward:
    def test_model_persists_into_a_turn_that_never_names_it(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, state
    ):
        """The settings block appears only on the turn the user switched models.

        Without carry-forward three quarters of turns report no model at all.
        """
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": (
                        "<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` from None to "
                        "Gemini 3.7 Flash (High). No need to comment.</USER_SETTINGS_CHANGE>"
                        "<USER_REQUEST>first</USER_REQUEST>"
                    ),
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "first answer",
                },
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-06-09T16:00:10Z",
                    "content": "<USER_REQUEST>second</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:11Z",
                    "content": "second answer",
                },
            ],
        )
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        llm = _by_kind(captured_spans, "LLM")
        assert len(llm) == 2
        for payload in llm:
            assert _get_span_attrs(payload)["llm.model_name"]["stringValue"] == "gemini-3.7-flash"
        assert state.get("model_label") == "Gemini 3.7 Flash (High)"

    def test_settings_file_is_the_last_resort(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        """A session that never switched models still reports the selected one."""
        from tracing.antigravity import constants as _c

        data_dir = tmp_path / "cli-data"
        data_dir.mkdir()
        (data_dir / "settings.json").write_text(json.dumps({"model": "Gemini 3.6 Flash (Low)"}), encoding="utf-8")
        monkeypatch.setattr(_c, "CLI_DATA_DIR", data_dir)

        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "hello",
                },
            ],
        )
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        attrs = _get_span_attrs(_by_kind(captured_spans, "LLM")[0])
        assert attrs["llm.model_name"]["stringValue"] == "gemini-3.6-flash"


# ---------------------------------------------------------------------------
# Batching: the receiver's JSON body limit is 1 MB
# ---------------------------------------------------------------------------


class TestPayloadBatching:
    def test_a_turn_ships_in_one_payload_when_it_fits(self, trace_enabled, mock_resolve, mock_ensure, mock_gc):
        payloads = []
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
        }
        with (
            mock.patch(
                "tracing.antigravity.hooks.handlers._send_payload_async",
                side_effect=payloads.append,
            ),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))),
        ):
            stop()
        assert len(payloads) == 1
        assert len(_get_spans(payloads[0])) == 12

    def test_oversized_turns_split_below_the_body_limit(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, monkeypatch
    ):
        """One POST over the limit loses the whole turn; two POSTs lose nothing."""
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")
        records = [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-06-09T16:00:00Z",
                "content": "<USER_REQUEST>big</USER_REQUEST>",
            }
        ]
        for i in range(40):
            records.append(
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:01Z",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": f"echo {i}"}}],
                }
            )
            records.append(
                {
                    "type": "RUN_COMMAND",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:02Z",
                    "exit_code": 0,
                    "content": "x" * 40_000,
                }
            )
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(transcript, records)

        payloads = []
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with (
            mock.patch(
                "tracing.antigravity.hooks.handlers._send_payload_async",
                side_effect=payloads.append,
            ),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))),
        ):
            stop()

        assert len(payloads) > 1
        for payload in payloads:
            assert len(json.dumps(payload)) < 1_000_000
        total = sum(len(_get_spans(p)) for p in payloads)
        assert total == 81  # 1 turn + 40 planners + 40 tools


# ---------------------------------------------------------------------------
# The invariant the Traces page depends on
# ---------------------------------------------------------------------------


class TestOneTurnSpanPerTrace:
    """Consumers key "a turn" on the LLM span kind, not on being a root.

    `getSpanTypeFilter('parent')` in the LLM tracing controller resolves to
    `subtype IN ('chat','completion')` and the traces list has no GROUP BY, so
    every LLM-kind span in a trace becomes its own row. Claude Code and Codex
    emit one per turn and so look correct; any harness emitting one per model
    call does not. These tests fail if that ever regresses here.
    """

    @pytest.fixture
    def multi_call_spans(self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        records = [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-06-09T16:00:00Z",
                "content": "<USER_REQUEST>do a lot of things</USER_REQUEST>",
            }
        ]
        for i in range(12):
            records.append(
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": f"2026-06-09T16:00:{i * 2 + 1:02d}Z",
                    "content": f"step {i}",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": f"echo {i}"}}],
                }
            )
            records.append(
                {
                    "type": "RUN_COMMAND",
                    "source": "MODEL",
                    "created_at": f"2026-06-09T16:00:{i * 2 + 2:02d}Z",
                    "exit_code": 0,
                    "content": "ok",
                }
            )
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(transcript, records)
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return captured_spans

    def test_twelve_model_calls_are_still_one_turn(self, multi_call_spans):
        assert len(_by_kind(multi_call_spans, "LLM")) == 1
        assert len(_by_kind(multi_call_spans, "CHAIN")) == 12
        assert len(_by_kind(multi_call_spans, "TOOL")) == 12

    def test_the_single_llm_span_is_the_root(self, multi_call_spans):
        span = _get_span(_by_kind(multi_call_spans, "LLM")[0])
        assert not span.get("parentSpanId")
        assert span["name"] == "Turn 1"

    def test_no_descendant_is_llm_kind(self, multi_call_spans):
        """An LLM-kind span anywhere below the root would read as another turn."""
        for payload in multi_call_spans:
            span = _get_span(payload)
            if not span.get("parentSpanId"):
                continue
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert attrs["openinference.span.kind"]["stringValue"] != "LLM"

    def test_the_turn_carries_the_prompt_and_the_final_response(self, multi_call_spans, monkeypatch):
        """The row on the Traces page is the turn, so it must read as one."""
        attrs = _get_span_attrs(_by_kind(multi_call_spans, "LLM")[0])
        assert "do a lot of things" in attrs["input.value"]["stringValue"]
        assert attrs["output.value"]["stringValue"] == "step 11"


# ---------------------------------------------------------------------------
# Token counts land on the turn, and only on the turn
# ---------------------------------------------------------------------------


class TestTokenCounts:
    def _store(self, tmp_path, monkeypatch, conversation_id, blobs):
        import sqlite3

        from tracing.antigravity import constants as _c

        data_dir = tmp_path / "cli-data"
        monkeypatch.setattr(_c, "CLI_DATA_DIR", data_dir)
        path = data_dir / "conversations" / f"{conversation_id}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob)")
        for idx, blob in enumerate(blobs):
            conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (idx, blob))
        conn.commit()
        conn.close()

    @pytest.fixture
    def two_turn_spans(self, tmp_path, monkeypatch, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        from tests.tracing.antigravity.test_antigravity_usage import _usage_blob

        # Four model calls across two turns: 2 then 2.
        self._store(
            tmp_path,
            monkeypatch,
            "c-tok",
            [
                _usage_blob(1000, 10, 0),
                _usage_blob(2000, 20, 500),
                _usage_blob(3000, 30, 0),
                _usage_blob(4000, 40, 700),
            ],
        )
        records = []
        for turn in range(2):
            records.append(
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": f"2026-06-09T16:0{turn}:00Z",
                    "content": f"<USER_REQUEST>turn {turn}</USER_REQUEST>",
                }
            )
            for call in range(2):
                records.append(
                    {
                        "type": "PLANNER_RESPONSE",
                        "source": "MODEL",
                        "created_at": f"2026-06-09T16:0{turn}:0{call + 1}Z",
                        "content": f"turn {turn} call {call}",
                    }
                )
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(transcript, records)
        stdin_payload = {"conversationId": "c-tok", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return captured_spans

    def test_each_turn_sums_only_its_own_calls(self, two_turn_spans):
        turns = _by_kind(two_turn_spans, "LLM")
        assert len(turns) == 2
        first, second = (_get_span_attrs(t) for t in turns)
        # turn 0 = calls 0+1, turn 1 = calls 2+3
        assert first["llm.token_count.prompt"]["intValue"] == 1000 + 2000 + 500
        assert first["llm.token_count.completion"]["intValue"] == 30
        assert first["llm.token_count.prompt_details.cache_read"]["intValue"] == 500
        assert second["llm.token_count.prompt"]["intValue"] == 3000 + 4000 + 700
        assert second["llm.token_count.completion"]["intValue"] == 70
        assert second["llm.token_count.prompt_details.cache_read"]["intValue"] == 700

    def test_total_is_prompt_plus_completion(self, two_turn_spans):
        attrs = _get_span_attrs(_by_kind(two_turn_spans, "LLM")[0])
        assert attrs["llm.token_count.total"]["intValue"] == 3500 + 30

    def test_no_step_or_tool_span_repeats_the_counts(self, two_turn_spans):
        """The summary queries sum these columns over every span in the range."""
        for payload in two_turn_spans:
            span = _get_span(payload)
            if not span.get("parentSpanId"):
                continue
            for attr in span["attributes"]:
                assert not attr["key"].startswith("llm.token_count"), attr["key"]

    def test_absent_usage_emits_no_token_attributes_at_all(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        """A zero would read downstream as a turn that was priced and free."""
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "hello",
                },
            ],
        )
        stdin_payload = {"conversationId": "no-store", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        for payload in captured_spans:
            for attr in _get_span(payload)["attributes"]:
                assert not attr["key"].startswith("llm.token_count"), attr["key"]


# ---------------------------------------------------------------------------
# A turn interrupted by a background task
# ---------------------------------------------------------------------------


class TestBackgroundTaskPause:
    """Stop fires when the agent yields, and backgrounding a tool makes it yield.

    Treating that stop as "turn over" froze the turn at the pause: the watermark
    marked it emitted and every step after the resume was lost. Reproduced from a
    real session where 14 of 34 steps went missing from one turn.
    """

    def _records(self, *, resumed: bool):
        recs = [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-08-19T01:30:35Z",
                "content": "<USER_REQUEST>build it</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "created_at": "2026-08-19T01:30:39Z",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "npx vite build"}}],
            },
            # The backgrounded tool. This record is never updated.
            {
                "type": "GENERIC",
                "source": "MODEL",
                "status": "RUNNING",
                "created_at": "2026-08-19T01:32:07Z",
                "content": "Created At: 2026-08-19T01:32:07Z\nTool is running as a background task with task id 37",
            },
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "created_at": "2026-08-19T01:32:21Z",
                "content": "I have triggered the build. I will wait for it to complete.",
            },
        ]
        if resumed:
            recs += [
                # The wake-up, which is how the agent learns the task finished.
                {
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "created_at": "2026-08-19T01:33:29Z",
                    "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-08-19T01:33:33Z",
                    "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/w/out.txt"}}],
                },
                {
                    "type": "GENERIC",
                    "source": "MODEL",
                    "created_at": "2026-08-19T01:33:40Z",
                    "content": "Created At: 2026-08-19T01:33:40Z\nCompleted At: 2026-08-19T01:33:40Z\nbuild ok",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-08-19T01:33:45Z",
                    "content": "The build succeeded.",
                },
            ]
        return recs

    def _stop(self, transcript):
        stdin_payload = {"conversationId": "c-bg", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

    def test_stop_during_a_background_task_emits_nothing(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, state
    ):
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(transcript, self._records(resumed=False))
        self._stop(transcript)

        assert captured_spans == []
        # Crucially the watermark must not move, or the turn can never be emitted.
        assert state.get("last_emitted_turn") == "-1"

    def test_the_whole_turn_lands_once_the_task_reports_back(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, state
    ):
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(transcript, self._records(resumed=False))
        self._stop(transcript)
        assert captured_spans == []

        _write_jsonl(transcript, self._records(resumed=True))
        self._stop(transcript)

        assert len(_by_kind(captured_spans, "LLM")) == 1, "one turn, not two"
        # Every step, both sides of the pause.
        assert len(_by_kind(captured_spans, "CHAIN")) == 4
        names = sorted(_get_span(p)["name"] for p in _by_kind(captured_spans, "TOOL"))
        assert names == ["run_command", "view_file"]
        attrs = _get_span_attrs(_by_kind(captured_spans, "LLM")[0])
        assert attrs["output.value"]["stringValue"] == "The build succeeded."
        assert state.get("last_emitted_turn") == "0"

    def test_a_superseded_turn_is_emitted_even_though_it_never_settled(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        """If the user moves on, the turn will never report back — take it as is."""
        transcript = tmp_path / "transcript.jsonl"
        records = self._records(resumed=False) + [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-08-19T01:35:55Z",
                "content": "<USER_REQUEST>never mind</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "created_at": "2026-08-19T01:35:59Z",
                "content": "Stopping.",
            },
        ]
        _write_jsonl(transcript, records)
        self._stop(transcript)

        assert len(_by_kind(captured_spans, "LLM")) == 2

    def test_a_turn_with_no_background_task_still_emits_immediately(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        """The deferral must not delay the 79% of turns that never background anything."""
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-08-19T01:30:35Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "created_at": "2026-08-19T01:30:39Z",
                    "content": "hello",
                },
            ],
        )
        self._stop(transcript)
        assert len(_by_kind(captured_spans, "LLM")) == 1

    def test_user_interrupting_a_waiting_turn_rescues_it_on_the_next_invocation(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, state
    ):
        """Interrupting mid-wait must not strand the turn.

        The user typing again is what un-strands it: the waiting turn stops being
        the last one, so it is emitted as it stands rather than waiting for a
        settle that will never come. The realistic trigger is PreInvocation —
        it fires before the first model call of the new turn.
        """
        transcript = tmp_path / "transcript.jsonl"

        # The agent backgrounds a build, yields, and stops. Nothing is emitted.
        _write_jsonl(transcript, self._records(resumed=False))
        self._stop(transcript)
        assert captured_spans == []
        assert state.get("last_emitted_turn") == "-1"

        # The user interrupts with a new message instead of letting it finish.
        _write_jsonl(
            transcript,
            self._records(resumed=False)
            + [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "created_at": "2026-08-19T01:34:00Z",
                    "content": "<USER_REQUEST>stop, do it differently</USER_REQUEST>",
                }
            ],
        )
        stdin_payload = {"conversationId": "c-bg", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            pre_invocation()

        turns = _by_kind(captured_spans, "LLM")
        assert len(turns) == 1, "the abandoned turn is emitted, the new one is not"
        attrs = _get_span_attrs(turns[0])
        assert "build it" in attrs["input.value"]["stringValue"]
        assert state.get("last_emitted_turn") == "0"
