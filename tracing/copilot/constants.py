"""Constants for the Copilot tracing harness installer."""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_NAME = "copilot"

# Copilot rejects a hooks file that omits the schema version.
HOOK_CONFIG_VERSION = 1

# Copilot reads every *.json in the hooks directory, so owning a dedicated file
# means install and uninstall never rewrite one the user also edits.
HOOKS_FILE_NAME = "atatus-tracing.json"

# Where installs before the move to a user-level directory wrote their hooks:
# relative to the installer's working directory, so only the one project it
# happened to be run from was ever traced.
LEGACY_HOOKS_DIR = Path(".github/hooks")
LEGACY_HOOKS_FILE_NAME = "hooks.json"


def copilot_home() -> Path:
    """Copilot's configuration directory."""
    override = os.environ.get("COPILOT_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".copilot"


def hooks_dir() -> Path:
    """User-level hooks directory, read by both the VS Code extension and the CLI."""
    return copilot_home() / "hooks"


def hooks_file() -> Path:
    """Full path to the hooks file this installer owns."""
    return hooks_dir() / HOOKS_FILE_NAME


# Event names are the capitalised aliases rather than Copilot's native
# camelCase: both are accepted, and these are the spelling the editor
# documents, so a user comparing our file to the docs sees the same names.
HOOK_EVENTS: dict[str, str] = {
    "SessionStart": "atatus-hook-copilot-session-start",
    "UserPromptSubmit": "atatus-hook-copilot-user-prompt",
    "PreToolUse": "atatus-hook-copilot-pre-tool",
    "PostToolUse": "atatus-hook-copilot-post-tool",
    "PostToolUseFailure": "atatus-hook-copilot-post-tool-failure",
    "Stop": "atatus-hook-copilot-stop",
    "SubagentStop": "atatus-hook-copilot-subagent-stop",
    "SubagentStart": "atatus-hook-copilot-subagent-start",
    "PermissionRequest": "atatus-hook-copilot-permission-request",
    "SessionEnd": "atatus-hook-copilot-session-end",
}
