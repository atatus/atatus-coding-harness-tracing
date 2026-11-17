"""Claude Code's Workflow tool: launches, run files and the spans built from them.

Claude Code's workflow files are undocumented, so every read of them lives here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

from core.common import error

ENV_SWITCH = "ATATUS_TRACE_WORKFLOWS"
WORKFLOW_KIND = "workflow"

AGENT_STOP_KEY = "workflow_stop"
AGENT_FAILURE_KEY = "workflow_failure"
AGENT_EXPORTED_KEY = "workflow_exported"

COMPUTED_TASK_PREFIX = "[Workflow harness — computed task]"

_AGENT_FILE = re.compile(r"agent-(.+)\.jsonl")
_SEPARATORS = re.compile(r"[\\/]")
_drift_seen: set = set()

UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "true").lower() != "false"


def safe_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not UNSAFE_NAME.search(value)


@dataclass(frozen=True)
class Launch:
    task_id: str
    run_id: str
    run_dir: str
    workflow_name: str
    tool_use_id: str


def launch_from_result(result: object, tool_use_id: str) -> Optional[Launch]:
    """A clean local launch only: a syntax error, a remote launch or a malformed result is
    not a workflow the tracer can follow."""
    if not isinstance(result, dict) or result.get("status") != "async_launched" or result.get("error"):
        return None
    task_id, run_id, run_dir = result.get("taskId"), result.get("runId"), result.get("transcriptDir")
    if not (isinstance(task_id, str) and isinstance(run_id, str) and isinstance(run_dir, str) and run_dir):
        return None
    if not (safe_name(task_id) and safe_name(run_id)):
        return None
    name = result.get("workflowName")
    return Launch(
        task_id=task_id,
        run_id=run_id,
        run_dir=run_dir,
        workflow_name=name if isinstance(name, str) else "",
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else "",
    )


def wf_log(event: str, **fields: object) -> None:
    detail = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    error(f"workflow {event} {detail}".rstrip())


@dataclass(frozen=True)
class AgentPath:
    run_dir: Path
    run_id: str
    agent_id: str


def classify_agent_path(path: object) -> Optional[AgentPath]:
    """A workflow agent's transcript is ``…/subagents/workflows/<runId>/agent-<agentId>.jsonl``.
    The path is the only reliable mark: a script can set any agent type."""
    if not isinstance(path, str) or not path:
        return None
    parts = _SEPARATORS.split(path)
    if len(parts) < 4 or parts[-4] != "subagents" or parts[-3] != "workflows":
        return None
    match = _AGENT_FILE.fullmatch(parts[-1])
    run_id = parts[-2]
    if match is None or not safe_name(run_id) or not safe_name(match.group(1)):
        return None
    return AgentPath(Path(path).parent, run_id, match.group(1))


def in_workflow_run_dir(path: object) -> bool:
    if not isinstance(path, str):
        return False
    parts = _SEPARATORS.split(path)
    return any(parts[i] == "subagents" and parts[i + 1] == "workflows" for i in range(len(parts) - 1))


@dataclass(frozen=True)
class AgentMeta:
    label: str
    phase: str
    agent_type: str


def read_meta(run_dir: Path, agent_id: str) -> AgentMeta:
    data = _read_json(run_dir / f"agent-{agent_id}.meta.json", "meta")
    if not isinstance(data, dict):
        return AgentMeta("", "", "")

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return AgentMeta(text("description"), text("workflowPhase"), text("agentType"))


def run_agent_ids(run_dir: Path) -> list:
    try:
        names = sorted(p.name for p in run_dir.glob("agent-*.jsonl"))
    except OSError:
        return []
    return [m.group(1) for m in map(_AGENT_FILE.fullmatch, names) if m is not None and safe_name(m.group(1))]


def agent_input(transcript: Path) -> str:
    """The task a workflow script gave the agent. The transcript opens with the relayed user
    request; the agent's own task is the computed-task record after it."""
    first = ""
    for record in _records(transcript):
        message = record.get("message")
        if record.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if content.startswith(COMPUTED_TASK_PREFIX):
            return content
        first = first or content
    return first


def transcript_start_ms(transcript: Path) -> Optional[int]:
    for record in _records(transcript):
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                return None
    return None


def task_for_agent(tasks: Mapping[str, dict], agent_start_ms: Optional[int]) -> Optional[str]:
    """The tasks of one run never overlap (a resume starts after the previous task ended), so an
    agent belongs to the task with the latest launch start at or before its own start."""
    ordered = sorted((int(entry.get("launch_started_ms") or 0), task_id) for task_id, entry in tasks.items())
    if not ordered:
        return None
    if agent_start_ms is None:
        return ordered[-1][1]
    chosen = ordered[0][1]
    for started, task_id in ordered:
        if started <= agent_start_ms:
            chosen = task_id
    return chosen


@dataclass(frozen=True)
class Names:
    workflow: str
    phase: str
    agent: str


def display_names(
    workflow_name: str, phase_title: str, phase_index: int, label: str, ordinal: int, tool_details: bool
) -> Names:
    """Script-authored text can embed paths, so it is shown only with tool details on. The
    ordinal always stays, or the dashboard folds same-named siblings into one bucket."""
    if not tool_details:
        return Names("Workflow run", f"Phase {phase_index}", f"Agent {ordinal}")
    return Names(
        f"Workflow run: {workflow_name}" if workflow_name else "Workflow run",
        f"Phase {phase_index}: {phase_title}" if phase_title else f"Phase {phase_index}",
        f"Agent {ordinal}: {label}" if label else f"Agent {ordinal}",
    )


def drift(kind: str, detail: object) -> None:
    if kind in _drift_seen:
        return
    _drift_seen.add(kind)
    wf_log("drift", kind=kind, detail=detail)


def _read_json(path: Path, kind: str) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        drift(kind, path.name)
        return None


def _records(transcript: Path):
    try:
        with transcript.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return
