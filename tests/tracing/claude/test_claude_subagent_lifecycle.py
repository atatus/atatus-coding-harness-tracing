#!/usr/bin/env python3
"""Background, nested, forked and hook-less subagents land in one trace, each event once."""

import io
import json
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers
from core.common import StateManager
from core.event_model import AgentEvent, EventStatus, ModelCallEvent, ToolEvent
from tracing.claude_code.hooks.transcript import parse_claude_transcript

TRACE = "c" * 32
ROOT = "d" * 16


class Session:
    """Drives the real hook entry points against one temp session, capturing every send."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self.state = StateManager(tmp_path, tmp_path / "state_s1.json", tmp_path / ".lock_s1")
        self.state.init_state()
        self.state.set("session_id", "s1")
        self.state.set("trace_count", "1")
        self.payloads: list[dict] = []
        self.sender = None

    def hook(self, entry, payload: dict) -> None:
        payload = {"session_id": "s1", **payload}
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: self.state),
            mock.patch.object(handlers, "send_span", self.sender or (lambda p: self.payloads.append(p) or True)),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            entry()

    def spans(self) -> list[dict]:
        return [s for p in self.payloads for rs in p["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]

    def register_background(self, agent_id: str, tool_span_id: str = "e" * 16) -> None:
        self.state.set(
            "background_agents",
            json.dumps({agent_id: {"trace_id": TRACE, "tool_span_id": tool_span_id, "turn_id": "1"}}),
        )


def _attrs(span):
    return {a["key"]: list(a["value"].values())[0] for a in span["attributes"]}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "0")
    return Session(tmp_path)


def _write(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _assistant(uuid, message_id, blocks, ts="2026-11-14T03:33:45.000Z"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "assistant", "id": message_id, "model": "m", "content": blocks},
    }


def _tool_result(uuid, tool_use_id, content="ok", ts="2026-11-14T03:33:46.000Z"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
    }


def _tool_use(call_id, name="Bash", tool_input=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input or {}}


def _agent_root(agent_id="a1"):
    return AgentEvent(
        event_id=f"agent:{agent_id}",
        session_id="s1",
        turn_id="1",
        sequence=0,
        started_at_ms=None,
        ended_at_ms=None,
        status=EventStatus.COMPLETED,
        agent_id=agent_id,
        source_id="fork",
    )


def _fork_transcript(path: Path, launch_id="toolu_launch") -> Path:
    """A fork of a subagent: the parent's records, the copied launching call and its
    result, then the fork's own work."""
    return _write(
        path,
        [
            _assistant("p1", "msg_parent_1", [_tool_use("toolu_parent_bash")]),
            _tool_result("p2", "toolu_parent_bash"),
            _assistant("fork-copy", "msg_parent_2", [_tool_use(launch_id, "Agent", {"subagent_type": "fork"})]),
            _tool_result("fork-started", launch_id, [{"type": "text", "text": "Fork started — that fork is you."}]),
            _assistant("own1", "msg_own_1", [_tool_use("toolu_own_bash")], ts="2026-11-14T03:34:00.000Z"),
            _tool_result("own2", "toolu_own_bash", ts="2026-11-14T03:34:01.000Z"),
            _assistant("own3", "msg_own_2", [{"type": "text", "text": "done"}], ts="2026-11-14T03:34:02.000Z"),
        ],
    )


def _events(graph, kind):
    return [e for e in graph.events if isinstance(e, kind)]


class TestForkContextIsNotRenderedAgain:
    def test_a_fork_renders_only_its_own_work(self, tmp_path):
        graph = parse_claude_transcript(
            _fork_transcript(tmp_path / "f.jsonl"), _agent_root(), skip_through_tool_use_id="toolu_launch"
        )
        assert [e.event_id for e in _events(graph, ModelCallEvent)] == ["own1", "own3"]
        assert [e.tool_call_id for e in _events(graph, ToolEvent)] == ["toolu_own_bash"]

    def test_without_a_cut_every_record_is_rendered(self, tmp_path):
        graph = parse_claude_transcript(_fork_transcript(tmp_path / "f.jsonl"), _agent_root())
        assert len(_events(graph, ModelCallEvent)) == 4

    def test_an_id_that_never_appears_skips_nothing(self, tmp_path):
        graph = parse_claude_transcript(
            _fork_transcript(tmp_path / "f.jsonl"), _agent_root(), skip_through_tool_use_id="toolu_absent"
        )
        assert len(_events(graph, ModelCallEvent)) == 4
        assert any(d.code == "fork_context_boundary_missing" for d in graph.diagnostics)


class TestForkCutComesFromAgentMeta:
    def test_a_fork_is_cut_at_its_launching_call(self, tmp_path):
        transcript = _write(tmp_path / "agent-a1.jsonl", [])
        transcript.with_suffix(".meta.json").write_text(
            json.dumps({"agentType": "fork", "isFork": True, "toolUseId": "toolu_launch"}), encoding="utf-8"
        )
        assert handlers._fork_cut_tool_use_id(transcript) == "toolu_launch"

    def test_a_non_fork_is_not_cut(self, tmp_path):
        transcript = _write(tmp_path / "agent-a1.jsonl", [])
        transcript.with_suffix(".meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "toolUseId": "toolu_launch"}), encoding="utf-8"
        )
        assert handlers._fork_cut_tool_use_id(transcript) is None

    def test_missing_meta_is_not_cut(self, tmp_path):
        assert handlers._fork_cut_tool_use_id(tmp_path / "agent-a1.jsonl") is None

    def test_a_background_fork_export_sends_no_copied_record(self, session):
        transcript = _fork_transcript(session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl")
        transcript.with_suffix(".meta.json").write_text(
            json.dumps({"agentType": "fork", "isFork": True, "toolUseId": "toolu_launch"}), encoding="utf-8"
        )
        session.register_background("a1")

        session.hook(
            handlers.subagent_stop,
            {
                "hook_event_name": "SubagentStop",
                "agent_id": "a1",
                "agent_type": "fork",
                "agent_transcript_path": str(transcript),
            },
        )

        message_ids = {_attrs(s).get("llm.message.id") for s in session.spans()} - {None}
        assert message_ids == {"msg_own_1", "msg_own_2"}


def _held_span(span_id, name, parent="", start_ms=1794627100000, end_ms=1794627110000):
    span = {
        "traceId": TRACE,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": f"{start_ms}000000",
        "endTimeUnixNano": f"{end_ms}000000",
        "attributes": [],
        "status": {"code": 1},
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def _hold_launching_turn(session, agent_id="a1", tool_span_id="e" * 16):
    session.register_background(agent_id, tool_span_id)
    held = {
        TRACE: {"spans": [_held_span(ROOT, "Turn 1"), _held_span(tool_span_id, "Agent", ROOT)], "pending": [agent_id]}
    }
    session.state.set("held_spans", json.dumps(held))


def _held(session):
    return json.loads(session.state.get("held_spans") or "{}").get(TRACE, {}).get("spans", [])


def _stop(session, transcript, agent_id="a1", agent_type="general-purpose", main=None):
    session.hook(
        handlers.subagent_stop,
        {
            "hook_event_name": "SubagentStop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "agent_transcript_path": str(transcript),
            "last_assistant_message": "done",
            **({"transcript_path": str(main)} if main is not None else {}),
        },
    )


def _grow(transcript: Path, n: int) -> None:
    records = []
    for i in range(1, n + 1):
        records.append(
            _assistant(f"u{i}", f"msg_{i}", [_tool_use(f"toolu_{i}")], ts=f"2026-11-14T03:34:{10 + 2 * i:02d}.000Z")
        )
        records.append(_tool_result(f"r{i}", f"toolu_{i}", ts=f"2026-11-14T03:34:{11 + 2 * i:02d}.000Z"))
    _write(transcript, records)


class TestABackgroundAgentIsExportedOnce:
    """An agent that stops, is resumed and stops again sends each event once."""

    def test_six_stops_send_each_event_once(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        session.state.set("subagent_a1_start_time", "1794627240000")
        for stop in range(1, 7):
            _grow(transcript, stop)
            _stop(session, transcript)
            session.state.set("subagent_a1_start_time", str(1794627240000 + stop * 5000))

        sent = session.spans()
        llm = [_attrs(s)["llm.message.id"] for s in sent if _attrs(s).get("openinference.span.kind") == "LLM"]
        tools = [_attrs(s)["tool.call.id"] for s in sent if _attrs(s).get("openinference.span.kind") == "TOOL"]
        assert sorted(llm) == sorted(f"msg_{i}" for i in range(1, 7))
        assert sorted(tools) == sorted(f"toolu_{i}" for i in range(1, 7))

    def test_the_agent_span_is_held_once_with_its_first_start(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        session.state.set("subagent_a1_start_time", "1794627240000")
        for stop in range(1, 4):
            _grow(transcript, stop)
            _stop(session, transcript)
            session.state.set("subagent_a1_start_time", str(1794627240000 + stop * 5000))

        assert not [s for s in session.spans() if s["name"].startswith("Subagent")]
        agents = [s for s in _held(session) if s["name"].startswith("Subagent")]
        assert len(agents) == 1
        assert agents[0]["startTimeUnixNano"] == "1794627240000000000"
        assert agents[0]["parentSpanId"] == "e" * 16

    def test_children_hang_off_one_agent_span_across_stops(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        for stop in range(1, 3):
            _grow(transcript, stop)
            _stop(session, transcript)
        [agent] = [s for s in _held(session) if s["name"].startswith("Subagent")]
        llm_parents = {s["parentSpanId"] for s in session.spans() if _attrs(s).get("openinference.span.kind") == "LLM"}
        assert llm_parents == {agent["spanId"]}

    def test_a_failed_send_is_retried_on_the_next_stop(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 1)
        refused = []
        session.sender = lambda p: refused.append(p) or False
        _stop(session, transcript)
        session.sender = None
        assert refused
        assert session.payloads == []
        _grow(transcript, 2)
        _stop(session, transcript)
        _stop(session, transcript)

        llm = [
            _attrs(s)["llm.message.id"] for s in session.spans() if _attrs(s).get("openinference.span.kind") == "LLM"
        ]
        assert sorted(llm) == ["msg_1", "msg_2"]


class TestSpanIdsAreDerivedFromEvents:
    def test_the_same_event_gets_the_same_id_in_every_process(self):
        assert handlers._derived_span_id(TRACE, "tool:toolu_1") == handlers._derived_span_id(TRACE, "tool:toolu_1")
        assert handlers._derived_span_id(TRACE, "tool:toolu_1") != handlers._derived_span_id(TRACE, "tool:toolu_2")
        assert handlers._derived_span_id(TRACE, "tool:toolu_1") != handlers._derived_span_id("f" * 32, "tool:toolu_1")
        assert len(handlers._derived_span_id(TRACE, "x")) == 16


def _agent_call(
    call_id, agent_id, status, subagent_type="web-fetch", ts="2026-11-14T03:32:40.000Z", done="2026-11-14T03:32:50.000Z"
):
    use = _assistant(
        f"use-{call_id}",
        f"msg-{call_id}",
        [_tool_use(call_id, "Agent", {"subagent_type": subagent_type, "prompt": "go"})],
        ts=ts,
    )
    result = _tool_result(f"res-{call_id}", call_id, [{"type": "text", "text": f"{agent_id} report"}], ts=done)
    result["toolUseResult"] = {"status": status, "agentId": agent_id}
    return [use, result]


def _web_fetch_transcript(subagents: Path, agent_id: str) -> Path:
    transcript = _write(
        subagents / f"agent-{agent_id}.jsonl",
        [
            _assistant(
                f"{agent_id}-u1",
                f"msg-{agent_id}-1",
                [_tool_use(f"toolu_{agent_id}_fetch", "WebFetch")],
                ts="2026-11-14T03:32:42.000Z",
            ),
            _tool_result(f"{agent_id}-r1", f"toolu_{agent_id}_fetch", ts="2026-11-14T03:32:44.000Z"),
            _assistant(
                f"{agent_id}-u2",
                f"msg-{agent_id}-2",
                [{"type": "text", "text": "Example Domain"}],
                ts="2026-11-14T03:32:46.000Z",
            ),
        ],
    )
    transcript.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": "web-fetch", "requestShape": "foreground"}), encoding="utf-8"
    )
    return transcript


def _open_turn(session, transcript: Path) -> None:
    for key, value in {
        "current_trace_id": TRACE,
        "current_trace_span_id": ROOT,
        "current_trace_start_time": "1794627139000",
        "current_trace_prompt": "go",
        "current_prompt_id": "p-1",
        "trace_start_line": "0",
    }.items():
        session.state.set(key, value)


def _main_stop(session, transcript: Path, background_tasks=None) -> None:
    payload = {"hook_event_name": "Stop", "transcript_path": str(transcript), "last_assistant_message": "done"}
    if background_tasks is not None:
        payload["background_tasks"] = background_tasks
    session.hook(handlers.stop, payload)


def _by_name(spans, prefix):
    return [s for s in spans if s["name"].startswith(prefix)]


class TestForegroundAgentsAreFoundWithoutHooks:
    """The built-in web-fetch agent fires no SubagentStart or SubagentStop."""

    def test_a_hookless_foreground_agent_is_spliced_under_its_agent_tool(self, session):
        main = _write(session.dir / "proj" / "s1.jsonl", _agent_call("toolu_wf", "wf1", "completed"))
        _web_fetch_transcript(session.dir / "proj" / "s1" / "subagents", "wf1")
        _open_turn(session, main)
        _main_stop(session, main)

        spans = session.spans()
        [agent] = _by_name(spans, "Subagent: web-fetch")
        [tool] = [s for s in spans if s["name"] == "Agent"]
        [fetch] = [s for s in spans if s["name"] == "WebFetch"]
        assert agent["parentSpanId"] == tool["spanId"]
        assert any(s["spanId"] == fetch["parentSpanId"] and s.get("parentSpanId") == agent["spanId"] for s in spans)

    def test_a_background_launch_is_never_spliced_as_foreground(self, session):
        main = _write(
            session.dir / "proj" / "s1.jsonl", _agent_call("toolu_bg", "bg1", "async_launched", "general-purpose")
        )
        _web_fetch_transcript(session.dir / "proj" / "s1" / "subagents", "bg1")
        _open_turn(session, main)
        _main_stop(session, main)

        assert not _by_name(session.spans(), "Subagent")
        assert not [s for s in session.spans() if s["name"] == "WebFetch"]

    def test_a_buffered_descriptor_and_the_file_splice_once(self, session):
        main = _write(session.dir / "proj" / "s1.jsonl", _agent_call("toolu_wf", "wf1", "completed"))
        transcript = _web_fetch_transcript(session.dir / "proj" / "s1" / "subagents", "wf1")
        _open_turn(session, main)
        session.hook(
            handlers.subagent_stop,
            {
                "hook_event_name": "SubagentStop",
                "agent_id": "wf1",
                "agent_type": "web-fetch",
                "agent_transcript_path": str(transcript),
                "transcript_path": str(main),
            },
        )
        _main_stop(session, main)

        assert len(_by_name(session.spans(), "Subagent: web-fetch")) == 1
        assert len([s for s in session.spans() if s["name"] == "WebFetch"]) == 1

    def test_a_missing_transcript_is_skipped(self, session):
        main = _write(session.dir / "proj" / "s1.jsonl", _agent_call("toolu_wf", "wf1", "completed"))
        _open_turn(session, main)
        _main_stop(session, main)

        assert not _by_name(session.spans(), "Subagent")
        assert [s for s in session.spans() if s["name"] == "Agent"]

    def test_a_foreground_agent_inside_a_background_agent_is_spliced(self, session):
        subagents = session.dir / "proj" / "s1" / "subagents"
        background = _write(subagents / "agent-a1.jsonl", _agent_call("toolu_wf2", "wf2", "completed"))
        _web_fetch_transcript(subagents, "wf2")
        _hold_launching_turn(session)

        _stop(session, background)

        spans = session.spans() + _held(session)
        [inner] = _by_name(spans, "Subagent: web-fetch")
        [tool] = [s for s in spans if s["name"] == "Agent" and s["spanId"] != "e" * 16]
        assert inner["parentSpanId"] == tool["spanId"]
        assert [s for s in spans if s["name"] == "WebFetch"]


def _nested_parent_transcript(subagents: Path, parent="a1", child="n1", call_id="toolu_n1") -> Path:
    records = [
        _assistant(
            f"{parent}-u1", f"msg-{parent}-1", [_tool_use(f"toolu_{parent}_bash")], ts="2026-11-14T03:34:01.000Z"
        )
    ]
    records.append(_tool_result(f"{parent}-r1", f"toolu_{parent}_bash", ts="2026-11-14T03:34:02.000Z"))
    records += _agent_call(
        call_id, child, "async_launched", "Explore", ts="2026-11-14T03:34:05.000Z", done="2026-11-14T03:34:05.100Z"
    )
    records.append(
        _assistant(
            f"{parent}-u2", f"msg-{parent}-2", [{"type": "text", "text": "handing back"}], ts="2026-11-14T03:34:06.000Z"
        )
    )
    return _write(subagents / f"agent-{parent}.jsonl", records)


def _nested_child_transcript(subagents: Path, child="n1", parent="a1", call_id="toolu_n1") -> Path:
    transcript = _write(
        subagents / f"agent-{child}.jsonl",
        [
            _assistant(
                f"{child}-u1", f"msg-{child}-1", [_tool_use(f"toolu_{child}_bash")], ts="2026-11-14T03:34:07.000Z"
            ),
            _tool_result(f"{child}-r1", f"toolu_{child}_bash", ts="2026-11-14T03:34:08.000Z"),
            _assistant(
                f"{child}-u2", f"msg-{child}-2", [{"type": "text", "text": "listed"}], ts="2026-11-14T03:34:09.000Z"
            ),
        ],
    )
    transcript.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "agentType": "Explore",
                "requestShape": "background",
                "spawnDepth": 2,
                "parentAgentId": parent,
                "toolUseId": call_id,
            }
        ),
        encoding="utf-8",
    )
    return transcript


def _skilled(record, skill="release-notes"):
    return {**record, "attributionSkill": skill}


class TestASubagentsActiveSkillReachesItsModelCalls:
    def _llm_skills(self, session):
        return {
            _attrs(s)["llm.message.id"]: _attrs(s).get("skill.active")
            for s in session.spans()
            if _attrs(s).get("openinference.span.kind") == "LLM"
        }

    def test_a_spliced_foreground_agent(self, session):
        main = _write(session.dir / "proj" / "s1.jsonl", _agent_call("toolu_wf", "wf1", "completed"))
        subagent = _web_fetch_transcript(session.dir / "proj" / "s1" / "subagents", "wf1")
        records = [json.loads(line) for line in subagent.read_text(encoding="utf-8").splitlines()]
        _write(subagent, [_skilled(r) if r["type"] == "assistant" else r for r in records])
        _open_turn(session, main)
        _main_stop(session, main)

        skills = self._llm_skills(session)
        assert skills["msg-wf1-1"] == "release-notes"
        assert skills["msg-wf1-2"] == "release-notes"
        assert skills["msg-toolu_wf"] is None

    def test_a_background_agent(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _write(
            transcript,
            [
                _skilled(_assistant("u1", "msg_1", [_tool_use("toolu_1")], ts="2026-11-14T03:34:12.000Z")),
                _tool_result("r1", "toolu_1", ts="2026-11-14T03:34:13.000Z"),
                _assistant("u2", "msg_2", [{"type": "text", "text": "done"}], ts="2026-11-14T03:34:14.000Z"),
            ],
        )
        _stop(session, transcript)

        assert self._llm_skills(session) == {"msg_1": "release-notes", "msg_2": None}


class TestNestedBackgroundAgentsLandUnderTheirAgentTool:
    """A background agent launched by another background agent stops while no turn is open."""

    def _assert_nested_under_its_tool(self, session):
        spans = session.spans() + _held(session)
        tool_id = handlers._derived_span_id(TRACE, "tool:toolu_n1")
        [nested] = _by_name(spans, "Subagent: Explore")
        assert nested["parentSpanId"] == tool_id
        assert [s for s in spans if s["spanId"] == tool_id]
        llm = [
            _attrs(s)["llm.message.id"]
            for s in session.spans()
            if _attrs(s).get("llm.message.id", "").startswith("msg-n1")
        ]
        assert sorted(llm) == ["msg-n1-1", "msg-n1-2"]

    def test_a_nested_agent_stopping_after_its_parent_exported(self, session):
        subagents = session.dir / "proj" / "s1" / "subagents"
        parent = _nested_parent_transcript(subagents)
        child = _nested_child_transcript(subagents)
        _hold_launching_turn(session)

        _stop(session, parent)
        _stop(session, child, agent_id="n1", agent_type="Explore")

        self._assert_nested_under_its_tool(session)

    def test_a_nested_agent_stopping_before_its_parent_exported(self, session):
        subagents = session.dir / "proj" / "s1" / "subagents"
        parent = _nested_parent_transcript(subagents)
        child = _nested_child_transcript(subagents)
        _hold_launching_turn(session)

        _stop(session, child, agent_id="n1", agent_type="Explore")
        _stop(session, parent)

        self._assert_nested_under_its_tool(session)
        assert len(_by_name(session.spans() + _held(session), "Subagent: Explore")) == 1

    def test_registering_twice_keeps_what_was_already_sent(self, session):
        subagents = session.dir / "proj" / "s1" / "subagents"
        parent = _nested_parent_transcript(subagents)
        child = _nested_child_transcript(subagents)
        _hold_launching_turn(session)
        _stop(session, parent)
        _stop(session, child, agent_id="n1", agent_type="Explore")
        _stop(session, parent)
        _stop(session, child, agent_id="n1", agent_type="Explore")

        llm = [
            _attrs(s)["llm.message.id"] for s in session.spans() if _attrs(s).get("openinference.span.kind") == "LLM"
        ]
        assert sorted(llm) == sorted(set(llm))

    def test_a_nested_agent_does_not_join_the_wait_for_notifications(self, session):
        subagents = session.dir / "proj" / "s1" / "subagents"
        _hold_launching_turn(session)
        _stop(session, _nested_parent_transcript(subagents))
        held = json.loads(session.state.get("held_spans"))[TRACE]
        assert held["pending"] == ["a1"]


def _end_ms(span):
    return int(span["endTimeUnixNano"]) // 1_000_000


def _two_agent_trace(session):
    session.state.set(
        "background_agents",
        json.dumps(
            {
                "a1": {"trace_id": TRACE, "tool_span_id": "e" * 16, "turn_id": "1"},
                "a2": {"trace_id": TRACE, "tool_span_id": "f" * 16, "turn_id": "1"},
            }
        ),
    )
    held = {
        TRACE: {
            "spans": [
                _held_span(ROOT, "Turn 1"),
                _held_span("e" * 16, "Agent", ROOT),
                _held_span("f" * 16, "Agent", ROOT),
            ],
            "pending": ["a1", "a2"],
        }
    }
    session.state.set("held_spans", json.dumps(held))


def _idle_turn(session):
    main = _write(
        session.dir / "proj" / "s1.jsonl", [_assistant("m1", "msg-main", [{"type": "text", "text": "noted"}])]
    )
    session.state.set("current_trace_id", "9" * 32)
    session.state.set("current_trace_span_id", "8" * 16)
    session.state.set("current_trace_start_time", "1794627300000")
    session.state.set("trace_start_line", "0")
    return main


def _roots(session):
    return [s for s in session.spans() if s["spanId"] == ROOT]


class TestHeldSpansAreReleasedWhenTheWorkIsDone:
    def test_only_the_finishing_agents_ancestors_grow(self, session):
        _two_agent_trace(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 3)
        _stop(session, transcript)

        held = {s["spanId"]: s for s in _held(session)}
        assert _end_ms(held["f" * 16]) == 1794627110000, "sibling Agent span must not be stretched"
        assert _end_ms(held["e" * 16]) >= 1794627257000
        assert _end_ms(held[ROOT]) >= 1794627257000

    def test_released_at_the_main_stop_once_no_agent_of_the_trace_runs(self, session):
        _two_agent_trace(session)
        for agent in ("a1", "a2"):
            transcript = session.dir / "proj" / "s1" / "subagents" / f"agent-{agent}.jsonl"
            _grow(transcript, 2)
            _stop(session, transcript, agent_id=agent)
        main = _idle_turn(session)

        _main_stop(session, main, background_tasks=[{"id": "a2", "type": "subagent", "status": "running"}])
        assert not _roots(session), "a2 still runs"
        _main_stop(session, main, background_tasks=[])

        [root] = _roots(session)
        assert _end_ms(root) == max(
            _end_ms(s) for s in session.spans() if s["traceId"] == TRACE and s["spanId"] != ROOT
        )
        assert TRACE not in json.loads(session.state.get("held_spans") or "{}")

    def test_an_agent_that_only_handed_back_does_not_hold_the_root(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 1)
        _stop(session, transcript)
        main = _idle_turn(session)

        _main_stop(session, main, background_tasks=[])

        assert len(_roots(session)) == 1
        assert len(_by_name(session.spans(), "Subagent")) == 1

    def test_a_stop_without_background_tasks_keeps_todays_rules(self, session):
        _hold_launching_turn(session)
        main = _idle_turn(session)
        _main_stop(session, main)
        assert not _roots(session)

    def test_the_next_human_prompt_releases_a_finished_trace(self, session):
        _hold_launching_turn(session)
        main = _idle_turn(session)
        _main_stop(session, main, background_tasks=[{"id": "a1", "type": "subagent", "status": "running"}])
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 1)
        _stop(session, transcript)
        session.state.set("last_background_task_ids", json.dumps([]))

        session.hook(
            handlers.user_prompt_submit,
            {"hook_event_name": "UserPromptSubmit", "prompt": "next", "prompt_id": "p-2", "transcript_path": str(main)},
        )

        assert len(_roots(session)) == 1

    def test_session_end_never_stretches_a_held_span(self, session):
        _hold_launching_turn(session)
        session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})
        [root] = _roots(session)
        assert _end_ms(root) == 1794627110000


def _flaky(session, fail_calls):
    """A collector that refuses the given (1-based) POSTs and accepts the rest."""
    calls = {"n": 0}

    def send(payload):
        calls["n"] += 1
        if calls["n"] in fail_calls:
            return False
        session.payloads.append(payload)
        return True

    return send


def _wire_size(payload):
    return len(json.dumps(payload).encode("utf-8"))


class TestLargePayloadsAreSplit:
    BUDGET = 6_000

    @pytest.fixture(autouse=True)
    def _small_budget(self, monkeypatch):
        monkeypatch.setattr(handlers, "OTLP_MAX_PAYLOAD_BYTES", self.BUDGET)

    def _llm_ids(self, session):
        return sorted(
            _attrs(s)["llm.message.id"] for s in session.spans() if _attrs(s).get("openinference.span.kind") == "LLM"
        )

    def _padded_held_trace(self, session):
        _hold_launching_turn(session)
        held = json.loads(session.state.get("held_spans"))
        pad = [{"key": "pad", "value": {"stringValue": "p" * 2_500}}]
        held[TRACE]["spans"] += [
            {**_held_span(f"{i:016x}", f"Tool {i}", "e" * 16), "attributes": pad} for i in range(1, 7)
        ]
        session.state.set("held_spans", json.dumps(held))
        return sorted(s["spanId"] for s in held[TRACE]["spans"])

    def test_a_large_agent_export_goes_in_posts_under_budget(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 12)
        _stop(session, transcript)

        assert len(session.payloads) > 1
        assert all(_wire_size(p) <= self.BUDGET for p in session.payloads)
        assert self._llm_ids(session) == sorted(f"msg_{i}" for i in range(1, 13))

    def test_a_chunk_lost_twice_is_resent_alone_at_the_next_stop(self, session):
        _hold_launching_turn(session)
        transcript = session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
        _grow(transcript, 12)
        session.sender = _flaky(session, {2, 3})
        _stop(session, transcript)
        session.sender = None
        delivered_first = len(session.spans())
        _stop(session, transcript)

        assert 0 < delivered_first < len(session.spans())
        assert self._llm_ids(session) == sorted(f"msg_{i}" for i in range(1, 13))

    def test_a_held_release_goes_in_posts_under_budget(self, session):
        held_ids = self._padded_held_trace(session)
        session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})

        assert len(session.payloads) > 1
        assert all(_wire_size(p) <= self.BUDGET for p in session.payloads)
        assert sorted(s["spanId"] for s in session.spans() if s["traceId"] == TRACE) == held_ids

    def test_a_release_chunk_that_fails_once_is_retried(self, session):
        held_ids = self._padded_held_trace(session)
        session.sender = _flaky(session, {1})
        session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})

        assert sorted(s["spanId"] for s in session.spans() if s["traceId"] == TRACE) == held_ids

    def test_a_release_chunk_that_fails_twice_is_dropped_and_the_rest_still_go(self, session):
        held_ids = self._padded_held_trace(session)
        session.sender = _flaky(session, {2, 3})
        session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})

        sent = sorted(s["spanId"] for s in session.spans() if s["traceId"] == TRACE)
        assert 0 < len(sent) < len(held_ids)
        assert set(sent) < set(held_ids)


def _registered(session, agent_id="a1", tool_use_id="toolu_A1"):
    session.state.set(
        "background_agents",
        json.dumps(
            {agent_id: {"trace_id": TRACE, "tool_span_id": "e" * 16, "tool_use_id": tool_use_id, "turn_id": "1"}}
        ),
    )
    session.state.set("held_spans", json.dumps({TRACE: {"spans": [_held_span(ROOT, "Turn 1")], "pending": [agent_id]}}))


def _prompt(session, text, prompt_id="p-n"):
    main = _write(session.dir / "proj" / "s1.jsonl", [])
    session.hook(
        handlers.user_prompt_submit,
        {"hook_event_name": "UserPromptSubmit", "prompt": text, "prompt_id": prompt_id, "transcript_path": str(main)},
    )


class TestReactionTurnsStayInTheLaunchingTrace:
    def test_a_notification_with_only_a_task_id(self, session):
        _registered(session)
        _prompt(
            session,
            "<task-notification>\n<task-id>a1</task-id>\n<output-file>/tmp/x</output-file>\n<status>completed</status>",
        )
        assert session.state.get("current_trace_id") == TRACE
        assert session.state.get("continuation_agent_id") == "a1"

    def test_a_notification_naming_the_send_message_that_resumed_the_agent(self, session):
        _registered(session)
        _prompt(
            session,
            "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>toolu_SENDMSG</tool-use-id>\n<status>completed</status>",
        )
        assert session.state.get("current_trace_id") == TRACE

    def test_a_quoted_notification_inside_a_hand_back_does_not_match(self, session):
        _registered(session, "a1")
        prompt = (
            'Another Claude session sent a message:\n<agent-message from="parent9">\n  report quoting\n'
            "  <task-notification> <task-id>a1</task-id> </task-notification>\n</agent-message>"
        )
        _prompt(session, prompt)
        assert session.state.get("continuation_agent_id") != "a1"

    def test_an_unknown_task_id_starts_a_new_trace(self, session):
        _registered(session)
        _prompt(session, "<task-notification>\n<task-id>zz9</task-id>\n<status>completed</status>")
        assert session.state.get("current_trace_id") not in (None, TRACE)


def _ts(seconds: int) -> str:
    return f"2026-11-14T03:{32 + seconds // 60:02d}:{seconds % 60:02d}.000Z"


def _human(uuid, text, seconds):
    return {"type": "user", "uuid": uuid, "timestamp": _ts(seconds), "message": {"role": "user", "content": text}}


def _transcript(path: Path, records: list, meta: "dict | None" = None) -> Path:
    _write(path, records)
    if meta is not None:
        path.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


class TestASessionLikeTheDebugRun:
    """The hook order of DG's run 2 (session 76e1412e), on small transcripts. A web-fetch agent
    fires no hooks; a background agent hands back inside the live turn; another launches a
    nested agent that stops first, hands back, is resumed, and reports by task id alone."""

    @pytest.fixture
    def run(self, session):
        sub = session.dir / "proj" / "s1" / "subagents"
        main = session.dir / "proj" / "s1.jsonl"
        turn = [_human("h1", "do everything in master.prompt", 0)]
        turn += _agent_call("toolu_wf", "wf", "completed", "web-fetch", ts=_ts(2), done=_ts(8))
        launch = _assistant(
            "m2",
            "msg-main-2",
            [
                _tool_use("toolu_ex", "Agent", {"subagent_type": "Explore"}),
                _tool_use("toolu_gp", "Agent", {"subagent_type": "general-purpose"}),
            ],
            ts=_ts(10),
        )
        results = []
        for call_id, agent in (("toolu_ex", "ex"), ("toolu_gp", "gp")):
            result = _tool_result(f"res-{agent}", call_id, [{"type": "text", "text": "launched"}], ts=_ts(11))
            result["toolUseResult"] = {"status": "async_launched", "agentId": agent}
            results.append(result)
        turn += [launch, *results, _assistant("m3", "msg-main-3", [{"type": "text", "text": "A1 is done"}], ts=_ts(20))]

        _web_fetch_transcript(sub, "wf")
        ex = _transcript(
            sub / "agent-ex.jsonl",
            [
                _assistant("ex1", "msg-ex-1", [_tool_use("toolu_ex_bash")], ts=_ts(12)),
                _tool_result("ex2", "toolu_ex_bash", ts=_ts(13)),
                _assistant("ex3", "msg-ex-2", [{"type": "text", "text": "20 files"}], ts=_ts(14)),
            ],
            {"agentType": "Explore", "requestShape": "background", "toolUseId": "toolu_ex"},
        )
        gp_first = [
            _assistant("gp1", "msg-gp-1", [_tool_use("toolu_gp_bash")], ts=_ts(12)),
            _tool_result("gp2", "toolu_gp_bash", ts=_ts(13)),
            *_agent_call("toolu_n1", "n1", "async_launched", "Explore", ts=_ts(30), done=_ts(31)),
            _assistant("gp3", "msg-gp-2", [{"type": "text", "text": "handing back"}], ts=_ts(40)),
        ]
        gp_meta = {"agentType": "general-purpose", "requestShape": "background", "toolUseId": "toolu_gp"}
        gp = _transcript(sub / "agent-gp.jsonl", gp_first, gp_meta)
        n1 = _transcript(
            sub / "agent-n1.jsonl",
            [
                _assistant("n1a", "msg-n1-1", [_tool_use("toolu_n1_bash")], ts=_ts(32)),
                _tool_result("n1b", "toolu_n1_bash", ts=_ts(33)),
                _assistant("n1c", "msg-n1-2", [{"type": "text", "text": "listed"}], ts=_ts(34)),
            ],
            {"agentType": "Explore", "requestShape": "background", "parentAgentId": "gp", "toolUseId": "toolu_n1"},
        )

        def prompt(text, prompt_id):
            session.hook(
                handlers.user_prompt_submit,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": text,
                    "prompt_id": prompt_id,
                    "transcript_path": str(main),
                },
            )

        def stop(*running):
            tasks = [{"id": agent, "type": "subagent", "status": "running"} for agent in running]
            _main_stop(session, main, background_tasks=tasks)

        _write(main, [])
        prompt("do everything in master.prompt", "p-human")
        _write(main, turn[:-1])
        _stop(session, ex, agent_id="ex", agent_type="Explore", main=main)
        prompt('<agent-message from="ex">hand-back</agent-message>', "p-human")
        _write(main, turn)
        stop("gp")
        _stop(session, n1, agent_id="n1", agent_type="Explore", main=main)
        _stop(session, gp, agent_id="gp", main=main)
        hand_back = [_human("h2", '<agent-message from="gp">report</agent-message>', 45)]
        _write(main, turn + hand_back)
        prompt('<agent-message from="gp">report</agent-message>', "p-gp-hand-back")
        _write(
            main,
            turn
            + hand_back
            + [_assistant("m4", "msg-main-4", [{"type": "text", "text": "A3 handed back"}], ts=_ts(46))],
        )
        stop("gp")
        _transcript(
            gp, gp_first + [_assistant("gp4", "msg-gp-3", [{"type": "text", "text": "after resume"}], ts=_ts(50))]
        )
        _stop(session, gp, agent_id="gp", main=main)
        notified = (
            turn
            + hand_back
            + [_assistant("m4", "msg-main-4", [{"type": "text", "text": "A3 handed back"}], ts=_ts(46))]
        )
        notified += [_human("h3", "<task-notification><task-id>gp</task-id></task-notification>", 52)]
        _write(main, notified)
        prompt(
            "<task-notification>\n<task-id>gp</task-id>\n<output-file>/tmp/gp</output-file>\n<status>completed</status>",
            "p-gp-done",
        )
        _write(
            main,
            notified + [_assistant("m5", "msg-main-5", [{"type": "text", "text": "everything reported"}], ts=_ts(55))],
        )
        stop()
        return session

    def test_one_trace_with_a_root(self, run):
        spans = run.spans()
        assert {s["traceId"] for s in spans} == {TRACE_OF(run)}
        roots = [s for s in spans if not s.get("parentSpanId")]
        assert len(roots) == 1 and roots[0]["name"].startswith("Turn ")
        assert not json.loads(run.state.get("held_spans") or "{}")

    def test_no_span_points_at_a_parent_that_was_never_sent(self, run):
        spans = run.spans()
        ids = {s["spanId"] for s in spans}
        assert [s["name"] for s in spans if s.get("parentSpanId") and s["parentSpanId"] not in ids] == []

    def test_every_event_exactly_once(self, run):
        keys = []
        for span in run.spans():
            a = _attrs(span)
            kind = a.get("openinference.span.kind")
            if kind == "LLM":
                keys.append(("llm", a.get("llm.message.id")))
            elif kind == "TOOL":
                keys.append(("tool", a.get("tool.call.id")))
            elif span["name"].startswith("Subagent"):
                keys.append(("agent", a.get("subagent.id") or span["name"]))
        assert [k for k in set(keys) if keys.count(k) > 1] == []
        llm = {k[1] for k in keys if k[0] == "llm"}
        assert {"msg-ex-1", "msg-gp-1", "msg-gp-3", "msg-n1-1", "msg-wf-1"} <= llm
        assert len([k for k in keys if k[0] == "agent"]) == 4

    def test_every_parent_outlasts_its_children(self, run):
        spans = run.spans()
        for span in spans:
            children = [c for c in spans if c.get("parentSpanId") == span["spanId"]]
            if children:
                assert int(span["endTimeUnixNano"]) >= max(int(c["endTimeUnixNano"]) for c in children), span["name"]


def TRACE_OF(session) -> str:
    return next(s["traceId"] for s in session.spans() if s["name"].startswith("Turn ") and not s.get("parentSpanId"))


class TestALateAcknowledgementDoesNotEndTheNextTurn:
    """A turn's export is acknowledged by the detached sender once the POST returns. A reaction
    turn that continues the same trace can start before that; the late acknowledgement must
    not end it."""

    def test_the_reaction_turn_is_still_exported(self, session):
        main = session.dir / "proj" / "s1.jsonl"
        _write(main, [])
        session.hook(
            handlers.user_prompt_submit,
            {"hook_event_name": "UserPromptSubmit", "prompt": "go", "prompt_id": "p-1", "transcript_path": str(main)},
        )
        turn = [
            _human("h1", "go", 0),
            *_agent_call("toolu_a1", "a1", "async_launched", "general-purpose", ts=_ts(2), done=_ts(3)),
        ]
        turn.append(_assistant("m2", "msg-main-2", [{"type": "text", "text": "launched"}], ts=_ts(4)))
        _write(main, turn)

        deferred = []
        with mock.patch.object(
            handlers,
            "_send_span_async",
            lambda payload, on_success=None, on_success_ref=None: deferred.append(on_success),
        ):
            _main_stop(session, main, background_tasks=[{"id": "a1", "type": "subagent", "status": "running"}])

        notified = turn + [_human("h2", "<task-notification><task-id>a1</task-id></task-notification>", 10)]
        _write(main, notified)
        session.hook(
            handlers.user_prompt_submit,
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>",
                "prompt_id": "p-2",
                "transcript_path": str(main),
            },
        )
        for settle in deferred:
            if settle is not None:
                settle()

        assert session.state.get("current_trace_id"), "the late acknowledgement ended the reaction turn"
        _write(main, notified + [_assistant("m3", "msg-reply", [{"type": "text", "text": "A1 finished"}], ts=_ts(12))])
        _main_stop(session, main, background_tasks=[])

        llm = {_attrs(s).get("llm.message.id") for s in session.spans()}
        assert "msg-reply" in llm


def _append_later(path: Path, record: dict, delay_s: float = 0.1) -> threading.Timer:
    def append():
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    timer = threading.Timer(delay_s, append)
    timer.start()
    return timer


def _llm_ids(session):
    return {_attrs(s).get("llm.message.id") for s in session.spans()}


class TestTheFinalMessageIsWaitedFor:
    """Claude Code can fire Stop before the final reply reaches the transcript file."""

    def test_a_background_agent_waits_for_its_final_message(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "2000")
        _hold_launching_turn(session)
        transcript = _write(
            session.dir / "proj" / "s1" / "subagents" / "agent-a1.jsonl",
            [_assistant("u1", "msg_work", [_tool_use("toolu_1")]), _tool_result("r1", "toolu_1")],
        )
        timer = _append_later(transcript, _assistant("u2", "msg_final", [{"type": "text", "text": "done"}]))
        _stop(session, transcript)
        timer.join()

        assert "msg_final" in _llm_ids(session)

    def test_a_stop_waits_for_the_final_reply_behind_an_earlier_message(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "2000")
        main = _write(
            session.dir / "proj" / "s1.jsonl",
            [
                _assistant("m1", "msg_early", [{"type": "text", "text": "A3 has reported"}, _tool_use("toolu_send")]),
                _tool_result("m2", "toolu_send"),
            ],
        )
        _open_turn(session, main)
        timer = _append_later(main, _assistant("m3", "msg_final", [{"type": "text", "text": "done"}]))
        _main_stop(session, main)
        timer.join()

        assert {"msg_early", "msg_final"} <= _llm_ids(session)

    def test_without_an_expected_text_any_assistant_entry_ends_the_wait(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "2000")
        transcript = _write(tmp_path / "t.jsonl", [_assistant("m1", "msg_1", [{"type": "text", "text": "hi"}])])
        began = time.monotonic()

        assert handlers._wait_for_transcript_flush(transcript, 0)
        assert time.monotonic() - began < 0.5

    def test_a_final_text_that_never_lands_gives_up_at_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "100")
        transcript = _write(tmp_path / "t.jsonl", [_assistant("m1", "msg_1", [{"type": "text", "text": "hi"}])])
        began = time.monotonic()

        assert not handlers._wait_for_transcript_flush(transcript, 0, expected_text="bye")
        assert time.monotonic() - began < 1.0


def _subagent_failure(session, agent_id="wfa1"):
    session.hook(
        handlers.stop_failure,
        {
            "hook_event_name": "StopFailure",
            "agent_id": agent_id,
            "agent_type": "workflow-subagent",
            "error": "model_not_found",
            "last_assistant_message": "There's an issue with the selected model.",
        },
    )


class TestASubagentFailureLeavesTheMainTurnAlone:
    """A failed subagent fires StopFailure with its agent_id while the main turn is still live."""

    def _turn(self, session) -> Path:
        main = _write(
            session.dir / "proj" / "s1.jsonl",
            [
                _human("h1", "go", 0),
                _assistant("m1", "msg_launch", [_tool_use("toolu_bash")], ts=_ts(1)),
                _tool_result("m2", "toolu_bash", ts=_ts(2)),
                _assistant("m3", "msg_final", [{"type": "text", "text": "done"}], ts=_ts(3)),
            ],
        )
        _open_turn(session, main)
        return main

    def test_no_failed_turn_is_sent_and_the_turn_stays_open(self, session):
        self._turn(session)
        _subagent_failure(session)

        assert session.spans() == []
        assert session.state.get("current_trace_id") == TRACE
        assert session.state.get("current_trace_span_id") == ROOT

    def test_the_main_stop_still_exports_the_turns_own_work(self, session):
        main = self._turn(session)
        _subagent_failure(session)
        _main_stop(session, main)

        roots = [s for s in session.spans() if not s.get("parentSpanId")]
        assert [r["name"] for r in roots] == ["Turn 1"]
        assert roots[0]["spanId"] == ROOT
        assert {"msg_launch", "msg_final"} <= _llm_ids(session)
        assert "toolu_bash" in {_attrs(s).get("tool.call.id") for s in session.spans()}

    def test_a_main_thread_failure_still_ends_the_turn(self, session):
        self._turn(session)
        session.hook(handlers.stop_failure, {"hook_event_name": "StopFailure", "error": "rate_limit"})

        assert [s["name"] for s in session.spans()] == ["Turn 1 (failed)"]
        assert session.state.get("current_trace_id") is None
