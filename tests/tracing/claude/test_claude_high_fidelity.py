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
from tracing.claude_code.hooks.tool_buffer import TRUNCATED_BODY, ToolBuffer
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

    def test_a_skipped_notification_does_not_lend_its_absorb_time_to_the_next_prompt(self, tmp_path):
        notification_absorbed = "2026-08-22T16:16:43.100Z"
        prompt_absorbed = "2026-08-22T16:16:43.900Z"
        transcript = _write(
            tmp_path / "t.jsonl",
            _WORK
            + [
                _absorb_marker(notification_absorbed),
                _queued_command("<task-notification/>", origin=None, mode="task-notification"),
                _absorb_marker(prompt_absorbed),
                _queued_command("stop", ts="2026-08-22T16:16:43.000Z"),
            ],
        )
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        prompts = [s for s in _spans(captured[0]) if s["name"].startswith("User prompt")]
        assert [_attrs(s)["input.value"] for s in prompts] == ["stop"]
        absorbed_ms = int(datetime.fromisoformat(prompt_absorbed.replace("Z", "+00:00")).timestamp() * 1000)
        assert prompts[0]["startTimeUnixNano"] == f"{absorbed_ms}000000"

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


class TestStopAckSurvivesTheDetachedSender:
    """Without fork() the POST runs in ``core.sender``, which cannot carry the
    ``_settle`` closure. Stop therefore also hands over an importable reference;
    replaying it through the sender must acknowledge the turn exactly as the
    in-process path does."""

    def _stop_capturing_ref(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        state = _live_turn_state(tmp_path)
        seen = {}

        def capture(span_dict, sender=None, on_success=None, on_success_ref=None):
            seen["payload"] = span_dict
            seen["ref"] = on_success_ref

        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: state),
            mock.patch.object(handlers, "send_span_async", capture),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(
                sys, "stdin", new=io.StringIO(json.dumps({"session_id": "s1", "transcript_path": str(transcript)}))
            ),
        ):
            handlers.stop()
        return state, seen

    def test_stop_passes_a_json_safe_reference_alongside_the_closure(self, tmp_path):
        state, seen = self._stop_capturing_ref(tmp_path)
        target, kwargs = seen["ref"]
        assert target == "tracing.claude_code.hooks.handlers:settle_exported_turn"
        json.dumps(kwargs)
        assert kwargs["trace_id"] == "a" * 32
        assert kwargs["state_file"] == str(state.state_file)
        assert state.get("current_trace_id") == "a" * 32, "not acked until the sender says so"

    def test_replaying_the_reference_through_the_sender_acks_the_turn(self, tmp_path):
        import core.sender as sender

        state, seen = self._stop_capturing_ref(tmp_path)
        target, kwargs = seen["ref"]
        envelope = tmp_path / "env.json"
        envelope.write_text(json.dumps({"payload": seen["payload"], "on_success": {"callable": target, "kwargs": kwargs}}))

        with mock.patch("core.common.send_span", return_value=True):
            assert sender.main([str(envelope)]) == 0

        assert state.get("current_trace_id") is None
        assert state.get("export_attempted_trace_id") is None
        assert handlers.ToolBuffer(state).get("tu1") is None

    def test_a_refused_send_leaves_the_turn_unacked(self, tmp_path):
        import core.sender as sender

        state, seen = self._stop_capturing_ref(tmp_path)
        target, kwargs = seen["ref"]
        envelope = tmp_path / "env.json"
        envelope.write_text(json.dumps({"payload": seen["payload"], "on_success": {"callable": target, "kwargs": kwargs}}))

        with mock.patch("core.common.send_span", return_value=False):
            assert sender.main([str(envelope)]) == 1

        assert state.get("current_trace_id") == "a" * 32
        assert state.get("export_attempted_trace_id") == "a" * 32


def _agent_launch(
    tool_use_id, agent_id, prompt="do X", ts_use="2026-08-22T16:16:42.920Z", ts_res="2026-08-22T16:16:43.000Z"
):
    """An Agent tool call whose result reports a background launch, as Claude Code writes it."""
    return [
        _assistant(
            f"u-{tool_use_id}",
            f"msg-{tool_use_id}",
            [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {"description": "Round", "prompt": prompt, "subagent_type": "general-purpose"},
                }
            ],
            usage={"input_tokens": 50, "output_tokens": 5},
            ts=ts_use,
        ),
        {
            "type": "user",
            "uuid": f"res-{tool_use_id}",
            "timestamp": ts_res,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "launched"}],
            },
            "toolUseResult": {"status": "async_launched", "agentId": agent_id},
        },
    ]


def _notification(tool_use_id):
    return (
        f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
        "<status>completed</status>\n<result>done</result>\n</task-notification>"
    )


class TestBackgroundSubagentsStayInTheirTrace:
    """A turn launches agents in the background and ends. Each agent's completion then
    arrives as a task-notification prompt with a fresh prompt id, and its SubagentStop
    fires with no turn open. Both must land in the trace that launched the agent, not
    open a new one - and the spans that enclose the launch (root, model call, Agent tool)
    must not be sent until the agent's real lifetime is known, or the chart cannot nest it."""

    @pytest.fixture
    def launched(self, tmp_path):
        # A Bash before the launch: it does not enclose the agent, so it ships at Stop.
        transcript = _write(tmp_path / "t.jsonl", _WORK + _agent_launch("toolu_A", "agent-A"))
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(
            handlers.stop,
            state,
            {"session_id": "s1", "transcript_path": str(transcript), "last_assistant_message": "launched"},
            captured,
        )
        return state, transcript, captured

    @staticmethod
    def _all_spans(captured):
        return [s for p in captured if p for s in _spans(p)]

    def test_enclosing_spans_are_held_and_the_rest_ship_at_stop(self, launched):
        state, _, captured = launched
        sent = self._all_spans(captured)
        assert sorted(s["name"] for s in sent) == ["Bash", "LLM call 1: claude-haiku-4-5-20251001"]
        held = json.loads(state.get("held_spans"))["a" * 32]
        assert sorted(s["name"] for s in held["spans"]) == ["Agent", "LLM call 2: claude-haiku-4-5-20251001", "Turn 1"]
        assert held["pending"] == ["agent-A"]
        assert state.get("current_trace_id") is None, "the turn is still acked"

    def test_launch_is_remembered_after_the_turn_is_acked(self, launched):
        state, _, _ = launched
        registry = json.loads(state.get("background_agents"))
        held_tool = next(s for s in json.loads(state.get("held_spans"))["a" * 32]["spans"] if s["name"] == "Agent")
        assert registry["agent-A"]["trace_id"] == "a" * 32
        assert registry["agent-A"]["tool_span_id"] == held_tool["spanId"]
        assert registry["agent-A"]["tool_use_id"] == "toolu_A"

    def _finish_agent(self, state, tmp_path, captured, output="fetched", ts="2026-08-22T16:17:10.000Z"):
        agent_transcript = _write(
            tmp_path / "agent.jsonl",
            [
                _assistant(
                    "au1",
                    "amsg_1",
                    [{"type": "tool_use", "id": "tu-a1", "name": "Bash", "input": {"command": "curl"}}],
                    usage={"input_tokens": 7, "output_tokens": 3},
                    ts=ts,
                ),
                _tool_result("tu-a1", ts=ts),
            ],
        )
        payload = {
            "session_id": "s1",
            "agent_id": "agent-A",
            "agent_type": "general-purpose",
            "agent_transcript_path": str(agent_transcript),
            "transcript_path": str(tmp_path / "t.jsonl"),
            "last_assistant_message": output,
        }
        _run_hook(handlers.subagent_stop, state, payload, captured)

    def test_subagent_stop_attaches_under_the_held_agent_tool_but_does_not_release(self, launched, tmp_path):
        """The agent's completion still has to come back as a notification whose reaction
        is the parent's own work; the ancestors wait for that, not for the SubagentStop."""
        state, _, captured = launched
        before = len(self._all_spans(captured))
        self._finish_agent(state, tmp_path, captured)
        sent = self._all_spans(captured)[before:]
        assert {s["traceId"] for s in sent} == {"a" * 32}
        agent = next(s for s in sent if s["name"].startswith("Subagent"))
        held = json.loads(state.get("held_spans"))["a" * 32]
        tool = next(s for s in held["spans"] if s["name"] == "Agent")
        assert agent["parentSpanId"] == tool["spanId"]
        assert _attrs(agent)["subagent.background"] == "true"
        assert _attrs(agent)["output.value"] == "fetched"
        assert not [s for s in sent if s["name"] == "Turn 1"], "root must still be held"
        assert held["pending"] == ["agent-A"]
        for name in ("Turn 1", "LLM call 2: claude-haiku-4-5-20251001", "Agent"):
            span = next(s for s in held["spans"] if s["name"] == name)
            assert int(span["endTimeUnixNano"]) >= int(agent["endTimeUnixNano"]), name

    def test_notification_prompt_continues_the_trace_instead_of_opening_one(self, launched, tmp_path):
        state, transcript, captured = launched
        self._finish_agent(state, tmp_path, captured)
        agent = next(s for s in json.loads(state.get("held_spans"))["a" * 32]["spans"] if s["name"] == "Agent")
        payload = {
            "session_id": "s1",
            "prompt": _notification("toolu_A"),
            "prompt_id": "p-notif",
            "source": "system",
            "transcript_path": str(transcript),
        }
        before = len(self._all_spans(captured))
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        assert len(self._all_spans(captured)) == before, "a notification must not emit anything"
        assert state.get("current_trace_id") == "a" * 32
        assert state.get("current_trace_span_id") == agent["spanId"]
        assert state.get("trace_count") == "1", "not a new turn"

        reaction = [
            _assistant(
                "r1",
                "rmsg_1",
                [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "tu-r1", "name": "Read", "input": {"file_path": "/x"}},
                ],
                usage={"input_tokens": 9, "output_tokens": 2},
                ts="2026-08-22T16:17:50.000Z",
            ),
            _tool_result("tu-r1", ts="2026-08-22T16:17:50.500Z"),
        ]
        with open(transcript, "a", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in reaction) + "\n")
        _run_hook(
            handlers.stop,
            state,
            {"session_id": "s1", "transcript_path": str(transcript), "last_assistant_message": "ok"},
            captured,
        )
        sent = self._all_spans(captured)[before:]
        assert {s["traceId"] for s in sent} == {"a" * 32}
        llm = next(
            s for s in sent if _attrs(s)["openinference.span.kind"] == "LLM" and s["parentSpanId"] == agent["spanId"]
        )
        read = next(
            s for s in sent if _attrs(s)["openinference.span.kind"] == "TOOL" and s["parentSpanId"] == llm["spanId"]
        )
        assert read
        # The last notification's reaction is what releases the ancestors, stretched over it.
        root = next(s for s in sent if s["name"] == "Turn 1")
        assert int(root["endTimeUnixNano"]) >= int(read["endTimeUnixNano"])
        assert state.get("current_trace_id") is None
        assert state.get("continuation_agent_id") is None
        assert "a" * 32 not in json.loads(state.get("held_spans") or "{}")

    def test_notification_reaction_hangs_off_the_agent_tool_not_the_subagent(self, launched, tmp_path):
        """The subagent span is already on the wire with its own end; the parent's reaction
        happens after it, so it parents to the Agent tool call, which is held and grows."""
        state, transcript, captured = launched
        self._finish_agent(state, tmp_path, captured)
        spans = self._all_spans(captured)
        tool = next(s for s in json.loads(state.get("held_spans"))["a" * 32]["spans"] if s["name"] == "Agent")
        sub = next(s for s in spans if s["name"].startswith("Subagent"))
        _run_hook(
            handlers.user_prompt_submit,
            state,
            {
                "session_id": "s1",
                "prompt": _notification("toolu_A"),
                "prompt_id": "p-notif",
                "source": "system",
                "transcript_path": str(transcript),
            },
            captured,
        )
        assert state.get("current_trace_span_id") == tool["spanId"]
        assert state.get("current_trace_span_id") != sub["spanId"]

    def test_notification_before_the_agent_stopped_lands_under_the_agent_tool(self, launched, tmp_path):
        state, transcript, captured = launched
        held_tool = next(s for s in json.loads(state.get("held_spans"))["a" * 32]["spans"] if s["name"] == "Agent")
        _run_hook(
            handlers.user_prompt_submit,
            state,
            {
                "session_id": "s1",
                "prompt": _notification("toolu_A"),
                "prompt_id": "p-notif",
                "source": "system",
                "transcript_path": str(transcript),
            },
            captured,
        )
        assert state.get("current_trace_span_id") == held_tool["spanId"]

    def test_session_end_releases_ancestors_of_an_agent_that_never_reported(self, launched, tmp_path):
        state, transcript, captured = launched
        before = len(self._all_spans(captured))
        _run_hook(handlers.session_end, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        sent = self._all_spans(captured)[before:]
        assert sorted(s["name"] for s in sent) == ["Agent", "LLM call 2: claude-haiku-4-5-20251001", "Turn 1"]

    def test_peer_message_from_a_running_agent_continues_the_trace(self, launched, tmp_path):
        """A subagent can message the parent before it finishes; that is a prompt with a
        fresh prompt id too, keyed by agent id rather than tool-use id."""
        state, transcript, captured = launched
        held_tool = next(s for s in json.loads(state.get("held_spans"))["a" * 32]["spans"] if s["name"] == "Agent")
        prompt = (
            "Another Claude session sent a message:\n"
            '<agent-message from="agent-A">\nStep 12: what next?\n</agent-message>\n'
        )
        _run_hook(
            handlers.user_prompt_submit,
            state,
            {
                "session_id": "s1",
                "prompt": prompt,
                "prompt_id": "p-peer",
                "source": "system",
                "transcript_path": str(transcript),
            },
            captured,
        )
        assert state.get("current_trace_id") == "a" * 32
        assert state.get("current_trace_span_id") == held_tool["spanId"]
        assert state.get("trace_count") == "1"

    def test_a_typed_prompt_mentioning_a_tool_use_id_is_still_a_turn(self, launched, tmp_path):
        state, transcript, captured = launched
        payload = {
            "session_id": "s1",
            "prompt": _notification("toolu_A"),
            "prompt_id": "p-2",
            "source": "user",
            "transcript_path": str(transcript),
        }
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        assert state.get("current_trace_id") != "a" * 32
        assert state.get("trace_count") == "2"

    def test_unknown_notification_falls_back_to_a_normal_turn(self, launched, tmp_path):
        state, transcript, captured = launched
        payload = {
            "session_id": "s1",
            "prompt": _notification("toolu_NEVER_SEEN"),
            "prompt_id": "p-2",
            "source": "system",
            "transcript_path": str(transcript),
        }
        _run_hook(handlers.user_prompt_submit, state, payload, captured)
        assert state.get("trace_count") == "2"
        assert state.get("continuation_agent_id") is None


class TestParentsOutlastTheirChildren:
    """A model call is stamped when its response lands; the tools it requested run after.
    The chart can only nest a child whose window sits inside its parent's, so every parent
    closes no earlier than its last descendant."""

    def test_model_call_ends_no_earlier_than_its_tool(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            [
                _assistant(
                    "u1",
                    "msg_A",
                    [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "sleep"}}],
                    ts="2026-08-22T16:16:42.000Z",
                ),
                _tool_result("tu1", ts="2026-08-22T16:16:49.000Z"),
            ],
        )
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        spans = _spans(captured[0])
        by_name = {s["name"]: s for s in spans}
        llm = next(s for n, s in by_name.items() if n.startswith("LLM call"))
        assert int(llm["endTimeUnixNano"]) >= int(by_name["Bash"]["endTimeUnixNano"])
        assert int(by_name["Turn 1"]["endTimeUnixNano"]) >= int(llm["endTimeUnixNano"])

    def test_continuation_after_every_agent_reported_still_stretches_the_root(self, tmp_path):
        """The last notification's reaction renders after the last SubagentStop; the held
        ancestors must wait for it rather than release on the agent count alone."""
        transcript = _write(tmp_path / "t.jsonl", _agent_launch("toolu_A", "agent-A"))
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        agent_transcript = _write(
            tmp_path / "agent.jsonl",
            [_assistant("au1", "amsg_1", [{"type": "text", "text": "hi"}], ts="2026-08-22T16:17:00.000Z")],
        )
        _run_hook(
            handlers.subagent_stop,
            state,
            {
                "session_id": "s1",
                "agent_id": "agent-A",
                "agent_type": "general-purpose",
                "agent_transcript_path": str(agent_transcript),
                "transcript_path": str(transcript),
            },
            captured,
        )
        _run_hook(
            handlers.user_prompt_submit,
            state,
            {
                "session_id": "s1",
                "prompt": _notification("toolu_A"),
                "prompt_id": "p-n",
                "source": "system",
                "transcript_path": str(transcript),
            },
            captured,
        )
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(_assistant("r1", "rmsg_1", [{"type": "text", "text": "ok"}], ts="2026-08-22T16:18:30.000Z"))
                + "\n"
            )
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        spans = [s for p in captured if p for s in _spans(p)]
        root = next(s for s in spans if s["name"] == "Turn 1")
        reaction = [
            s
            for s in spans
            if s["name"].startswith("LLM call") and "parentSpanId" in s and s["parentSpanId"] != root["spanId"]
        ]
        assert reaction, "the reaction model call was not emitted"
        assert int(root["endTimeUnixNano"]) >= max(int(s["endTimeUnixNano"]) for s in spans)


class TestAgentThatStoppedInsideTheTurnIsStillAwaited:
    """Two background agents. One finishes while the turn is still running - its SubagentStop
    is buffered and spliced into the turn at Stop. Its completion notification still comes
    back after Stop, and the reaction to it is the parent's work, so the ancestors wait for
    both notifications regardless of when each SubagentStop landed."""

    def test_spliced_agent_ships_in_the_turn_and_is_still_awaited(self, tmp_path):
        transcript = _write(
            tmp_path / "t.jsonl",
            _agent_launch(
                "toolu_FAST", "agent-fast", ts_use="2026-08-22T16:16:42.000Z", ts_res="2026-08-22T16:16:42.100Z"
            )
            + _agent_launch(
                "toolu_SLOW", "agent-slow", ts_use="2026-08-22T16:16:43.000Z", ts_res="2026-08-22T16:16:43.100Z"
            ),
        )
        state = _live_turn_state(tmp_path)
        captured = []
        fast_transcript = _write(
            tmp_path / "fast.jsonl",
            [_assistant("fu1", "fmsg", [{"type": "text", "text": "fast done"}], ts="2026-08-22T16:16:45.000Z")],
        )
        _run_hook(
            handlers.subagent_stop,
            state,
            {
                "session_id": "s1",
                "agent_id": "agent-fast",
                "agent_type": "general-purpose",
                "agent_transcript_path": str(fast_transcript),
                "transcript_path": str(transcript),
            },
            captured,
        )
        assert captured == [], "a foreground stop is buffered, not sent"
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        held = json.loads(state.get("held_spans"))["a" * 32]
        assert held["pending"] == ["agent-fast", "agent-slow"]
        registry = json.loads(state.get("background_agents"))
        assert registry["agent-fast"].get("subagent_span_id"), "spliced agent keeps its span id"
        sent_names = [s["name"] for p in captured if p for s in _spans(p)]
        assert "Subagent: general-purpose" in sent_names, "the fast agent shipped inside the turn"
        assert "Turn 1" not in sent_names

        for agent, tool_use in (("agent-fast", "toolu_FAST"), ("agent-slow", "toolu_SLOW")):
            _run_hook(
                handlers.user_prompt_submit,
                state,
                {
                    "session_id": "s1",
                    "prompt": _notification(tool_use),
                    "prompt_id": f"p-{agent}",
                    "source": "system",
                    "transcript_path": str(transcript),
                },
                captured,
            )
            _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        sent_names = [s["name"] for p in captured if p for s in _spans(p)]
        assert "Turn 1" in sent_names, "root released after the last notification"
        assert "a" * 32 not in json.loads(state.get("held_spans") or "{}")


class TestSubagentHooksDoNotContendOnTheSessionFile:
    """Ten background agents each fire PreToolUse/PostToolUse with the parent's session_id.
    Routing them all into the session file made every tool call rewrite one shared,
    ever-growing buffer under one lock; the agents' routine work stalled behind it."""

    def test_subagent_tool_hooks_land_in_the_agent_file_not_the_session_file(self, tmp_path, monkeypatch):
        import tracing.claude_code.hooks.adapter as adapter

        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        session = StateManager(tmp_path, tmp_path / "state_s1.json", tmp_path / ".lock_s1")
        session.init_state()
        session.set("session_id", "s1")
        payload = {
            "session_id": "s1",
            "agent_id": "agent-A",
            "tool_use_id": "tu-9",
            "tool_name": "Read",
            "tool_input": {"file_path": "/x"},
            "transcript_path": str(transcript),
        }
        with mock.patch.object(handlers, "resolve_session", lambda *a, **k: session):
            handlers._handle_pre_tool_use(payload)
            handlers._handle_post_tool_use({**payload, "tool_response": "ok"})
        assert session.get(ToolBuffer.STATE_KEY) in (None, "", "{}")
        agent_file = tmp_path / "state_s1__agent_agent-A.json"
        assert agent_file.is_file()
        assert "tu-9" in agent_file.read_text(encoding="utf-8")

    def test_large_tool_bodies_are_not_copied_into_state(self, tmp_path):
        state = _live_turn_state(tmp_path)
        big = "x" * 200_000
        buffer = ToolBuffer(state)
        buffer.record_start("tu-big", tool_name="Write", tool_input={"content": big}, started_at_ms=1)
        buffer.record_result("tu-big", status="success", tool_response=big, ended_at_ms=2)
        assert state.state_file.stat().st_size < 20_000
        obs = buffer.get("tu-big")
        assert obs.tool_input == TRUNCATED_BODY and obs.tool_response == TRUNCATED_BODY
        assert obs.started_at_ms == 1 and obs.ended_at_ms == 2 and obs.status == "success"

    def test_truncated_body_never_replaces_the_transcript_body(self, tmp_path):
        transcript = _write(tmp_path / "t.jsonl", _WORK)
        state = _live_turn_state(tmp_path)
        ToolBuffer(state).record_start("tu1", tool_name="Bash", tool_input={"command": "x" * 20_000}, started_at_ms=1)
        ToolBuffer(state).record_result("tu1", status="success", tool_response="y" * 20_000, ended_at_ms=2)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        bash = next(s for s in _spans(captured[0]) if s["name"] == "Bash")
        assert TRUNCATED_BODY not in json.dumps(bash)
        assert _attrs(bash).get("tool.command") == "ls"

    def test_agent_observations_are_applied_at_export_and_the_agent_file_is_dropped(self, tmp_path, monkeypatch):
        import tracing.claude_code.hooks.adapter as adapter

        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
        transcript = _write(tmp_path / "t.jsonl", _agent_launch("toolu_A", "agent-A"))
        state = _live_turn_state(tmp_path)
        captured = []
        _run_hook(handlers.stop, state, {"session_id": "s1", "transcript_path": str(transcript)}, captured)
        agent_transcript = _write(
            tmp_path / "agent.jsonl",
            [
                _assistant(
                    "au1",
                    "amsg",
                    [{"type": "tool_use", "id": "tu-a1", "name": "Bash", "input": {"command": "curl"}}],
                    ts="2026-08-22T16:17:00.000Z",
                ),
                _tool_result("tu-a1", ts="2026-08-22T16:17:01.000Z"),
            ],
        )
        tool_payload = {
            "session_id": "s1",
            "agent_id": "agent-A",
            "tool_use_id": "tu-a1",
            "tool_name": "Bash",
            "tool_input": {"command": "curl"},
            "transcript_path": str(agent_transcript),
        }
        with mock.patch.object(handlers, "resolve_session", lambda *a, **k: state):
            handlers._handle_pre_tool_use(tool_payload)
            handlers._handle_post_tool_use_failure({**tool_payload, "tool_response": "boom", "error": "exit 7"})
        agent_file = tmp_path / "state_s1__agent_agent-A.json"
        assert agent_file.is_file()
        _run_hook(
            handlers.subagent_stop,
            state,
            {
                "session_id": "s1",
                "agent_id": "agent-A",
                "agent_type": "general-purpose",
                "agent_transcript_path": str(agent_transcript),
                "transcript_path": str(transcript),
            },
            captured,
        )
        bash = next(
            s
            for p in captured
            if p
            for s in _spans(p)
            if s["name"] == "Bash" and _attrs(s).get("tool.call.id") == "tu-a1"
        )
        assert bash["status"]["code"] == 2, "the agent's own hook observed the failure"
        assert not agent_file.exists(), "consumed at export"

    def test_buffer_never_exceeds_the_observation_ceiling(self, tmp_path):
        from tracing.claude_code.hooks.tool_buffer import MAX_OBSERVATIONS

        state = _live_turn_state(tmp_path)
        buffer = ToolBuffer(state)
        for i in range(MAX_OBSERVATIONS + 50):
            buffer.record_result(f"tu-{i:04d}", status="success", tool_response="ok", ended_at_ms=i)
        kept = buffer.all()
        assert len(kept) == MAX_OBSERVATIONS
        assert min(o.ended_at_ms for o in kept) == 50, "the oldest are the ones evicted"
