"""Constants for the Cursor tracing harness."""

from pathlib import Path

HARNESS_NAME = "cursor"
DISPLAY_NAME = "Cursor"
HARNESS_HOME = ".cursor"  # ~/.cursor — presence check for soft install detection
HARNESS_BIN = "cursor"  # binary name for shutil.which() fallback

HOOKS_FILE = Path.home() / ".cursor" / "hooks.json"
HOOK_BIN_NAME = "atatus-hook-cursor"

# All routed to a single CLI entry point (the handler dispatches based on
# hook_event_name / hookEventName in the JSON payload).
# Includes IDE events plus CLI-specific events (sessionStart, sessionEnd).
#
# `beforeTabFileRead` and `afterTabFileEdit` are deliberately absent. They are
# Cursor Tab — inline autocomplete, which fires while the user types and is not
# agent activity. They also arrive with no conversation and no generation, so
# every one became a single-span trace of its own.
HOOK_EVENTS = (
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "stop",
    "sessionStart",
    "sessionEnd",
    "postToolUse",
)
