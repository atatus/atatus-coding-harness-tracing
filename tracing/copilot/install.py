#!/usr/bin/env python3
"""Copilot tracing harness installer.

Handles install and uninstall for GitHub Copilot tracing hooks. Writes a hooks
file into Copilot's user-level hooks directory, so every project is traced
rather than only the one the installer was run from:

    {"version": 1, "hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}

Usage (called by the shell router):
    python tracing/copilot/install.py install   [--project NAME]
    python tracing/copilot/install.py uninstall
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import core.setup as _setup
from core.setup import (
    configure_harness,
    dry_run,
    info,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    venv_bin,
)
from tracing.copilot.constants import (
    HARNESS_NAME,
    HOOK_CONFIG_VERSION,
    HOOK_EVENTS,
    LEGACY_HOOKS_DIR,
    LEGACY_HOOKS_FILE_NAME,
    hooks_dir,
    hooks_file,
)

#: Our commands all live in the venv under this prefix, which is what lets
#: uninstall and legacy cleanup tell our entries apart from the user's.
_HOOK_BIN_PREFIX = "atatus-hook-copilot-"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on missing or malformed files."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _is_ours(entry: object) -> bool:
    """True when a hook entry is one this installer wrote."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command") or entry.get("bash") or entry.get("exec") or ""
    return _HOOK_BIN_PREFIX in str(command)


# ---------------------------------------------------------------------------
# Hook file
# ---------------------------------------------------------------------------


def _install_hooks(filepath: Path) -> None:
    """Merge our hook entries into *filepath*."""
    if dry_run():
        info(f"would write hooks to {filepath}")
        return

    data = _read_json(filepath)
    data["version"] = HOOK_CONFIG_VERSION
    hooks_map = data.setdefault("hooks", {})
    if not isinstance(hooks_map, dict):
        hooks_map = {}
        data["hooks"] = hooks_map

    for event, entry_point in HOOK_EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list = hooks_map.setdefault(event, [])
        if not isinstance(event_list, list):
            event_list = []
            hooks_map[event] = event_list

        # Drop any previous entry of ours first: a moved venv changes the
        # absolute path, and matching on it alone would stack up duplicates.
        event_list[:] = [h for h in event_list if not _is_ours(h)]
        event_list.append({"type": "command", "command": cmd})

    _write_json(filepath, data)


def _uninstall_hooks(filepath: Path) -> None:
    """Remove our hook entries from *filepath*. Removes the file if nothing else remains."""
    if not filepath.is_file():
        return

    if dry_run():
        info(f"would remove hooks from {filepath}")
        return

    data = _read_json(filepath)
    hooks_map = data.get("hooks")
    if not isinstance(hooks_map, dict):
        return

    for event in list(hooks_map):
        entries = hooks_map[event]
        if not isinstance(entries, list):
            continue
        remaining = [h for h in entries if not _is_ours(h)]
        if remaining:
            hooks_map[event] = remaining
        else:
            hooks_map.pop(event, None)

    if hooks_map:
        data["hooks"] = hooks_map
        _write_json(filepath, data)
    else:
        try:
            filepath.unlink()
        except OSError:
            pass


def _legacy_hook_files() -> list[Path]:
    """Project-local hook files an earlier install may have written.

    The installer's working directory is the documented case; the install
    directory is the one that actually bites, because running the installer
    from the checkout registered hooks that only fired inside it.
    """
    candidates = [
        Path.cwd() / LEGACY_HOOKS_DIR / LEGACY_HOOKS_FILE_NAME,
        Path(_setup.INSTALL_DIR) / LEGACY_HOOKS_DIR / LEGACY_HOOKS_FILE_NAME,
    ]
    seen: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved not in [p.resolve() for p in seen] and path.is_file():
            seen.append(path)
    return seen


def _clean_legacy_hooks() -> None:
    """Strip our entries from any project-local hook file left by an older install.

    Left in place they double-fire alongside the user-level hooks, producing two
    spans for every event inside that one project.
    """
    for path in _legacy_hook_files():
        data = _read_json(path)
        hooks_map = data.get("hooks")
        if not isinstance(hooks_map, dict):
            continue
        if not any(_is_ours(h) for entries in hooks_map.values() if isinstance(entries, list) for h in entries):
            continue
        if dry_run():
            info(f"would remove superseded project-local hooks from {path}")
            continue
        _uninstall_hooks(path)
        info(f"Removed superseded project-local hooks from {path}")
        parent = path.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install Copilot tracing hooks (VS Code + CLI) and register in config.json."""
    # No presence check: the hooks directory is created on demand and Copilot
    # ships inside the editor, so there is no user-level file or binary that
    # reliably says it is installed. A check with nothing dependable to look at
    # would warn on every correct install, so this harness passes no signals.
    setup = configure_harness(HARNESS_NAME)
    if setup is None:
        info("Aborted.")
        return

    target = hooks_file()

    if not dry_run():
        hooks_dir().mkdir(parents=True, exist_ok=True)

    _install_hooks(target)
    _clean_legacy_hooks()

    if with_skills:
        symlink_skills(HARNESS_NAME)

    info(f"Copilot tracing installed ({target})")


def uninstall() -> None:
    """Remove Copilot tracing hooks and deregister from config.json."""
    _uninstall_hooks(hooks_file())
    _clean_legacy_hooks()

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
