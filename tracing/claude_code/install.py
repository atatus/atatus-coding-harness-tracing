"""Claude Code harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import json
import shlex
import sys

from core.setup import (
    configure_harness,
    dry_run,
    harness_dir,
    info,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    venv_bin,
)
from tracing.claude_code.constants import (
    ATATUS_ENV_KEYS,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_EVENTS,
    HOOK_TIMEOUT_SECONDS,
    SETTINGS_FILE,
)


def install(with_skills: bool = False) -> None:
    """Install Claude Code tracing: configure backend, register hooks, optionally symlink skills."""
    setup = configure_harness(
        HARNESS_NAME,
        display_name=DISPLAY_NAME,
        home_subdir=HARNESS_HOME,
        bin_name=HARNESS_BIN,
    )
    if setup is None:
        info("Aborted.")
        return

    _register_claude_hooks(setup.project_name)
    if with_skills:
        symlink_skills(HARNESS_NAME)
    info(f"Claude Code tracing installed ({SETTINGS_FILE})")


def uninstall() -> None:
    """Remove Claude Code tracing hooks, harness entry, and skill symlinks."""
    _unregister_claude_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Claude Code tracing uninstalled")


def _load_settings() -> dict:
    """Load SETTINGS_FILE as JSON, returning {} if missing or malformed."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(settings: dict) -> None:
    """Write settings dict as formatted JSON with trailing newline."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")


def _register_claude_hooks(project_name: str = HARNESS_NAME) -> None:
    """Read SETTINGS_FILE (or init to {}), add plugin reference + hook commands.

    Registering the local plugin (path → ~/.atatus/harness/tracing/claude_code)
    makes Claude Code auto-load its bundled hooks even in non-interactive
    (-p) mode, where ``--setting-sources`` defaults to ``project,local`` and
    user-level hooks would otherwise be skipped.

    Merges with existing entries without duplicating. Uses venv_bin() for each
    HOOK_EVENTS entry point. Honors dry_run().
    """
    settings = _load_settings()
    plugin_dir = str(harness_dir("claude-code"))

    # Add plugin reference (idempotent — skip if the path is already listed
    # under either the string or {path: ...} shape used by the marketplace).
    plugins = settings.setdefault("plugins", [])
    has_plugin = any(
        (isinstance(p, str) and p == plugin_dir) or (isinstance(p, dict) and p.get("path") == plugin_dir)
        for p in plugins
    )
    if not has_plugin:
        plugins.append({"type": "local", "path": plugin_dir})

    # The project name is assigned, not defaulted: a reconfigure exists to change
    # it, and this file exports the value into every session, so leaving a stale
    # one here would silently outrank the answer the user just gave.
    env_block = settings.setdefault("env", {})
    env_block["ATATUS_PROJECT_NAME"] = project_name
    env_block.setdefault("ATATUS_TRACE_ENABLED", "true")

    # Register hooks. Existing entries get the timeout stamped too, so an update
    # tightens installs that predate it instead of leaving them on the 60s default.
    hooks = settings.setdefault("hooks", {})
    for event, entry_point in HOOK_EVENTS.items():
        hook_path = venv_bin(entry_point)
        hook_cmd = shlex.quote(hook_path.as_posix())
        event_hooks = hooks.setdefault(event, [])

        # Older installers wrote native (unquoted) paths, which Git Bash on
        # Windows can misinterpret. Drop only this event's exact legacy command.
        legacy_cmd = str(hook_path)
        if legacy_cmd != hook_cmd:
            cleaned = []
            for entry in event_hooks:
                entry_hooks = entry.get("hooks", [])
                kept_hooks = [
                    hook
                    for hook in entry_hooks
                    if not (hook.get("type") == "command" and hook.get("command") == legacy_cmd)
                ]
                if len(kept_hooks) == len(entry_hooks):
                    cleaned.append(entry)
                elif kept_hooks:
                    cleaned.append({**entry, "hooks": kept_hooks})
            event_hooks[:] = cleaned

        ours = [h for entry in event_hooks for h in entry.get("hooks", []) if h.get("command", "") == hook_cmd]
        for h in ours:
            h["timeout"] = HOOK_TIMEOUT_SECONDS
        if not ours:
            event_hooks.append({"hooks": [{"type": "command", "command": hook_cmd, "timeout": HOOK_TIMEOUT_SECONDS}]})

    if dry_run():
        info(f"would write Claude hooks to {SETTINGS_FILE}")
        return

    _save_settings(settings)


def _unregister_claude_hooks() -> None:
    """Remove our hook entries and plugin reference from SETTINGS_FILE.

    Keeps other hooks, plugins, and env vars intact. No-op if file doesn't
    exist. Honors dry_run().
    """
    if not SETTINGS_FILE.exists():
        return

    settings = _load_settings()
    if not settings:
        return

    plugin_dir = str(harness_dir("claude-code"))

    # Remove our plugin entries (drops empty list).
    if "plugins" in settings:
        settings["plugins"] = [
            p
            for p in settings["plugins"]
            if not ((isinstance(p, str) and p == plugin_dir) or (isinstance(p, dict) and p.get("path") == plugin_dir))
        ]
        if not settings["plugins"]:
            del settings["plugins"]

    # Remove our hook entries (both legacy unquoted and quoted-posix commands)
    if "hooks" in settings:
        our_commands = {str(venv_bin(ep)) for ep in HOOK_EVENTS.values()}
        our_commands.update(shlex.quote(venv_bin(ep).as_posix()) for ep in HOOK_EVENTS.values())
        hooks = settings["hooks"]
        for event in list(hooks.keys()):
            event_hooks = hooks[event]
            filtered = []
            for entry in event_hooks:
                entry_hooks = entry.get("hooks", [])
                kept_hooks = [
                    hook
                    for hook in entry_hooks
                    if not (hook.get("type") == "command" and hook.get("command") in our_commands)
                ]
                if len(kept_hooks) == len(entry_hooks):
                    filtered.append(entry)
                elif kept_hooks:
                    filtered.append({**entry, "hooks": kept_hooks})
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]
        if not hooks:
            del settings["hooks"]

    # Remove our env keys so stale values don't linger post-uninstall.
    if "env" in settings and isinstance(settings["env"], dict):
        env_block = settings["env"]
        for key in ATATUS_ENV_KEYS:
            env_block.pop(key, None)
        if not env_block:
            del settings["env"]

    if dry_run():
        info(f"would remove Claude hooks from {SETTINGS_FILE}")
        return

    _save_settings(settings)


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
