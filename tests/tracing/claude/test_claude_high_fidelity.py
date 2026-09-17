#!/usr/bin/env python3
"""High-fidelity Claude Code trace reconstruction: Turn -> LLM call -> Tool.

Pins the shape and the invariants that are invisible in the read path: nothing
downstream validates them, and nothing will report when they break.
"""

import io
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import tracing.claude_code.hooks.handlers as handlers
from core.common import StateManager
from core.event_model import EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks.transcript import parse_claude_transcript


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    for var in ("ATATUS_LOG_PROMPTS", "ATATUS_LOG_TOOL_CONTENT", "ATATUS_LOG_TOOL_DETAILS"):
        monkeypatch.setenv(var, "true")


def _write(path: Path, records: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _assistant(uuid, message_id, blocks, usage=None, model="claude-haiku-4-5-20251001", ts="2026-08-22T16:16:42.920Z"):
    message = {"role": "assistant", "id": message_id, "model": model, "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "message": message}


def _tool_result(tool_use_id, content="ok", ts="2026-08-22T16:16:43.000Z", is_error=False):
    return {
        "type": "user",
        "uuid": f"res-{tool_use_id}",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}],
        },
    }


def _root():
    return TurnEvent(
        event_id="turn:1",
        session_id="s1",
        turn_id="1",
        sequence=0,
        started_at_ms=0,
        ended_at_ms=0,
        status=EventStatus.COMPLETED,
    )


class TestOneModelCallPerMessageId:
    """Claude v2 splits one response across records; each must fold into one span."""

    def test_split_records_collapse_to_a_single_model_call(self, tmp_path):
        # Same message.id, three records: thinking, text, tool_use.
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [{"type": "thinking", "thinking": "hmm"}],
                    usage={"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 100},
                ),
                _assistant(
                    "u2",
                    "msg_A",
                    [{"type": "text", "text": "hello"}],
                    usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
                ),
                _assistant(
                    "u3",
                    "msg_A",
                    [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
                    usage={"input_tokens": 10, "output_tokens": 9, "cache_read_input_tokens": 100},
                ),
                _tool_result("toolu_1"),
            ],
        )
        graph = parse_claude_transcript(transcript, _root())
        calls = [e for e in graph.events if isinstance(e, ModelCallEvent)]
        tools = [e for e in graph.events if isinstance(e, ToolEvent)]

        assert len(calls) == 1, "one message.id must produce exactly one LLM span"
        assert len(tools) == 1
        # Usage is the fullest single record, never the sum -- summing multi-counts a
        # single API response and inflates cost by a clean multiple.
        assert calls[0].usage.output_tokens == 9
        assert calls[0].usage.cache_read_tokens == 100

    def test_distinct_message_ids_stay_distinct(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant("u1", "msg_A", [{"type": "text", "text": "a"}]),
                _assistant("u2", "msg_B", [{"type": "text", "text": "b"}]),
            ],
        )
        graph = parse_claude_transcript(transcript, _root())
        assert len([e for e in graph.events if isinstance(e, ModelCallEvent)]) == 2


class TestToolParentage:
    def test_every_tool_hangs_off_its_model_call_not_the_turn(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "/x"}},
                    ],
                ),
                _tool_result("toolu_1"),
                _tool_result("toolu_2"),
            ],
        )
        root = _root()
        graph = parse_claude_transcript(transcript, root)
        call = next(e for e in graph.events if isinstance(e, ModelCallEvent))
        tools = [e for e in graph.events if isinstance(e, ToolEvent)]

        assert len(tools) == 2
        assert all(t.parent_event_id == call.event_id for t in tools)
        assert not [t for t in tools if t.parent_event_id == root.event_id], "no orphaned tools"

    def test_a_tool_with_no_hook_report_is_still_captured(self, tmp_path):
        """The reason coverage went 19/21 -> 21/21: hooks miss tools, transcripts do not."""
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [
                        {"type": "tool_use", "id": "toolu_1", "name": "WebFetch", "input": {"url": "http://x"}},
                    ],
                ),
                _tool_result("toolu_1"),
            ],
        )
        graph = parse_claude_transcript(transcript, _root())
        tools = [e for e in graph.events if isinstance(e, ToolEvent)]
        assert [t.tool_name for t in tools] == ["WebFetch"]

    def test_failed_tool_result_marks_the_event_failed(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "nope"}},
                    ],
                ),
                _tool_result("toolu_1", content="boom", is_error=True),
            ],
        )
        graph = parse_claude_transcript(transcript, _root())
        tool = next(e for e in graph.events if isinstance(e, ToolEvent))
        assert tool.status is EventStatus.FAILED
        assert tool.error


class TestUsageContract:
    def test_prompt_is_cache_inclusive_and_the_one_hour_split_survives(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [{"type": "text", "text": "x"}],
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 497,
                        "cache_read_input_tokens": 37086,
                        "cache_creation_input_tokens": 297,
                        "cache_creation": {"ephemeral_1h_input_tokens": 297},
                    },
                ),
            ],
        )
        graph = parse_claude_transcript(transcript, _root())
        usage = next(e for e in graph.events if isinstance(e, ModelCallEvent)).usage
        # Matches Arize's LLM call 1 for this trace exactly.
        assert usage.prompt_tokens == 37393
        assert usage.cache_read_tokens == 37086
        assert usage.cache_write_tokens == 297
        # Anthropic bills the 1-hour tier at 2x input; losing it under-prices by ~7%.
        assert usage.cache_write_1h_tokens == 297


class TestRenderedShape:
    """Drives the real Stop entry point, not the renderer in isolation."""

    @pytest.fixture
    def sent(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                    ],
                    usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
                ),
                _tool_result("toolu_1"),
                _assistant(
                    "u2", "msg_B", [{"type": "text", "text": "done"}], usage={"input_tokens": 12, "output_tokens": 7}
                ),
            ],
        )
        state = StateManager(tmp_path, tmp_path / "s.json", tmp_path / "s.lock")
        state.init_state()
        for key, value in {
            "session_id": "s1",
            "user_id": "dg",
            "trace_count": "1",
            "current_trace_id": "a" * 32,
            "current_trace_span_id": "b" * 16,
            "current_trace_start_time": "1",
            "current_trace_prompt": "go",
            "trace_start_line": "0",
        }.items():
            state.set(key, value)

        captured = []
        payload = {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)}
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
            mock.patch.object(handlers, "send_span", lambda p: captured.append(p) or True),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            handlers.stop()
        assert captured, "Stop sent nothing"
        return captured[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]

    @staticmethod
    def _attrs(span):
        return {a["key"]: list(a["value"].values())[0] for a in span["attributes"]}

    def test_turn_is_the_only_chain_root_and_model_calls_are_llm(self, sent):
        kinds = [self._attrs(s)["openinference.span.kind"] for s in sent]
        roots = [s for s in sent if "parentSpanId" not in s]
        assert len(roots) == 1
        assert self._attrs(roots[0])["openinference.span.kind"] == "CHAIN"
        assert roots[0]["name"] == "Turn 1"
        assert kinds.count("LLM") == 2
        assert kinds.count("TOOL") == 1

    def test_model_call_names_carry_their_ordinal(self, sent):
        """Guards the UI span bucketer: useSpanBuckets collapses 3+ adjacent same-name
        siblings and re-parents their children to depth 0. Identical names would make
        this whole port render as flat as before it."""
        names = [s["name"] for s in sent if self._attrs(s)["openinference.span.kind"] == "LLM"]
        assert names[0].startswith("LLM call 1")
        assert names[1].startswith("LLM call 2")
        assert len(set(names)) == len(names)

    def test_tools_are_parented_to_a_model_call(self, sent):
        by_id = {s["spanId"]: s for s in sent}
        tools = [s for s in sent if self._attrs(s)["openinference.span.kind"] == "TOOL"]
        assert tools
        for tool in tools:
            parent = by_id[tool["parentSpanId"]]
            assert self._attrs(parent)["openinference.span.kind"] == "LLM"

    def test_correlation_attributes_are_on_every_span(self, sent):
        for span in sent:
            attrs = self._attrs(span)
            assert attrs.get("turn.id") == "1"
            assert attrs.get("user.id") == "dg"
            assert attrs.get("session.id") == "s1"

    def test_tool_call_id_and_message_id_are_emitted(self, sent):
        tools = [self._attrs(s) for s in sent if self._attrs(s)["openinference.span.kind"] == "TOOL"]
        llms = [self._attrs(s) for s in sent if self._attrs(s)["openinference.span.kind"] == "LLM"]
        assert all(a.get("tool.call.id") for a in tools)
        assert all(a.get("llm.message.id") for a in llms)

    def test_root_span_id_is_reused_from_state(self, sent):
        """A retry must not publish the turn a second time under fresh span IDs."""
        roots = [s for s in sent if "parentSpanId" not in s]
        assert roots[0]["spanId"] == "b" * 16

    def test_tokens_live_on_the_model_calls(self, sent):
        root = next(s for s in sent if "parentSpanId" not in s)
        assert "llm.token_count.total" not in self._attrs(root)
        llms = [self._attrs(s) for s in sent if self._attrs(s)["openinference.span.kind"] == "LLM"]
        assert sum(int(a["llm.token_count.total"]) for a in llms) == (110 + 5) + (12 + 7)


class TestLegacyFallback:
    """A transcript with no stable assistant UUIDs must stay one flat LLM span."""

    def test_v1_transcript_falls_back_to_a_single_llm_span(self, tmp_path):
        # No `uuid` on the assistant record -> assistant-line-* ids -> gate fails.
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-22T16:16:42.920Z",
                    "message": {
                        "role": "assistant",
                        "id": "msg_A",
                        "model": "m",
                        "content": [{"type": "text", "text": "hi"}],
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        state = StateManager(tmp_path, tmp_path / "s.json", tmp_path / "s.lock")
        state.init_state()
        for key, value in {
            "session_id": "s1",
            "trace_count": "1",
            "current_trace_id": "a" * 32,
            "current_trace_span_id": "b" * 16,
            "current_trace_start_time": "1",
            "current_trace_prompt": "go",
            "trace_start_line": "0",
        }.items():
            state.set(key, value)

        captured = []
        payload = {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)}
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
            mock.patch.object(handlers, "send_span", lambda p: captured.append(p) or True),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            handlers.stop()

        spans = captured[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = {a["key"]: list(a["value"].values())[0] for a in spans[0]["attributes"]}
        assert attrs["openinference.span.kind"] == "LLM"
        assert spans[0]["name"] == "Turn 1"


class TestAbandonedTurnIsNotASuccess:
    """A live turn closed with no transcript to replay is the one case nothing can vouch
    for. Reporting it as a success would make it indistinguishable from a clean turn in
    every rollup, so it stays an error - and stays marked, so it is never mistaken for a
    turn that ran and genuinely failed."""

    @pytest.fixture
    def failsafe_span(self, tmp_path):
        state = StateManager(tmp_path, tmp_path / "s.json", tmp_path / "s.lock")
        state.init_state()
        for key, value in {
            "session_id": "s1",
            "trace_count": "1",
            "current_trace_id": "a" * 32,
            "current_trace_span_id": "b" * 16,
            "current_trace_start_time": "1",
            "current_trace_prompt": "go",
        }.items():
            state.set(key, value)

        captured = []
        payload = {"session_id": "s1", "prompt": "next turn", "cwd": str(tmp_path)}
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
            mock.patch.object(handlers, "send_span", lambda p: captured.append(p) or True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            handlers.user_prompt_submit()
        assert captured, "fail-safe emitted nothing for the abandoned turn"
        return captured[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    def test_status_is_not_ok(self, failsafe_span):
        # 1 is OK. An abandoned turn must not claim it.
        assert failsafe_span["status"]["code"] != 1
        assert failsafe_span["status"].get("message")

    def test_marked_incomplete_so_it_is_distinguishable_from_a_real_failure(self, failsafe_span):
        attrs = {a["key"]: list(a["value"].values())[0] for a in failsafe_span["attributes"]}
        assert attrs.get("turn.incomplete") == "true"

    def test_reason_is_abandoned(self, failsafe_span):
        attrs = {a["key"]: list(a["value"].values())[0] for a in failsafe_span["attributes"]}
        assert attrs.get("turn.end_reason") == "abandoned"


def _interrupt_marker(ts="2026-08-22T16:16:44.000Z"):
    return {
        "type": "user",
        "uuid": "int-1",
        "timestamp": ts,
        "promptId": "p-1",
        "interruptedMessageId": "msg_A",
        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
    }


def _queued_command(prompt, origin="human", ts="2026-08-22T16:16:43.500Z", mode="prompt"):
    attachment = {"type": "queued_command", "prompt": prompt, "commandMode": mode, "timestamp": ts}
    if origin is not None:
        attachment["origin"] = {"kind": origin}
    return {"type": "attachment", "uuid": f"att-{abs(hash(prompt))}", "timestamp": ts, "attachment": attachment}


def _absorb_marker(ts):
    return {"type": "queue-operation", "operation": "remove", "reason": "absorbed_mid_turn", "timestamp": ts}


def _live_turn_state(tmp_path, prompt_id="p-1"):
    state = StateManager(tmp_path, tmp_path / "s.json", tmp_path / "s.lock")
    state.init_state()
    for key, value in {
        "session_id": "s1",
        "user_id": "dg",
        "trace_count": "1",
        "current_trace_id": "a" * 32,
        "current_trace_span_id": "b" * 16,
        "current_trace_start_time": "1",
        "current_trace_prompt": "go",
        "current_prompt_id": prompt_id,
        "trace_start_line": "0",
    }.items():
        state.set(key, value)
    return state


def _run_hook(entry, state, payload, captured):
    with (
        mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
        mock.patch.object(handlers, "send_span", lambda p: captured.append(p) or True),
        mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
        mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
    ):
        entry()


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span):
    return {a["key"]: list(a["value"].values())[0] for a in span["attributes"]}


def _root_span(spans):
    return next(s for s in spans if "parentSpanId" not in s)


_WORK = [
    _assistant(
        "u1",
        "msg_A",
        [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
        ],
        usage={"input_tokens": 100, "output_tokens": 5},
    ),
    _tool_result("tu1"),
]


class TestInterruptedTurnIsReplayedNotStubbed:
    """Stop never fires when the user interrupts, so the next prompt closes the turn. It
    must ship what the turn actually did, and interrupting is not a failure."""

    @pytest.fixture
    def closed(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK + [_interrupt_marker()])
        state = _live_turn_state(tmp_path)
        captured = []
        payload = {
            "session_id": "s1",
            "prompt": "next turn",
            "prompt_id": "p-2",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
        }
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        assert len(captured) == 1, "exactly one export for the interrupted turn"
        return state, _spans(captured[0])

    def test_root_is_ok_and_marked_interrupted(self, closed):
        _, spans = closed
        root = _root_span(spans)
        assert root["status"]["code"] == 1
        attrs = _attrs(root)
        assert attrs["turn.end_reason"] == "interrupted"
        assert attrs["turn.incomplete"] == "true"
        assert attrs["prompt.id"] == "p-1"

    def test_root_keeps_its_reserved_ids_so_children_stay_attached(self, closed):
        _, spans = closed
        root = _root_span(spans)
        assert root["spanId"] == "b" * 16
        assert root["traceId"] == "a" * 32

    def test_real_model_call_and_tool_are_shipped(self, closed):
        _, spans = closed
        kinds = [_attrs(s)["openinference.span.kind"] for s in spans]
        assert kinds.count("LLM") == 1
        assert kinds.count("TOOL") == 1
        llm = next(s for s in spans if _attrs(s)["openinference.span.kind"] == "LLM")
        assert int(_attrs(llm)["llm.token_count.total"]) == 105

    def test_next_turn_starts_fresh(self, closed):
        state, _ = closed
        assert state.get("current_trace_id") != "a" * 32
        assert state.get("current_prompt_id") == "p-2"
        assert state.get("trace_count") == "2"
        assert state.get("export_attempted_trace_id") is None
        assert state.get("high_fidelity_span_ids") is None


class TestContinuedTurn:
    def test_new_prompt_with_no_abort_evidence_is_continued_not_error(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        state = _live_turn_state(tmp_path)
        captured = []
        payload = {"session_id": "s1", "prompt": "next", "prompt_id": "p-2", "transcript_path": str(transcript)}
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        root = _root_span(_spans(captured[0]))
        assert root["status"]["code"] == 1
        assert _attrs(root)["turn.end_reason"] == "continued"
        assert _attrs(root)["turn.incomplete"] == "true"


class TestAbsorbedPromptBelongsToTheTurn:
    """A message typed mid-turn that the harness absorbs fires no hook. The transcript is
    the only place it exists, so the turn that ran it must carry it."""

    @pytest.fixture
    def stopped(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK[:1] + [_queued_command("also do Y")] + _WORK[1:])
        state = _live_turn_state(tmp_path)
        captured = []
        payload = {"session_id": "s1", "transcript_path": str(transcript), "last_assistant_message": "done"}
        _run_hook(handlers.stop, state, payload, captured)
        assert len(captured) == 1
        return state, _spans(captured[0])

    def test_one_trace_with_both_prompts(self, stopped):
        _, spans = stopped
        assert len({s["traceId"] for s in spans}) == 1
        root = _root_span(spans)
        attrs = _attrs(root)
        assert attrs["input.value"] == "go\n\nalso do Y"
        assert attrs["turn.prompt_count"] == "2"
        assert attrs["turn.end_reason"] == "completed"
        assert "turn.incomplete" not in attrs
        assert root["status"]["code"] == 1

    def test_absorbed_prompt_is_its_own_span_under_the_turn(self, stopped):
        _, spans = stopped
        root = _root_span(spans)
        prompts = [s for s in spans if s["name"].startswith("User prompt")]
        assert [s["name"] for s in prompts] == ["User prompt 2"]
        assert prompts[0]["parentSpanId"] == root["spanId"]
        assert _attrs(prompts[0])["input.value"] == "also do Y"
        assert _attrs(prompts[0])["openinference.span.kind"] == "CHAIN"
        assert prompts[0]["startTimeUnixNano"] == prompts[0]["endTimeUnixNano"] != "0000000"

    def test_prompt_is_placed_where_the_harness_absorbed_it_not_where_it_was_typed(self, tmp_path):
        typed_at = "2026-08-22T16:16:42.000Z"
        absorbed_at = "2026-08-22T16:16:43.900Z"
        transcript = _write(
            tmp_path / "t.jsonl",
            _WORK + [_absorb_marker(absorbed_at), _queued_command("also do Y", ts=typed_at)],
        )
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        spans = _spans(captured[0])
        prompt = next(s for s in spans if s["name"] == "User prompt 2")
        tool = next(s for s in spans if _attrs(s)["openinference.span.kind"] == "TOOL")
        assert int(prompt["startTimeUnixNano"]) > int(tool["endTimeUnixNano"])
        absorbed_ms = int(datetime.fromisoformat(absorbed_at.replace("Z", "+00:00")).timestamp() * 1000)
        assert prompt["startTimeUnixNano"] == f"{absorbed_ms}000000"

    def test_state_is_acknowledged(self, stopped):
        state, _ = stopped
        assert state.get("current_trace_id") is None
        assert state.get("export_attempted_trace_id") is None

    @pytest.mark.parametrize(
        "record",
        [
            _queued_command("<task-notification/>", origin="system"),
            # The real shape: no origin at all, commandMode says what it is.
            _queued_command("<task-notification/>", origin=None, mode="task-notification"),
        ],
    )
    def test_machine_queued_command_is_not_a_prompt(self, tmp_path, record):
        transcript = _write(tmp_path / "t.jsonl", _WORK + [record])
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        spans = _spans(captured[0])
        attrs = _attrs(_root_span(spans))
        assert attrs["input.value"] == "go"
        assert "turn.prompt_count" not in attrs
        assert not [s for s in spans if s["name"].startswith("User prompt")]


class TestSessionEndClosesTheLiveTurn:
    def test_last_turn_of_a_session_is_not_lost(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.session_end, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        assert len(captured) == 1
        root = _root_span(_spans(captured[0]))
        assert root["spanId"] == "b" * 16
        assert root["status"]["code"] == 1
        assert _attrs(root)["turn.end_reason"] == "interrupted"


class TestStopMarksTheExportBeforeSending:
    def test_a_refused_export_is_never_re_emitted_as_a_second_root(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        state = _live_turn_state(tmp_path)
        refused = []
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
            mock.patch.object(handlers, "send_span", lambda p: refused.append(p) or False),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(
                sys, "stdin", new=io.StringIO(json.dumps({"session_id": "s1", "transcript_path": str(transcript)}))
            ),
        ):
            handlers.stop()
        assert len(refused) == 1
        assert state.get("current_trace_id") == "a" * 32, "un-acked on refusal"
        assert state.get("export_attempted_trace_id") == "a" * 32

        captured = []
        payload = {"session_id": "s1", "prompt": "next", "prompt_id": "p-2", "transcript_path": str(transcript)}
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        assert captured == []
        assert state.get("current_trace_id") != "a" * 32
        assert state.get("trace_count") == "2"
