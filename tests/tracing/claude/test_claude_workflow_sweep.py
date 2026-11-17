#!/usr/bin/env python3
"""A session killed mid-workflow is swept by a later session: its open workflows are finalized
as incomplete and its held traces are sent once."""

import json
import os
import time

import pytest
from workflow_fixtures import SESSION, Session, attrs, workflow_agent, write_jsonl

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers
from core.common import build_span
from tracing.claude_code.hooks import workflows

OLD_TRACE = "a" * 32
ROOT_SPAN = "b" * 16
TOOL_SPAN = "c" * 16


def _dead_pid() -> int:
    pid = 4_000_000
    while adapter._is_pid_alive(pid):
        pid += 1
    return pid


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "0")
    monkeypatch.setattr(workflows, "_drift_seen", set())
    return Session(tmp_path)


def _span(name, span_id, parent=""):
    payload = build_span(name, "CHAIN", span_id, OLD_TRACE, parent, 1794627100000, 1794627101000, {})
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _old_session(session, key="old", *, age_s=7200, pid=None, workflow=True, finalized=False):
    run_dir = session.dir / "proj" / key / "subagents" / "workflows" / "wf_old-1"
    entry = {
        "trace_id": OLD_TRACE,
        "tool_span_id": TOOL_SPAN,
        "tool_use_id": "toolu_old",
        "turn_id": "1",
        "launched_at_ms": 1794627100500,
    }
    if workflow:
        entry.update(
            kind="workflow",
            run_id="wf_old-1",
            run_dir=str(run_dir),
            workflow_name="wf-old",
            launch_started_ms=1794627100400,
            claude_pid=pid if pid is not None else _dead_pid(),
        )
        if finalized:
            entry.update(finalized_at_ms=1794627105000, status="completed")
    data = {
        "session_id": key,
        "background_agents": json.dumps({"wold": entry}),
        "held_spans": json.dumps(
            {
                OLD_TRACE: {
                    "spans": [_span("Turn 1", ROOT_SPAN), _span("Workflow", TOOL_SPAN, ROOT_SPAN)],
                    "pending": ["wold"],
                }
            }
        ),
    }
    path = session.dir / f"state_{key}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path, run_dir


def _start(session):
    session.hook(handlers.session_start, {"hook_event_name": "SessionStart", "source": "startup"})


def _registry(path):
    return json.loads(json.loads(path.read_text(encoding="utf-8"))["background_agents"])


def _sent_names(session):
    return [s["name"] for s in session.spans() if s["traceId"] == OLD_TRACE]


class TestSweep:
    def test_an_idle_session_with_a_dead_process_is_finalized_and_released_once(self, session):
        path, _ = _old_session(session)
        _start(session)

        entry = _registry(path)["wold"]
        assert entry["status"] == "incomplete" and entry["finalize_reason"] == "sweep"
        names = _sent_names(session)
        assert names.count("Workflow run: wf-old") == 1
        [root] = [s for s in session.spans() if s["spanId"] == ROOT_SPAN]
        assert attrs(root)["trace.release_reason"] == "sweep"
        assert json.loads(json.loads(path.read_text(encoding="utf-8")).get("held_spans") or "{}") == {}

    def test_a_second_sweep_sends_nothing_more(self, session):
        _old_session(session)
        _start(session)
        sent = len(session.spans())
        _start(session)

        assert len(session.spans()) == sent

    def test_its_interrupted_agent_is_rendered(self, session):
        _, run_dir = _old_session(session)
        workflow_agent(run_dir, "a1", "slow", "Second", handback=False)
        agent_file = session.dir / "state_old__agent_a1.json"
        agent_file.write_text(json.dumps({"start_time": "1794627100600"}), encoding="utf-8")
        stamp = time.time() - 7200
        os.utime(agent_file, (stamp, stamp))
        _start(session)

        [agent] = [s for s in session.spans() if s["name"].startswith("Agent")]
        assert attrs(agent)["workflow.agent_state"] == "interrupted"
        assert not agent_file.exists()


class TestNotSwept:
    def test_a_live_process(self, session):
        path, _ = _old_session(session, pid=os.getpid())
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]
        assert not _sent_names(session)

    def test_an_agent_file_changed_within_the_hour(self, session):
        path, _ = _old_session(session)
        (session.dir / "state_old__agent_a1.json").write_text("{}", encoding="utf-8")
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]

    def test_a_session_holding_only_background_agents(self, session):
        path, _ = _old_session(session, workflow=False)
        _start(session)

        assert not _sent_names(session)
        assert json.loads(json.loads(path.read_text(encoding="utf-8"))["held_spans"])

    def test_a_session_idle_for_more_than_a_week(self, session):
        path, _ = _old_session(session, age_s=8 * 24 * 3600)
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]

    def test_a_session_idle_for_less_than_an_hour(self, session):
        path, _ = _old_session(session, age_s=600)
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]

    def test_the_current_session(self, session):
        path, _ = _old_session(session, key=SESSION)
        session.state = handlers.StateManager(session.dir, path, session.dir / f".lock_{SESSION}")
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]

    def test_the_switch_turns_the_sweep_off(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_WORKFLOWS", "false")
        path, _ = _old_session(session)
        _start(session)

        assert "finalized_at_ms" not in _registry(path)["wold"]


class TestBounds:
    def test_at_most_fifty_sessions_are_swept_per_pass(self, session):
        paths = [_old_session(session, key=f"old{i:02d}")[0] for i in range(55)]
        _start(session)

        swept = sum(1 for path in paths if "finalized_at_ms" in _registry(path)["wold"])
        assert swept == workflows.SWEEP_MAX_SESSIONS == 50


class TestProcessIsRecorded:
    def test_registration_records_the_claude_process(self, session):
        from workflow_fixtures import workflow_call, workflow_result

        session.prompt("go", "p-1")
        run_dir = session.subagents / "workflows" / "wf_aaaa-111"
        result = workflow_result("w1", "wf_aaaa-111", run_dir)
        write_jsonl(session.main, workflow_call("toolu_wf1", result, 2, 3))
        session.post_tool("toolu_wf1", "Workflow", {}, result)

        assert session.registry()["w1"]["claude_pid"] == int(adapter._get_grandparent_pid())


class TestDrift:
    def test_an_unreadable_meta_file_logs_one_drift_line(self, session, capsys):
        run_dir = session.subagents / "workflows" / "wf_x"
        run_dir.mkdir(parents=True)
        (run_dir / "agent-a1.meta.json").write_text("{not json", encoding="utf-8")
        (run_dir / "agent-a2.meta.json").write_text("{not json", encoding="utf-8")
        workflows.read_meta(run_dir, "a1")
        workflows.read_meta(run_dir, "a2")

        assert capsys.readouterr().err.count("workflow drift kind=meta") == 1

    def test_an_unknown_run_file_shape_logs_drift_and_reads_as_absent(self, session, capsys):
        run_dir = session.subagents / "workflows" / "wf_x"
        path = run_dir.parents[2] / "workflows" / "wf_x.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"taskId": "w1", "status": 3}), encoding="utf-8")

        assert workflows.read_run_json(run_dir, "wf_x", "w1") is None
        assert "workflow drift kind=run_json" in capsys.readouterr().err

    def test_a_missing_run_file_is_not_drift(self, session, capsys):
        run_dir = session.subagents / "workflows" / "wf_x"

        assert workflows.read_run_json(run_dir, "wf_x", "w1") is None
        assert "drift" not in capsys.readouterr().err
