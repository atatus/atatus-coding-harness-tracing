#!/usr/bin/env python3
"""A background shell command's completion continues the trace that launched it."""

import pytest
from workflow_fixtures import (
    ROOT,
    TRACE,
    Session,
    assistant,
    human,
    notification,
    text,
    tool_result,
    tool_use,
    write_jsonl,
)

import tracing.claude_code.hooks.adapter as adapter
import tracing.claude_code.hooks.handlers as handlers

SHELL = "by5eujbqc"
CALL = "toolu_sleep"
SLEEP = {"command": "sleep 30", "run_in_background": True}


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


def _shell_result(task_id: str = SHELL) -> dict:
    return {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
        "backgroundTaskId": task_id,
    }


def _turn(result: dict) -> list:
    return [
        human("h1", "wait in the background", 0),
        assistant("m1", "msg-m1", [tool_use(CALL, "Bash", SLEEP)], 2),
        tool_result("r1", CALL, "Command running in background with ID: by5eujbqc", 3, result),
        assistant("m2", "msg-m2", [text("waiting")], 5),
    ]


def _running(*ids):
    return [{"id": i, "type": "local_bash", "status": "running"} for i in ids]


class TestRegistration:
    def test_post_tool_use_registers_the_shell_under_its_task_id(self, session):
        _open_turn(session)
        session.post_tool(CALL, "Bash", SLEEP, _shell_result())

        entry = session.registry()[SHELL]
        assert entry["kind"] == "shell"
        assert entry["trace_id"] == TRACE
        assert entry["tool_span_id"] == handlers._derived_span_id(TRACE, f"tool:{CALL}")
        assert SHELL in session.held()["pending"]

    def test_the_stop_scan_alone_registers_the_shell(self, session):
        _open_turn(session)
        write_jsonl(session.main, _turn(_shell_result()))
        session.stop(_running(SHELL))

        assert session.registry()[SHELL]["kind"] == "shell"

    def test_a_foreground_command_is_not_registered(self, session):
        _open_turn(session)
        session.post_tool(CALL, "Bash", {"command": "ls"}, {"stdout": "a b", "stderr": "", "interrupted": False})

        assert session.registry() == {}

    def test_an_unsafe_task_id_is_not_registered(self, session):
        _open_turn(session)
        session.post_tool(CALL, "Bash", SLEEP, _shell_result("../escape"))

        assert session.registry() == {}


class TestContinuation:
    def test_the_completion_continues_the_launching_trace(self, session):
        _open_turn(session)
        records = _turn(_shell_result())
        write_jsonl(session.main, records)
        session.post_tool(CALL, "Bash", SLEEP, _shell_result())
        session.stop(_running(SHELL))
        trace_count = session.state.get("trace_count")

        prompt = notification(SHELL, CALL, summary='Background command "sleep 30" completed (exit code 0)')
        write_jsonl(session.main, records + [human("h2", prompt, 35)])
        session.prompt(prompt, "p-2")

        assert session.state.get("current_trace_id") == TRACE
        assert session.state.get("current_trace_span_id") == handlers._derived_span_id(TRACE, f"tool:{CALL}")
        assert session.state.get("trace_count") == trace_count

    def test_the_trace_is_held_until_the_reaction_is_exported(self, session):
        _open_turn(session)
        records = _turn(_shell_result())
        write_jsonl(session.main, records)
        session.post_tool(CALL, "Bash", SLEEP, _shell_result())
        session.stop(_running(SHELL))
        assert session.held()["spans"]

        prompt = notification(SHELL, CALL)
        reaction = records + [human("h2", prompt, 35), assistant("m3", "msg-m3", [text("it finished")], 37)]
        write_jsonl(session.main, records + [human("h2", prompt, 35)])
        session.prompt(prompt, "p-2")
        write_jsonl(session.main, reaction)
        session.stop([], "it finished")

        assert session.held() == {}
        assert {s["traceId"] for s in session.spans()} == {TRACE}
