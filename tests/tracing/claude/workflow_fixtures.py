"""Synthetic Claude Code sessions for the Workflow tool tests.

The shapes copy real hook payloads and transcripts; every value is made up.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import tracing.claude_code.hooks.handlers as handlers
from core.common import StateManager

TRACE = "c" * 32
ROOT = "d" * 16
SESSION = "s1"

GOLDEN_DIR = Path(__file__).parent / "data"


class Session:
    """Drives the real hook entry points against one temp session, capturing every send."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self.state = StateManager(tmp_path, tmp_path / f"state_{SESSION}.json", tmp_path / f".lock_{SESSION}")
        self.state.init_state()
        self.state.set("session_id", SESSION)
        self.state.set("trace_count", "1")
        self.payloads: list[dict] = []
        self.main = tmp_path / "proj" / f"{SESSION}.jsonl"
        self.subagents = tmp_path / "proj" / SESSION / "subagents"

    def hook(self, entry, payload: dict) -> None:
        payload = {"session_id": SESSION, **payload}
        with (
            mock.patch.object(handlers, "resolve_session", lambda *a, **k: self.state),
            mock.patch.object(handlers, "send_span", lambda p: self.payloads.append(p) or True),
            mock.patch.object(handlers, "gc_stale_state_files", lambda *a, **k: None),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            entry()

    def spans(self) -> list[dict]:
        return [s for p in self.payloads for rs in p["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]

    def data(self) -> dict:
        return json.loads(self.state.state_file.read_text(encoding="utf-8"))

    def registry(self) -> dict:
        return json.loads(self.state.get("background_agents") or "{}")

    def held(self, trace_id: str = TRACE) -> dict:
        return json.loads(self.state.get("held_spans") or "{}").get(trace_id, {})

    def prompt(self, text: str, prompt_id: str) -> None:
        self.hook(
            handlers.user_prompt_submit,
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": text,
                "prompt_id": prompt_id,
                "transcript_path": str(self.main),
            },
        )

    def pre_tool(self, tool_use_id: str, tool_name: str, tool_input: dict) -> None:
        self.hook(
            handlers.pre_tool_use,
            {
                "hook_event_name": "PreToolUse",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "transcript_path": str(self.main),
            },
        )

    def post_tool(self, tool_use_id: str, tool_name: str, tool_input: dict, tool_response) -> None:
        self.hook(
            handlers.post_tool_use,
            {
                "hook_event_name": "PostToolUse",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_response": tool_response,
                "transcript_path": str(self.main),
            },
        )

    def stop(self, running: "list[dict] | None" = None, message: str = "done") -> None:
        payload = {"hook_event_name": "Stop", "transcript_path": str(self.main), "last_assistant_message": message}
        if running is not None:
            payload["background_tasks"] = running
        self.hook(handlers.stop, payload)


def attrs(span: dict) -> dict:
    return {a["key"]: list(a["value"].values())[0] for a in span["attributes"]}


def ts(seconds: int) -> str:
    return f"2026-11-14T03:{32 + seconds // 60:02d}:{seconds % 60:02d}.000Z"


def write_jsonl(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def human(uuid: str, text: str, seconds: int) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": ts(seconds), "message": {"role": "user", "content": text}}


def assistant(uuid: str, message_id: str, blocks: list, seconds: int) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts(seconds),
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": "m",
            "content": blocks,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def tool_use(call_id: str, name: str, tool_input: "dict | None" = None) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input or {}}


def tool_result(uuid: str, call_id: str, content, seconds: int, tool_use_result=None) -> dict:
    record = {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts(seconds),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content}]},
    }
    if tool_use_result is not None:
        record["toolUseResult"] = tool_use_result
    return record


def workflow_result(task_id: str, run_id: str, run_dir: Path, name: str = "wf-test") -> dict:
    """The `Workflow` tool's result (debug run 1, `PostToolUse_519289`)."""
    return {
        "status": "async_launched",
        "taskId": task_id,
        "taskType": "local_workflow",
        "workflowName": name,
        "runId": run_id,
        "summary": f"Dynamic workflow {name!r} launched",
        "transcriptDir": str(run_dir),
        "scriptPath": str(run_dir.parent.parent.parent / "workflows" / "scripts" / f"{name}-{run_id}.js"),
    }


def workflow_call(call_id: str, result, start: int, done: int) -> list:
    return [
        assistant(f"use-{call_id}", f"msg-{call_id}", [tool_use(call_id, "Workflow", {"script": "phase('A')"})], start),
        tool_result(f"res-{call_id}", call_id, [text("Workflow launched in background")], done, result),
    ]


def background_call(call_id: str, agent_id: str, start: int, done: int) -> list:
    return [
        assistant(
            f"use-{call_id}",
            f"msg-{call_id}",
            [tool_use(call_id, "Agent", {"subagent_type": "general-purpose", "prompt": "go"})],
            start,
        ),
        tool_result(
            f"res-{call_id}", call_id, [text("launched")], done, {"status": "async_launched", "agentId": agent_id}
        ),
    ]


def notification(task_id: str, tool_use_id: str = "", status: str = "completed", summary: str = "done") -> str:
    tool_use_tag = f"\n<tool-use-id>{tool_use_id}</tool-use-id>" if tool_use_id else ""
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>{tool_use_tag}\n<output-file>/tmp/{task_id}</output-file>"
        f"\n<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>"
    )


def canonical(payloads: list[dict]) -> list[dict]:
    """Spans in send order with ids replaced by name-based labels and times dropped, so the
    same session compares equal across runs whatever ids and clocks it drew."""
    spans = [s for p in payloads for rs in p["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]
    labels: dict[str, str] = {}
    seen: dict[str, int] = {}
    for span in spans:
        if span["spanId"] not in labels:
            seen[span["name"]] = seen.get(span["name"], 0) + 1
            labels[span["spanId"]] = f"{span['name']}#{seen[span['name']]}"
    traces: dict[str, str] = {}
    out = []
    for span in spans:
        traces.setdefault(span["traceId"], f"trace#{len(traces) + 1}")
        parent = span.get("parentSpanId") or ""
        out.append(
            {
                "label": labels[span["spanId"]],
                "trace": traces[span["traceId"]],
                "parent": labels.get(parent, "<external>" if parent else ""),
                "status": span.get("status", {}).get("code"),
                "attributes": {k: v for k, v in sorted(attrs(span).items()) if k not in VOLATILE_ATTRIBUTES},
            }
        )
    return out


VOLATILE_ATTRIBUTES = {"session.id", "trace.start_time", "agent.version"}


def no_workflow_session(session: Session) -> dict:
    """A session that exercises every hook the Workflow changes touch, with no workflow in it:
    session start, a main-thread tool, a background agent that reports back, a final reply."""
    agent = write_jsonl(
        session.subagents / "agent-bg.jsonl",
        [
            human("bg0", "go", 12),
            assistant("bg1", "msg-bg-1", [tool_use("toolu_bg_bash", "Bash", {"command": "ls"})], 13),
            tool_result("bg2", "toolu_bg_bash", "a b", 14),
            assistant("bg3", "msg-bg-2", [text("listed")], 15),
        ],
    )
    agent.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "requestShape": "background", "toolUseId": "toolu_bg"}),
        encoding="utf-8",
    )
    turn = [
        human("h1", "list the files twice", 0),
        assistant("m1", "msg-m1", [tool_use("toolu_bash", "Bash", {"command": "ls"})], 2),
        tool_result("r1", "toolu_bash", "a b", 3),
        *background_call("toolu_bg", "bg", 5, 6),
        assistant("m2", "msg-m2", [text("started a background agent")], 8),
    ]
    write_jsonl(session.main, [])
    session.hook(handlers.session_start, {"hook_event_name": "SessionStart", "source": "startup"})
    session.prompt("list the files twice", "p-1")
    write_jsonl(session.main, turn[:2])
    session.pre_tool("toolu_bash", "Bash", {"command": "ls"})
    write_jsonl(session.main, turn[:3])
    session.post_tool("toolu_bash", "Bash", {"command": "ls"}, {"stdout": "a b"})
    write_jsonl(session.main, turn)
    session.stop([{"id": "bg", "type": "subagent", "status": "running"}], "started a background agent")
    session.hook(
        handlers.subagent_start, {"hook_event_name": "SubagentStart", "agent_id": "bg", "agent_type": "general-purpose"}
    )
    session.hook(
        handlers.subagent_stop,
        {
            "hook_event_name": "SubagentStop",
            "agent_id": "bg",
            "agent_type": "general-purpose",
            "agent_transcript_path": str(agent),
            "last_assistant_message": "listed",
            "background_tasks": [{"id": "bg", "type": "subagent", "status": "running"}],
        },
    )
    reaction = turn + [human("h2", notification("bg", "toolu_bg"), 20)]
    write_jsonl(session.main, reaction)
    session.prompt(notification("bg", "toolu_bg"), "p-2")
    write_jsonl(session.main, reaction + [assistant("m3", "msg-m3", [text("both listings match")], 22)])
    session.stop([], "both listings match")
    state_keys = sorted(session.data())
    session.hook(handlers.session_end, {"hook_event_name": "SessionEnd", "reason": "exit"})
    return {"spans": canonical(session.payloads), "state_keys": state_keys}


USER_REQUEST = "[Workflow harness — user request] The harness relays, verbatim and indented below, the user request."
COMPUTED_TASK = "[Workflow harness — computed task] The task text below was computed at runtime by a workflow script."


def workflow_agent(
    run_dir: Path,
    agent_id: str,
    label: str,
    phase: str = "",
    answer: str = "b",
    start: int = 10,
    handback: bool = True,
) -> Path:
    """One workflow agent's transcript and meta file, shaped like debug run 5."""
    records = [
        human(f"{agent_id}-u0", USER_REQUEST, start),
        human(f"{agent_id}-u1", f"{COMPUTED_TASK}\n\nRun `echo {answer}` with Bash.", start),
        assistant(
            f"{agent_id}-a1",
            f"msg-{agent_id}-1",
            [tool_use(f"toolu_{agent_id}_bash", "Bash", {"command": f"echo {answer}"})],
            start + 1,
        ),
        tool_result(f"{agent_id}-r1", f"toolu_{agent_id}_bash", answer, start + 2),
    ]
    if handback:
        records += [
            assistant(
                f"{agent_id}-a2",
                f"msg-{agent_id}-2",
                [tool_use(f"toolu_{agent_id}_hb", "SubagentHandback", {"message": answer})],
                start + 3,
            ),
            tool_result(f"{agent_id}-r2", f"toolu_{agent_id}_hb", "", start + 4),
        ]
    else:
        records.append(assistant(f"{agent_id}-a2", f"msg-{agent_id}-2", [text(f"last words {answer}")], start + 3))
    transcript = write_jsonl(run_dir / f"agent-{agent_id}.jsonl", records)
    meta = {"agentType": "workflow-subagent", "description": label, "spawnDepth": 1, "requestShape": "foreground"}
    if phase:
        meta["workflowPhase"] = phase
    transcript.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return transcript


def agent_start(session: Session, agent_id: str, agent_type: str = "workflow-subagent") -> None:
    session.hook(
        handlers.subagent_start, {"hook_event_name": "SubagentStart", "agent_id": agent_id, "agent_type": agent_type}
    )


def agent_stop(
    session: Session,
    transcript: Path,
    agent_id: str,
    agent_type: str = "workflow-subagent",
    running: "list[dict] | None" = None,
) -> None:
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "agent_transcript_path": str(transcript),
        "transcript_path": str(session.main),
    }
    if running is not None:
        payload["background_tasks"] = running
    session.hook(handlers.subagent_stop, payload)
