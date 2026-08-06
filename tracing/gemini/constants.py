"""Constants for the Gemini tracing harness installer."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "gemini"

# Gemini settings.json lives at ~/.gemini/settings.json (user) or
# .gemini/settings.json (project). We install user-level by default.
SETTINGS_DIR = Path.home() / ".gemini"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# The friendly hook name written into the inner hook block of settings.json.
# Used by both install() (to write) and uninstall() (to identify entries to remove).
HOOK_NAME = "atatus-tracing"

# Map of Gemini hook event name -> CLI entry-point script name.
# These entry-point names are registered in pyproject.toml [project.scripts]
# in the wire-entry-points task. Order is preserved when writing settings.json.
EVENTS: dict[str, str] = {
    "SessionStart": "atatus-hook-gemini-session-start",
    "SessionEnd": "atatus-hook-gemini-session-end",
    "BeforeAgent": "atatus-hook-gemini-before-agent",
    "AfterAgent": "atatus-hook-gemini-after-agent",
    "BeforeModel": "atatus-hook-gemini-before-model",
    "AfterModel": "atatus-hook-gemini-after-model",
    "BeforeTool": "atatus-hook-gemini-before-tool",
    "AfterTool": "atatus-hook-gemini-after-tool",
}

# Default per-hook timeout in milliseconds (Gemini's own default is 60000).
HOOK_TIMEOUT_MS = 30000
