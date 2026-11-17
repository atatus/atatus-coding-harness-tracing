#!/usr/bin/env python3
"""Each workflow agent is exported by its own SubagentStop hook, under its phase or workflow span."""

import json

import pytest
from workflow_fixtures import (
    SESSION,
    TRACE,
    Session,
    agent_start,
    agent_stop,
    attrs,
    workflow_agent,
)

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers
from tracing.claude_code.hooks import workflows

RUN = "wf_aaaa-111"
TASK = "w1"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "0")
    s = Session(tmp_path)
    s.state.set("user_id", "u-1")
    return s


def _run_dir(session, run_id=RUN):
    return session.subagents / "workflows" / run_id


def _register(session, task_id=TASK, run_id=RUN, launch_started_ms=1000, name="wf-test", **extra):
    launch = workflows.Launch(task_id, run_id, str(_run_dir(session, run_id)), name, f"toolu_{task_id}")
    entry = handlers._workflow_entry(
        launch, TRACE, handlers._derived_span_id(TRACE, f"tool:toolu_{task_id}"), "1", launch_started_ms, 1001
    )
    entry.update(extra)
    handlers._store_background_launches(session.state, TRACE, {task_id: entry})


def _agent_file(session, agent_id):
    return session.dir / f"state_{SESSION}__agent_{agent_id}.json"


def _agent_data(session, agent_id):
    return json.loads(_agent_file(session, agent_id).read_text(encoding="utf-8"))


def _set_start(session, agent_id, start_ms):
    path = _agent_file(session, agent_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["start_time"] = str(start_ms)
    path.write_text(json.dumps(data), encoding="utf-8")


def _stop_agent(session, agent_id="a1", label="scan-1", phase="Scan", **kwargs):
    transcript = workflow_agent(_run_dir(session), agent_id, label, phase, **kwargs)
    agent_start(session, agent_id)
    agent_stop(session, transcript, agent_id)
    return transcript


def _agent_spans(session):
    return [s for s in session.spans() if s["name"].startswith("Agent")]


class TestAgentExport:
    def test_the_agent_span_hangs_under_its_phase_span(self, session):
        _register(session)
        _stop_agent(session)

        [agent] = _agent_spans(session)
        assert agent["name"] == "Agent 1: scan-1"
        assert agent["traceId"] == TRACE
        assert agent["spanId"] == handlers._derived_span_id(TRACE, "agent:a1")
        assert agent["parentSpanId"] == handlers._derived_span_id(TRACE, f"phase:{TASK}:Scan")
        values = attrs(agent)
        assert values["output.value"] == "b"
        assert values["workflow.run_id"] == RUN
        assert values["workflow.task_id"] == TASK
        assert values["workflow.name"] == "wf-test"
        assert values["workflow.phase"] == "Scan"
        assert values["workflow.agent_label"] == "scan-1"
        assert values["workflow.agent_state"] == "completed"
        assert values["subagent.type"] == "workflow-subagent"
        assert values["user.id"] == "u-1"

    def test_an_agent_without_a_phase_hangs_under_the_workflow_span(self, session):
        _register(session)
        _stop_agent(session, label="nophase", phase="")

        [agent] = _agent_spans(session)
        assert agent["parentSpanId"] == handlers._derived_span_id(TRACE, f"workflow:{TASK}")

    def test_the_agent_work_is_under_the_agent_span(self, session):
        _register(session)
        _stop_agent(session)

        spans = session.spans()
        [agent] = _agent_spans(session)
        [bash] = [s for s in spans if s["name"] == "Bash"]
        llm_ids = {s["spanId"] for s in spans if s.get("parentSpanId") == agent["spanId"]}
        assert bash["parentSpanId"] in llm_ids

    def test_tokens_match_the_transcript(self, session):
        _register(session)
        _stop_agent(session)

        prompt_tokens = [attrs(s).get("llm.token_count.prompt") for s in session.spans()]
        assert sum(int(t) for t in prompt_tokens if t is not None) == 20

    def test_the_hook_leaves_the_main_state_untouched(self, session):
        _register(session)
        transcript = workflow_agent(_run_dir(session), "a1", "scan-1", "Scan")
        agent_start(session, "a1")
        before = session.state.state_file.read_bytes()
        agent_stop(session, transcript, "a1")

        assert session.state.state_file.read_bytes() == before
        assert _agent_spans(session)

    def test_the_agent_file_survives_with_its_records(self, session):
        _register(session)
        _stop_agent(session)

        data = _agent_data(session, "a1")
        assert json.loads(data[workflows.AGENT_STOP_KEY])["run_id"] == RUN
        exported = json.loads(data[workflows.AGENT_EXPORTED_KEY])
        assert exported["by"] == "stop" and exported["spans"] >= 3

    def test_a_second_stop_for_the_same_agent_sends_nothing(self, session):
        _register(session)
        transcript = _stop_agent(session)
        sent = len(session.spans())
        agent_stop(session, transcript, "a1")

        assert len(session.spans()) == sent

    def test_every_send_goes_through_the_detached_sender(self, session, monkeypatch):
        calls = []
        monkeypatch.setattr(handlers, "_send_span_async", lambda payload, *a, **k: calls.append(payload))
        _register(session)
        _stop_agent(session)

        assert len(calls) == 1
        assert not session.payloads

    def test_the_hook_logs_one_exported_line(self, session, capsys):
        _register(session)
        _stop_agent(session)

        lines = [line for line in capsys.readouterr().err.splitlines() if "workflow exported" in line]
        assert len(lines) == 1 and "task=w1" in lines[0] and "agent=a1" in lines[0]


class TestNamesAndOutput:
    def test_the_ordinal_follows_start_order_within_the_task(self, session):
        _register(session)
        for i, agent_id in enumerate(("a1", "a2", "a3")):
            workflow_agent(_run_dir(session), agent_id, f"label-{agent_id}", "Scan")
            agent_start(session, agent_id)
            _set_start(session, agent_id, 2000 + i)
        agent_stop(session, _run_dir(session) / "agent-a3.jsonl", "a3")

        [agent] = _agent_spans(session)
        assert agent["name"] == "Agent 3: label-a3"

    def test_names_follow_tool_details(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        from core.common import env

        env.invalidate_caches()
        _register(session)
        _stop_agent(session)

        [agent] = _agent_spans(session)
        assert agent["name"] == "Agent 1"
        values = attrs(agent)
        assert "scan-1" not in values["workflow.agent_label"]
        assert "Scan" not in values["workflow.phase"]
        assert "wf-test" not in values["workflow.name"]

    def test_without_a_hand_back_the_output_is_the_last_reply(self, session):
        _register(session)
        _stop_agent(session, handback=False, answer="z")

        [agent] = _agent_spans(session)
        assert attrs(agent)["output.value"] == "last words z"

    def test_the_input_is_the_computed_task(self, session):
        _register(session)
        _stop_agent(session)

        [agent] = _agent_spans(session)
        assert "computed task" in attrs(agent)["input.value"]


class TestAttribution:
    def test_after_a_resume_an_agent_belongs_to_the_task_that_started_before_it(self, session):
        _register(session, "w1", launch_started_ms=1000, finalized_at_ms=4000, status="stopped")
        _register(session, "w2", launch_started_ms=5000)
        transcript = workflow_agent(_run_dir(session), "a9", "slow", "Second")
        agent_start(session, "a9")
        _set_start(session, "a9", 6000)
        agent_stop(session, transcript, "a9")

        [agent] = _agent_spans(session)
        assert attrs(agent)["workflow.task_id"] == "w2"
        assert agent["parentSpanId"] == handlers._derived_span_id(TRACE, "phase:w2:Second")

    def test_an_agent_of_a_finalized_task_is_dropped(self, session, capsys):
        _register(session, finalized_at_ms=4000, status="stopped")
        transcript = workflow_agent(_run_dir(session), "a1", "scan-1", "Scan")
        agent_start(session, "a1")
        _set_start(session, "a1", 2000)
        agent_stop(session, transcript, "a1")

        assert not _agent_spans(session)
        assert "workflow dropped" in capsys.readouterr().err

    def test_an_agent_with_no_registered_launch_is_deferred(self, session):
        _stop_agent(session)

        assert not _agent_spans(session)
        assert workflows.AGENT_STOP_KEY in _agent_data(session, "a1")
        assert workflows.AGENT_EXPORTED_KEY not in _agent_data(session, "a1")


class TestClassification:
    def test_a_scripted_agent_type_under_a_workflow_path_takes_the_workflow_branch(self, session):
        _register(session)
        transcript = workflow_agent(_run_dir(session), "a1", "scan-1", "Scan")
        agent_start(session, "a1", agent_type="general-purpose")
        agent_stop(session, transcript, "a1", agent_type="general-purpose")

        assert _agent_spans(session)

    def test_a_workflow_agent_type_outside_a_run_directory_does_not(self, session):
        _register(session)
        transcript = workflow_agent(session.subagents, "a1", "scan-1", "Scan")
        agent_start(session, "a1")
        agent_stop(session, transcript, "a1")

        assert not _agent_spans(session)

    def test_the_switch_turns_the_workflow_branch_off(self, session, monkeypatch):
        _register(session)
        monkeypatch.setenv("ATATUS_TRACE_WORKFLOWS", "false")
        _stop_agent(session)

        assert not _agent_spans(session)


class TestStopFailure:
    def test_a_failed_agent_records_its_failure_in_its_own_file(self, session):
        agent_start(session, "a1")
        session.hook(
            handlers.stop_failure,
            {
                "hook_event_name": "StopFailure",
                "agent_id": "a1",
                "agent_type": "workflow-subagent",
                "error": "model_not_found",
                "last_assistant_message": "API Error: model not found",
            },
        )

        failure = json.loads(_agent_data(session, "a1")[workflows.AGENT_FAILURE_KEY])
        assert failure["error"] == "model_not_found"
        assert failure["message"] == "API Error: model not found"
        assert not session.spans()

    def test_a_failure_with_no_agent_file_creates_none(self, session):
        session.hook(
            handlers.stop_failure,
            {"hook_event_name": "StopFailure", "agent_id": "zz9", "error": "x", "last_assistant_message": "y"},
        )

        assert not _agent_file(session, "zz9").exists()


class TestDebris:
    def test_stop_drops_pending_subagents_inside_workflow_run_directories(self, session):
        workflow_path = str(_run_dir(session) / "agent-old.jsonl")
        other_path = str(session.subagents / "agent-keep.jsonl")
        session.state.set(
            "pending_subagents",
            json.dumps({"old": {"transcript_path": workflow_path}, "keep": {"transcript_path": other_path}}),
        )
        session.state.set("current_trace_id", TRACE)
        session.state.set("current_trace_span_id", "d" * 16)
        session.main.parent.mkdir(parents=True, exist_ok=True)
        session.main.write_text("", encoding="utf-8")
        session.stop([])

        assert set(json.loads(session.state.get("pending_subagents") or "{}")) <= {"keep"}

    def test_workflow_paths_are_recognised_with_windows_separators(self):
        assert workflows.in_workflow_run_dir(r"C:\Users\u\.claude\projects\p\s\subagents\workflows\wf_a\agent-x.jsonl")
        assert not workflows.in_workflow_run_dir(r"C:\Users\u\.claude\projects\p\s\subagents\agent-x.jsonl")


class TestNonCreatingReaders:
    def test_a_missing_agent_file_reads_as_none_and_is_not_created(self, session):
        assert adapter.existing_agent_state(SESSION, "nope") is None
        assert not _agent_file(session, "nope").exists()

    def test_a_missing_state_file_reads_as_none(self, session):
        assert adapter.existing_state("missing-session") is None
        assert not (session.dir / "state_missing-session.json").exists()

    def test_an_existing_agent_file_is_returned(self, session):
        agent_start(session, "a1")
        state = adapter.existing_agent_state(SESSION, "a1")
        assert state is not None and state.get("start_time")
