"""Claude Code's Workflow tool: launches, run files and the spans built from them.

Claude Code's workflow files are undocumented, so every read of them lives here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from core.common import error

ENV_SWITCH = "ATATUS_TRACE_WORKFLOWS"
WORKFLOW_KIND = "workflow"

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
