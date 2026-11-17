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

from core.common import build_span, error

ENV_SWITCH = "ATATUS_TRACE_WORKFLOWS"
WORKFLOW_KIND = "workflow"

AGENT_STOP_KEY = "workflow_stop"
AGENT_FAILURE_KEY = "workflow_failure"
AGENT_EXPORTED_KEY = "workflow_exported"

MARKERS_KEY = "workflow_markers"
REGISTRY_TTL_MS = 24 * 3600 * 1000

SWEEP_IDLE_S = 3600
SWEEP_MAX_AGE_S = 7 * 24 * 3600
SWEEP_MAX_SESSIONS = 50
SWEEP_MAX_CANDIDATES = 500

COMPUTED_TASK_PREFIX = "[Workflow harness — computed task]"

_RUN_STATUS = {"completed": "completed", "failed": "failed", "killed": "stopped"}
_NOTIFICATION = re.compile(r"\s*<task-notification>")
_TAG = {
    name: re.compile(rf"<{name}>\s*(.*?)\s*</{name}>", re.DOTALL)
    for name in ("task-id", "tool-use-id", "status", "summary")
}

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


@dataclass(frozen=True)
class RunJson:
    task_id: str
    status: str
    error: str
    start_ms: Optional[int]
    duration_ms: Optional[int]
    total_tokens: Optional[int]
    total_tool_calls: Optional[int]
    phases: tuple


def read_run_json(run_dir: Path, run_id: str, task_id: str) -> Optional[RunJson]:
    """Claude Code writes it when a run ends and overwrites it on a resume, so it describes the
    task it names and no other."""
    try:
        path = run_dir.parents[1].parent / "workflows" / f"{run_id}.json"
    except IndexError:
        return None
    data = _read_json(path, "run_json")
    if not isinstance(data, dict) or data.get("taskId") != task_id:
        return None
    status = data.get("status")
    if not isinstance(status, str):
        drift("run_json", "status")
        return None
    phases: list = []
    progress = data.get("workflowProgress")
    for item in progress if isinstance(progress, list) else []:
        if isinstance(item, dict) and item.get("type") == "workflow_phase":
            index, title = item.get("index"), item.get("title")
            if isinstance(index, int) and isinstance(title, str) and title not in {t for _, t in phases}:
                phases.append((index, title))
    error_text = data.get("error")
    return RunJson(
        task_id=task_id,
        status=status,
        error=error_text if isinstance(error_text, str) else "",
        start_ms=_int(data.get("startTime")),
        duration_ms=_int(data.get("durationMs")),
        total_tokens=_int(data.get("totalTokens")),
        total_tool_calls=_int(data.get("totalToolCalls")),
        phases=tuple(phases),
    )


def map_status(run_status: str) -> Optional[str]:
    return _RUN_STATUS.get(run_status)


@dataclass(frozen=True)
class Notification:
    task_id: str
    tool_use_id: str
    status: str
    summary: str


def parse_notification(prompt: object) -> Optional[Notification]:
    if not isinstance(prompt, str) or not _NOTIFICATION.match(prompt):
        return None

    def tag(name: str) -> str:
        match = _TAG[name].search(prompt)
        return match.group(1) if match else ""

    task_id = tag("task-id")
    if not safe_name(task_id):
        drift("notification", "task-id")
        return None
    return Notification(task_id, tag("tool-use-id"), tag("status"), tag("summary"))


def transcript_end_ms(transcript: Path) -> Optional[int]:
    last = None
    for record in _records(transcript):
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                last = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                continue
    return last


def chain_span(
    name: str,
    span_id: str,
    trace_id: str,
    parent_span_id: str,
    start_ms: int,
    end_ms: int,
    attrs: dict,
    service_name: str,
    scope_name: str,
    error_message: str = "",
    failed: bool = False,
) -> dict:
    return build_span(
        name,
        "CHAIN",
        span_id,
        trace_id,
        parent_span_id,
        start_ms,
        max(end_ms, start_ms),
        {"openinference.span.kind": "CHAIN", **attrs},
        service_name,
        scope_name,
        status_code=2 if failed else 1,
        status_message=error_message if failed else "",
    )


def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stale_session_keys(state_dir: Path, now_s: float, current_key: str) -> list:
    """Other sessions' main state files idle for more than an hour and less than a week, newest
    first, that mention a workflow at all. The substring check is a cheap filter before the
    caller parses a file; most idle sessions never ran a workflow."""
    candidates = []
    try:
        paths = list(state_dir.glob("state_*.json"))
    except OSError:
        return []
    for path in paths:
        key = path.name[len("state_") : -len(".json")]
        if "__agent_" in key or key == current_key:
            continue
        try:
            changed = path.stat().st_mtime
        except OSError:
            continue
        if SWEEP_IDLE_S < now_s - changed <= SWEEP_MAX_AGE_S:
            candidates.append((changed, key, path))
    keys = []
    for _, key, path in sorted(candidates, reverse=True)[:SWEEP_MAX_CANDIDATES]:
        try:
            if WORKFLOW_KIND.encode() in path.read_bytes():
                keys.append(key)
        except OSError:
            continue
    return keys


def agents_recently_active(state_dir: Path, key: str, now_s: float) -> bool:
    try:
        paths = list(state_dir.glob(f"state_{key}__agent_*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            if now_s - path.stat().st_mtime <= SWEEP_IDLE_S:
                return True
        except OSError:
            continue
    return False
