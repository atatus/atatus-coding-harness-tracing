"""Check Claude Code transcripts for records the trace parser cannot place.

Run it after every Claude Code update::

    python -m tracing.claude_code.transcript_check [PATH ...] [--limit N]

With no PATH it reads the newest transcripts under ``~/.claude/projects``. It exits 1
when a transcript has a model call without a uuid or a tool recorded twice: either one
means a newer transcript format is reaching the parser in a shape it does not read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from core.event_model import EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks.transcript import (
    is_conversation_record,
    is_unidentified_model_call,
    parse_claude_transcript,
)


@dataclass
class TranscriptReport:
    path: Path
    model_calls: int = 0
    tools: int = 0
    unidentified_model_calls: int = 0
    duplicate_tools: int = 0
    versions: set[str] = field(default_factory=set)
    skipped_record_types: Counter = field(default_factory=Counter)

    @property
    def drifted(self) -> bool:
        return bool(self.unidentified_model_calls or self.duplicate_tools)

    def line(self) -> str:
        skipped = ",".join(f"{name}:{count}" for name, count in sorted(self.skipped_record_types.items()))
        return (
            f"{'DRIFT' if self.drifted else 'OK':5}  {self.path}  "
            f"versions={','.join(sorted(self.versions)) or '-'} model_calls={self.model_calls} tools={self.tools} "
            f"unidentified={self.unidentified_model_calls} duplicates={self.duplicate_tools} skipped={skipped or '-'}"
        )


def check_transcript(path: Path) -> TranscriptReport:
    report = TranscriptReport(path)
    root = TurnEvent(
        event_id="turn:check",
        session_id="",
        turn_id="0",
        sequence=0,
        started_at_ms=None,
        ended_at_ms=None,
        status=EventStatus.COMPLETED,
    )
    for event in parse_claude_transcript(path, root).events:
        if isinstance(event, ModelCallEvent):
            report.model_calls += 1
            report.unidentified_model_calls += is_unidentified_model_call(event)
        elif isinstance(event, ToolEvent):
            if ":duplicate:" in event.event_id:
                report.duplicate_tools += 1
            else:
                report.tools += 1

    with path.open("rb") as handle:
        for raw in handle:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if isinstance(version, str) and version:
                report.versions.add(version)
            if isinstance(entry.get("message"), dict) and not is_conversation_record(entry):
                report.skipped_record_types[str(entry.get("type"))] += 1
    return report


def _default_root() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "projects"


def _transcripts(paths: list[str], limit: int) -> list[Path]:
    found: list[Path] = []
    for raw in paths or [str(_default_root())]:
        path = Path(raw).expanduser()
        if path.is_dir():
            found.extend(path.rglob("*.jsonl"))
        elif path.is_file():
            found.append(path)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracing.claude_code.transcript_check", description=__doc__)
    parser.add_argument("paths", nargs="*", help="transcript files or directories (default: ~/.claude/projects)")
    parser.add_argument("--limit", type=int, default=20, help="newest transcripts to check (default: 20)")
    args = parser.parse_args(argv)

    drifted = 0
    for path in _transcripts(args.paths, args.limit):
        try:
            report = check_transcript(path)
        except OSError as exc:
            print(f"SKIP   {path}  {exc}")
            continue
        drifted += report.drifted
        print(report.line())
    print(f"{drifted} transcript(s) drifted")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
