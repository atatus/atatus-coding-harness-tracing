#!/usr/bin/env python3
"""Copilot tracing harness installer.

Handles install and uninstall for GitHub Copilot tracing hooks. Writes a
single .github/hooks/hooks.json in the format VS Code Copilot Chat expects:
    {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}

Usage (called by the shell router):
    python tracing/copilot/install.py install   [--project NAME]
    python tracing/copilot/install.py uninstall
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.setup import (
    configure_harness,
    dry_run,
    info,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    venv_bin,
)
from tracing.copilot.constants import HARNESS_NAME, HOOK_EVENTS, HOOKS_DIR, HOOKS_FILE

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on missing or malformed files."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Hook file (.github/hooks/hooks.json)
# ---------------------------------------------------------------------------
#
# VS Code Copilot Chat schema:
#   {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}
# Docs: https://code.visualstudio.com/docs/copilot/customization/hooks


def _install_hooks(hooks_dir: Path) -> None:
    """Merge our hook entries into hooks_dir/hooks.json."""
    filepath = hooks_dir / HOOKS_FILE.name

    if dry_run():
        info(f"would write hooks to {filepath}")
        return

    data = _read_json(filepath)
    hooks_map: dict = data.setdefault("hooks", {})

    for event, entry_point in HOOK_EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list: list = hooks_map.setdefault(event, [])

        already = any(h.get("command") == cmd and h.get("type") == "command" for h in event_list)
        if already:
            continue

        event_list.append({"type": "command", "command": cmd})

    _write_json(filepath, data)


def _uninstall_hooks(hooks_dir: Path) -> None:
    """Remove our hook entries from hooks.json. Removes the file if empty."""
    filepath = hooks_dir / HOOKS_FILE.name
    if not filepath.is_file():
        return

    if dry_run():
        info(f"would remove hooks from {filepath}")
        return

    data = _read_json(filepath)
    hooks_map = data.get("hooks", {})

    for event, entry_point in HOOK_EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list = hooks_map.get(event, [])
        filtered = [h for h in event_list if h.get("command") != cmd]
        if filtered:
            hooks_map[event] = filtered
        else:
            hooks_map.pop(event, None)

    if not hooks_map:
        filepath.unlink()
    else:
        data["hooks"] = hooks_map
        _write_json(filepath, data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install Copilot tracing hooks (VS Code + CLI) and register in config.json."""
    # No presence check: Copilot's hooks live in the *project's* .github/hooks,
    # and there is no user-level directory or binary that reliably says it is
    # installed. A check with nothing dependable to look at would warn on every
    # correct install, so this harness deliberately passes no signals.
    setup = configure_harness(HARNESS_NAME)
    if setup is None:
        info("Aborted.")
        return

    hooks_dir = Path.cwd() / HOOKS_DIR

    if not dry_run():
        hooks_dir.mkdir(parents=True, exist_ok=True)

    _install_hooks(hooks_dir)

    if with_skills:
        symlink_skills(HARNESS_NAME)

    info("Copilot tracing installed")


def uninstall() -> None:
    """Remove Copilot tracing hooks and deregister from config.json."""
    hooks_dir = Path.cwd() / HOOKS_DIR

    _uninstall_hooks(hooks_dir)

    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Copilot tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI entry point (called by the shell router)
# ---------------------------------------------------------------------------


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print(f"usage: {sys.argv[0]} {{install|uninstall}} [--with-skills]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    flags = set(sys.argv[2:])

    if action == "install":
        install(with_skills="--with-skills" in flags)
    else:
        uninstall()


if __name__ == "__main__":
    main()
