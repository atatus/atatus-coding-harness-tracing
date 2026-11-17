#!/usr/bin/env python3
"""A workflow is finalized once, when its end is known: workflow and phase spans, the agents its
stops did not export, and the root's final reply at release."""

import json

import pytest
from workflow_fixtures import (
    SESSION,
    Session,
    agent_start,
    agent_stop,
    assistant,
    attrs,
    human,
    notification,
    text,
    workflow_agent,
    workflow_call,
    workflow_result,
    write_jsonl,
    write_run_json,
)

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers
from tracing.claude_code.hooks import workflows

RUN = "wf_aaaa-111"
TASK = "w1"
CALL = "toolu_wf1"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "0")
    return Session(tmp_path)


def _run_dir(session, run_id=RUN):
    return session.subagents / "workflows" / run_id


def _running(*task_ids):
    return [{"id": t, "type": "workflow", "status": "running"} for t in task_ids]


class Flow:
    """One human prompt that launches one workflow, as the hooks see it."""

    def __init__(self, session: Session):
        self.s = session
        self.records = []

    def launch(self, task_id=TASK, run_id=RUN, call_id=CALL):
        self.s.prompt("run the workflow", "p-1")
        result = workflow_result(task_id, run_id, _run_dir(self.s, run_id))
        self.records = [human("h1", "run the workflow", 0), *workflow_call(call_id, result, 2, 3)]
        write_jsonl(self.s.main, self.records[:2])
        self.s.pre_tool(call_id, "Workflow", {})
        write_jsonl(self.s.main, self.records)
        self.s.post_tool(call_id, "Workflow", {}, result)
        self.records.append(assistant("m2", "msg-m2", [text("launched")], 5))
        write_jsonl(self.s.main, self.records)
        self.s.stop(_running(task_id), "launched")
        return self

    def agent(self, agent_id, label, phase="", stop=True, **kwargs):
        transcript = workflow_agent(_run_dir(self.s), agent_id, label, phase, **kwargs)
        agent_start(self.s, agent_id)
        if stop:
            agent_stop(self.s, transcript, agent_id, running=_running(TASK))
        return transcript

    def notify(self, task_id=TASK, status="completed", summary="done", reply="everything reported"):
        prompt = notification(task_id, CALL, status, summary)
        self.records.append(human("h2", prompt, 40))
        write_jsonl(self.s.main, self.records)
        self.s.prompt(prompt, "p-2")
        self.records.append(assistant("m3", "msg-m3", [text(reply)], 42))
        write_jsonl(self.s.main, self.records)
        self.s.stop([], reply)
        return self


def _by_name(session, prefix):
    return [s for s in session.spans() if s["name"].startswith(prefix)]


def _entry(session, task_id=TASK):
    return session.registry().get(task_id, {})


def _trace(session, task_id=TASK):
    return _entry(session, task_id)["trace_id"]


class TestCompletedWorkflow:
    @pytest.fixture
    def flow(self, session):
        flow = Flow(session).launch()
        flow.agent("a1", "scan-1", "Scan")
        flow.agent("a2", "check-1", "Check")
        flow.agent("a3", "nophase")
        write_run_json(_run_dir(session), TASK, phases=[(1, "Scan"), (2, "Check")])
        flow.notify()
        return flow

    def test_the_workflow_span_hangs_under_the_workflow_tool_span(self, session, flow):
        [workflow] = _by_name(session, "Workflow run")
        assert workflow["name"] == "Workflow run: wf-test"
        trace = _trace(session)
        assert workflow["spanId"] == handlers._derived_span_id(trace, f"workflow:{TASK}")
        assert workflow["parentSpanId"] == handlers._derived_span_id(trace, f"tool:{CALL}")
        values = attrs(workflow)
        assert values["workflow.status"] == "completed"
        assert values["workflow.run_id"] == RUN and values["workflow.task_id"] == TASK
        assert int(values["workflow.agent_count"]) == 3
        assert workflow["status"]["code"] == 1

    def test_phase_spans_carry_the_run_json_index(self, session, flow):
        phases = {s["name"]: s for s in _by_name(session, "Phase")}
        assert set(phases) == {"Phase 1: Scan", "Phase 2: Check"}
        workflow_id = handlers._derived_span_id(_trace(session), f"workflow:{TASK}")
        assert {s["parentSpanId"] for s in phases.values()} == {workflow_id}

    def test_no_span_points_at_a_parent_that_was_never_sent(self, session, flow):
        spans = session.spans()
        ids = {s["spanId"] for s in spans}
        assert {s.get("parentSpanId") for s in spans if s.get("parentSpanId")} <= ids
        assert {s["traceId"] for s in spans} == {_trace(session)}

    def test_the_root_carries_the_final_reply_and_rollups(self, session, flow):
        [root] = [s for s in session.spans() if not s.get("parentSpanId")]
        values = attrs(root)
        assert values["output.value"] == "everything reported"
        assert int(values["workflow.count"]) == 1
        assert int(values["workflow.failed_count"]) == 0
        assert values["trace.release_reason"] == "final_reply"

    def test_workflow_and_phase_spans_carry_no_tokens(self, session, flow):
        for span in _by_name(session, "Workflow run") + _by_name(session, "Phase"):
            assert not [k for k in attrs(span) if k.startswith("llm.token_count")]

    def test_the_task_leaves_no_state_behind(self, session, flow):
        data = session.data()
        assert not [k for k in data if k.startswith("subagent_")]
        assert not list(session.dir.glob(f"state_{SESSION}__agent_*.json"))
        assert _entry(session)["status"] == "completed"
        assert _entry(session)["finalize_reason"] == "notification"

    def test_each_agent_is_sent_once(self, session, flow):
        agent_ids = [s["spanId"] for s in _by_name(session, "Agent")]
        assert len(agent_ids) == len(set(agent_ids)) == 3


class TestEndSignals:
    def test_leaving_background_tasks_without_a_run_json_does_not_finalize(self, session):
        Flow(session).launch()
        session.stop([], "still waiting")

        assert not _entry(session).get("finalized_at_ms")
        assert session.held(_trace(session))

    def test_a_run_json_seen_at_stop_finalizes_a_stopped_run(self, session):
        flow = Flow(session).launch()
        flow.agent("a1", "quick", "First")
        flow.agent("a2", "slow", "Second", stop=False)
        write_run_json(_run_dir(session), TASK, status="killed", error="Error: Workflow aborted")
        session.stop([], "stopped it")

        entry = _entry(session)
        assert entry["status"] == "stopped" and entry["finalize_reason"] == "run_json"
        [workflow] = _by_name(session, "Workflow run")
        assert workflow["status"]["code"] == 1
        assert session.held(_trace(session)) == {}

    def test_an_interrupted_agent_is_rendered_with_its_partial_work(self, session):
        flow = Flow(session).launch()
        flow.agent("a2", "slow", "Second", stop=False, handback=False)
        write_run_json(_run_dir(session), TASK, status="killed")
        session.stop([], "stopped it")

        [agent] = [s for s in _by_name(session, "Agent") if "slow" in s["name"]]
        assert attrs(agent)["workflow.agent_state"] == "interrupted"
        assert agent["status"]["code"] == 1
        assert [s for s in session.spans() if s.get("parentSpanId") == agent["spanId"]]

    def test_a_failed_agent_is_an_error_span_with_its_partial_work(self, session):
        flow = Flow(session).launch()
        flow.agent("a1", "bad-model", "Try", stop=False, handback=False)
        session.hook(
            handlers.stop_failure,
            {
                "hook_event_name": "StopFailure",
                "agent_id": "a1",
                "error": "model_not_found",
                "last_assistant_message": "API Error: model not found",
            },
        )
        write_run_json(_run_dir(session), TASK, status="failed", error="Error: deliberate failure")
        flow.notify(status="failed", summary="failed")

        [agent] = _by_name(session, "Agent")
        assert agent["status"]["code"] == 2
        assert attrs(agent)["workflow.agent_state"] == "failed"
        assert "model_not_found" in agent["status"].get("message", "")
        [workflow] = _by_name(session, "Workflow run")
        assert workflow["status"]["code"] == 2
        assert "deliberate failure" in workflow["status"]["message"]

    def test_a_failed_notification_without_a_run_json_uses_its_summary(self, session):
        Flow(session).launch().notify(status="failed", summary="Dynamic workflow failed: boom")

        [workflow] = _by_name(session, "Workflow run")
        assert attrs(workflow)["workflow.status"] == "failed"
        assert "boom" in workflow["status"]["message"]

    def test_session_end_finalizes_an_open_workflow_as_incomplete(self, session):
        Flow(session).launch()
        session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})

        [workflow] = _by_name(session, "Workflow run")
        assert attrs(workflow)["workflow.status"] == "incomplete"
        assert workflow["status"]["code"] == 2
        [root] = [s for s in session.spans() if not s.get("parentSpanId")]
        assert attrs(root)["trace.release_reason"] == "session_end"

    def test_a_second_signal_does_not_finalize_again(self, session):
        flow = Flow(session).launch()
        write_run_json(_run_dir(session), TASK)
        flow.notify()
        sent = len(session.spans())
        session.stop([], "again")

        assert len(_by_name(session, "Workflow run")) == 1
        assert len(session.spans()) == sent


class TestResume:
    def test_a_resume_finalizes_the_stopped_task_and_keeps_agents_apart(self, session):
        flow = Flow(session).launch()
        flow.agent("a1", "quick", "First")
        flow.agent("a2", "slow", "Second", stop=False)
        write_run_json(_run_dir(session), TASK, status="killed")

        resume = workflow_result("w2", RUN, _run_dir(session))
        session.prompt("resume it", "p-3")
        session.pre_tool("toolu_wf2", "Workflow", {})
        session.post_tool("toolu_wf2", "Workflow", {}, resume)
        assert _entry(session, TASK)["status"] == "stopped"
        assert _entry(session, TASK)["finalize_reason"] in {"resumed", "run_json"}

        flow.agent("a3", "slow", "Second", stop=False)
        agent_stop(session, _run_dir(session) / "agent-a3.jsonl", "a3", running=_running("w2"))
        write_run_json(_run_dir(session), "w2")
        session.stop([], "resumed run done")

        by_task = {}
        for span in _by_name(session, "Agent"):
            by_task.setdefault(attrs(span)["workflow.task_id"], []).append(attrs(span)["workflow.agent_label"])
        assert sorted(by_task[TASK]) == ["quick", "slow"]
        assert by_task["w2"] == ["slow"]
        assert len(_by_name(session, "Workflow run")) == 2


class TestMarkerAndNames:
    def test_a_completion_during_another_trace_adds_one_marker(self, session):
        Flow(session).launch()
        write_run_json(_run_dir(session), TASK)
        other = "f" * 32
        for key, value in {
            "current_trace_id": other,
            "current_trace_span_id": "e" * 16,
            "current_prompt_id": "p-live",
            "current_trace_start_time": "1794627139000",
            "trace_start_line": "0",
        }.items():
            session.state.set(key, value)
        session.prompt(notification(TASK, CALL), "p-live")

        assert _entry(session)["status"] == "completed"
        requests = json.loads(session.state.get(workflows.MARKERS_KEY) or "[]")
        assert [r["task_id"] for r in requests] == [TASK]
        assert requests[0]["origin_trace_id"] == _trace(session)

    def test_names_follow_tool_details(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        from core.common import env

        env.invalidate_caches()
        flow = Flow(session).launch()
        flow.agent("a1", "scan-1", "Scan")
        write_run_json(_run_dir(session), TASK, phases=[(1, "Scan")])
        flow.notify()

        assert [s["name"] for s in _by_name(session, "Workflow run")] == ["Workflow run"]
        assert [s["name"] for s in _by_name(session, "Phase")] == ["Phase 1"]

    def test_the_error_text_follows_tool_content(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "false")
        from core.common import env

        env.invalidate_caches()
        flow = Flow(session).launch()
        write_run_json(_run_dir(session), TASK, status="failed", error="Error at /home/u/secret.js:3")
        flow.notify(status="failed")

        [workflow] = _by_name(session, "Workflow run")
        assert "/home/u/secret.js" not in workflow["status"].get("message", "")


class TestRegistryEnd:
    def test_old_finalized_entries_are_deleted_at_session_start(self, session):
        flow = Flow(session).launch()
        write_run_json(_run_dir(session), TASK)
        flow.notify()
        registry = session.registry()
        registry[TASK]["finalized_at_ms"] = 1
        session.state.set("background_agents", json.dumps(registry))
        session.hook(handlers.session_start, {"hook_event_name": "SessionStart", "source": "resume"})

        assert TASK not in session.registry()
