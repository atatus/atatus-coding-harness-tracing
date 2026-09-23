"""Constants for the Claude Code harness installer."""

from pathlib import Path

HARNESS_NAME = "claude-code"
DISPLAY_NAME = "Claude Code"
HARNESS_HOME = ".claude"  # ~/.claude — presence check for soft install detection
HARNESS_BIN = "claude"  # binary name for shutil.which() fallback

SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# event name → venv binary basename
HOOK_EVENTS = {
    "SessionStart": "atatus-hook-session-start",
    "UserPromptSubmit": "atatus-hook-user-prompt-submit",
    "PreToolUse": "atatus-hook-pre-tool-use",
    "PostToolUse": "atatus-hook-post-tool-use",
    "Stop": "atatus-hook-stop",
    "SubagentStop": "atatus-hook-subagent-stop",
    "StopFailure": "atatus-hook-stop-failure",
    "Notification": "atatus-hook-notification",
    "PermissionRequest": "atatus-hook-permission-request",
    "SessionEnd": "atatus-hook-session-end",
    "PostToolUseFailure": "atatus-hook-post-tool-use-failure",
    "SubagentStart": "atatus-hook-subagent-start",
    "UserPromptExpansion": "atatus-hook-user-prompt-expansion",
    "PreCompact": "atatus-hook-pre-compact",
    "PostCompact": "atatus-hook-post-compact",
    "PermissionDenied": "atatus-hook-permission-denied",
    "Elicitation": "atatus-hook-elicitation",
    "ElicitationResult": "atatus-hook-elicitation-result",
}

# Claude Code kills a hook that runs past this and carries on without it
HOOK_TIMEOUT_SECONDS = 10

# Env keys written into settings.json by the installer. Uninstall pops
# any of these present so stale values don't linger after teardown.
ATATUS_ENV_KEYS = (
    "ATATUS_TRACE_ENABLED",
    "ATATUS_PROJECT_NAME",
    "ATATUS_USER_ID",
    "ATATUS_API_KEY",
    "ATATUS_OTLP_ENDPOINT",
    "ATATUS_OTLP_ENDPOINT",
    "ATATUS_API_KEY",
    "ATATUS_DRY_RUN",
    "ATATUS_VERBOSE",
    "ATATUS_LOG_FILE",
)
