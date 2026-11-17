#!/usr/bin/env python3
"""Workflow launches are registered, held and continued like background agents."""

import json
import os

import pytest
from workflow_fixtures import (
    GOLDEN_DIR,
    ROOT,
    TRACE,
    Session,
    assistant,
    attrs,
    human,
    no_workflow_session,
    notification,
    text,
    workflow_call,
    workflow_result,
    write_jsonl,
)

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers

GOLDEN = GOLDEN_DIR / "no_workflow_session.golden.json"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setenv("ATATUS_TRANSCRIPT_WAIT_MS", "0")
    return Session(tmp_path)


def _open_turn(session: Session) -> None:
    for key, value in {
        "current_trace_id": TRACE,
        "current_trace_span_id": ROOT,
        "current_trace_start_time": "1794627139000",
        "current_trace_prompt": "go",
        "current_prompt_id": "p-1",
        "trace_start_line": "0",
    }.items():
        session.state.set(key, value)


def _run_dir(session: Session, run_id: str = "wf_aaaa-111"):
    return session.subagents / "workflows" / run_id


def _launch(session: Session, task_id="w1", run_id="wf_aaaa-111", call_id="toolu_wf1", start=2, done=3):
    """A turn that launches one workflow, through PreToolUse and PostToolUse."""
    result = workflow_result(task_id, run_id, _run_dir(session, run_id))
    records = [human("h1", "run the workflow", 0), *workflow_call(call_id, result, start, done)]
    write_jsonl(session.main, records[:2])
    session.pre_tool(call_id, "Workflow", {"script": "phase('A')"})
    write_jsonl(session.main, records)
    session.post_tool(call_id, "Workflow", {"script": "phase('A')"}, result)
    return records


def _running(*task_ids):
    return [{"id": t, "type": "workflow", "status": "running", "name": "wf-test"} for t in task_ids]


class TestNoWorkflowSessionIsUnchanged:
    def test_spans_and_state_keys_match_the_recording(self, session):
        produced = no_workflow_session(session)
        if os.environ.get("ATATUS_UPDATE_GOLDEN") == "true":
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(produced, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        assert produced == json.loads(GOLDEN.read_text(encoding="utf-8"))


class TestRegistration:
    def test_post_tool_use_registers_the_launch_under_its_task_id(self, session):
        _open_turn(session)
        _launch(session)

        entry = session.registry()["w1"]
        assert entry["kind"] == "workflow"
        assert entry["trace_id"] == TRACE
        assert entry["run_id"] == "wf_aaaa-111"
        assert entry["run_dir"] == str(_run_dir(session))
        assert entry["workflow_name"] == "wf-test"
        assert entry["tool_use_id"] == "toolu_wf1"
        assert entry["tool_span_id"] == handlers._derived_span_id(TRACE, "tool:toolu_wf1")
        assert "w1" in session.held()["pending"]

    def test_the_launch_starts_at_pre_tool_use(self, session):
        _open_turn(session)
        session.state.set("tool_toolu_wf1_start", "1794627100123")
        result = workflow_result("w1", "wf_aaaa-111", _run_dir(session))
        session.post_tool("toolu_wf1", "Workflow", {}, result)

        assert session.registry()["w1"]["launch_started_ms"] == 1794627100123

    def test_the_stop_scan_does_not_overwrite_the_first_registration(self, session):
        _open_turn(session)
        _launch(session)
        first = dict(session.registry()["w1"])
        session.stop(_running("w1"))

        assert session.registry()["w1"] == first

    def test_the_stop_scan_alone_registers_a_launch(self, session):
        _open_turn(session)
        result = workflow_result("w1", "wf_aaaa-111", _run_dir(session))
        write_jsonl(session.main, [human("h1", "go", 0), *workflow_call("toolu_wf1", result, 2, 3)])
        session.stop(_running("w1"))

        entry = session.registry()["w1"]
        assert entry["kind"] == "workflow"
        assert entry["tool_span_id"] == handlers._derived_span_id(TRACE, "tool:toolu_wf1")
        assert entry["launch_started_ms"] > 0

    @pytest.mark.parametrize(
        "change",
        [
            {"status": "remote_launched"},
            {"error": "SyntaxError: Unexpected token"},
            {"runId": None},
            {"transcriptDir": None},
            {"taskId": "../escape"},
        ],
    )
    def test_a_result_that_is_not_a_clean_local_launch_is_ignored(self, session, change):
        _open_turn(session)
        result = {**workflow_result("w1", "wf_aaaa-111", _run_dir(session)), **change}
        result = {k: v for k, v in result.items() if v is not None}
        session.post_tool("toolu_wf1", "Workflow", {}, result)

        assert not any(e.get("kind") == "workflow" for e in session.registry().values())

    def test_a_string_result_is_ignored(self, session):
        _open_turn(session)
        session.post_tool("toolu_wf1", "Workflow", {}, "SyntaxError: Unexpected token")

        assert session.registry() == {}

    def test_a_launch_outside_a_turn_is_ignored(self, session):
        result = workflow_result("w1", "wf_aaaa-111", _run_dir(session))
        session.post_tool("toolu_wf1", "Workflow", {}, result)

        assert session.registry() == {}

    def test_the_switch_turns_registration_off(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_WORKFLOWS", "false")
        _open_turn(session)
        _launch(session)
        session.stop(_running("w1"))

        assert session.registry() == {}

    def test_registration_logs_one_line(self, session, capsys):
        _open_turn(session)
        _launch(session)

        lines = [line for line in capsys.readouterr().err.splitlines() if "workflow registered" in line]
        assert len(lines) == 1
        assert "task=w1" in lines[0] and "run=wf_aaaa-111" in lines[0] and f"trace={TRACE}" in lines[0]


class TestHoldAndContinuation:
    def test_the_launching_spans_are_held_while_the_workflow_runs(self, session):
        _open_turn(session)
        records = _launch(session)
        write_jsonl(session.main, records + [assistant("m2", "msg-m2", [text("launched")], 5)])
        session.stop(_running("w1"))

        held = {s["name"] for s in session.held()["spans"]}
        assert "Workflow" in held
        assert not [s for s in session.spans() if s["name"] == "Workflow"]

    def test_the_tool_span_carries_the_workflow_ids(self, session):
        _open_turn(session)
        records = _launch(session)
        write_jsonl(session.main, records + [assistant("m2", "msg-m2", [text("launched")], 5)])
        session.stop(_running("w1"))

        [tool] = [s for s in session.held()["spans"] if s["name"] == "Workflow"]
        assert tool["spanId"] == handlers._derived_span_id(TRACE, "tool:toolu_wf1")
        values = attrs(tool)
        assert values["workflow.run_id"] == "wf_aaaa-111"
        assert values["workflow.task_id"] == "w1"
        assert values["workflow.name"] == "wf-test"

    def test_the_workflow_name_follows_tool_details(self, session, monkeypatch):
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        from core.common import env

        env.invalidate_caches()
        _open_turn(session)
        records = _launch(session)
        write_jsonl(session.main, records + [assistant("m2", "msg-m2", [text("launched")], 5)])
        session.stop(_running("w1"))

        [tool] = [s for s in session.held()["spans"] if s["name"] == "Workflow"]
        assert "wf-test" not in attrs(tool)["workflow.name"]
        assert attrs(tool)["workflow.task_id"] == "w1"

    def test_the_tool_output_keeps_the_launch_result(self, session):
        _open_turn(session)
        records = _launch(session)
        write_jsonl(session.main, records + [assistant("m2", "msg-m2", [text("launched")], 5)])
        session.stop(_running("w1"))

        [tool] = [s for s in session.held()["spans"] if s["name"] == "Workflow"]
        output = json.loads(attrs(tool)["output.value"])
        assert output["toolUseResult"]["taskId"] == "w1"

    @pytest.mark.parametrize("by", ["task_id", "tool_use_id"])
    def test_the_completion_continues_the_launching_trace(self, session, by):
        _open_turn(session)
        records = _launch(session)
        records += [assistant("m2", "msg-m2", [text("launched")], 5)]
        write_jsonl(session.main, records)
        session.stop(_running("w1"))

        prompt = notification("w1", "toolu_wf1") if by == "task_id" else notification("other", "toolu_wf1")
        write_jsonl(session.main, records + [human("h2", prompt, 20)])
        session.prompt(prompt, "p-2")

        assert session.state.get("current_trace_id") == TRACE
        assert session.state.get("current_trace_span_id") == handlers._derived_span_id(TRACE, "tool:toolu_wf1")

    def test_a_trace_with_two_parallel_workflows_waits_for_both(self, session):
        _open_turn(session)
        result_a = workflow_result("w1", "wf_aaaa-111", _run_dir(session, "wf_aaaa-111"))
        result_b = workflow_result("w2", "wf_bbbb-222", _run_dir(session, "wf_bbbb-222"))
        records = [human("h1", "two at once", 0)]
        records += workflow_call("toolu_wf1", result_a, 2, 3) + workflow_call("toolu_wf2", result_b, 2, 3)
        records += [assistant("m2", "msg-m2", [text("launched two")], 5)]
        write_jsonl(session.main, records)
        session.post_tool("toolu_wf1", "Workflow", {}, result_a)
        session.post_tool("toolu_wf2", "Workflow", {}, result_b)
        session.stop(_running("w1", "w2"))
        session.stop(_running("w2"))

        assert session.held()

    def test_payloads_without_background_tasks_keep_the_trace_held(self, session):
        _open_turn(session)
        records = _launch(session)
        write_jsonl(session.main, records + [assistant("m2", "msg-m2", [text("launched")], 5)])
        session.stop(None)

        assert session.held()
