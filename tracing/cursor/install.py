"""Cursor harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import json
import sys

from core.setup import (
    INSTALL_DIR,
    configure_harness,
    dry_run,
    info,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    venv_bin,
)
from tracing.cursor.constants import (
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_BIN_NAME,
    HOOK_EVENTS,
    HOOKS_FILE,
)


def install(with_skills: bool = False) -> None:
    """Install Cursor tracing: configure backend, register hooks, optionally symlink skills."""
    setup = configure_harness(
        HARNESS_NAME,
        display_name=DISPLAY_NAME,
        home_subdir=HARNESS_HOME,
        bin_name=HARNESS_BIN,
    )
    if setup is None:
        info("Aborted.")
        return

    # Create cursor state dir
    state_dir = INSTALL_DIR / "state" / HARNESS_NAME
    if dry_run():
        info(f"would create {state_dir}")
    else:
        state_dir.mkdir(parents=True, exist_ok=True)

    _register_cursor_hooks()
    if with_skills:
        symlink_skills(HARNESS_NAME)
    info(f"Cursor tracing installed ({HOOKS_FILE})")


def uninstall() -> None:
    """Remove Cursor tracing hooks, harness entry, and skill symlinks."""
    _unregister_cursor_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Cursor tracing uninstalled")


def _load_hooks() -> dict:
    """Load HOOKS_FILE as JSON, returning a fresh skeleton if missing or malformed."""
    if not HOOKS_FILE.exists():
        return {"version": 1, "hooks": {}}
    try:
        data = json.loads(HOOKS_FILE.read_text())
        if not isinstance(data, dict):
            return {"version": 1, "hooks": {}}
        data.setdefault("version", 1)
        data.setdefault("hooks", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "hooks": {}}


def _save_hooks(data: dict) -> None:
    """Write hooks dict as formatted JSON with trailing newline."""
    HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _register_cursor_hooks() -> None:
    """Add hook entries for all HOOK_EVENTS to ~/.cursor/hooks.json.

    For each event, ensure an entry with ``command == venv_bin(HOOK_BIN_NAME)``
    exists — skip if already there.  Merges with existing entries without
    duplicating.  Honors dry_run().
    """
    data = _load_hooks()
    hooks = data["hooks"]
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    for event in HOOK_EVENTS:
        event_list = hooks.setdefault(event, [])
        already = any(h.get("command") == hook_cmd for h in event_list)
        if not already:
            event_list.append({"command": hook_cmd})

    if dry_run():
        info(f"would write Cursor hooks to {HOOKS_FILE}")
        return

    _save_hooks(data)


def _unregister_cursor_hooks() -> None:
    """Remove our hook entries from ~/.cursor/hooks.json.

    Keeps other hooks intact.  Removes event keys that become empty after
    filtering.  No-op if file doesn't exist.  Honors dry_run().
    """
    if not HOOKS_FILE.exists():
        return

    data = _load_hooks()
    hooks = data.get("hooks", {})
    if not hooks:
        return

    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    for event in list(hooks.keys()):
        event_list = hooks[event]
        filtered = [h for h in event_list if h.get("command") != hook_cmd]
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if dry_run():
        info(f"would remove Cursor hooks from {HOOKS_FILE}")
        return

    _save_hooks(data)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    flags = set(sys.argv[2:])
    if cmd == "install":
        install(with_skills="--with-skills" in flags)
    elif cmd == "uninstall":
        uninstall()
    else:
        print("usage: install.py {install|uninstall} [--with-skills]", file=sys.stderr)
        sys.exit(2)
