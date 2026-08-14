"""Constants for the Codex tracing harness."""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_NAME = "codex"
DISPLAY_NAME = "Codex CLI"
HARNESS_HOME = ".codex"  # ~/.codex — presence check for soft install detection
HARNESS_BIN = "codex"  # binary name for shutil.which() fallback

ENV_FILE_NAME = "atatus-env.sh"
NOTIFY_BIN_NAME = "atatus-hook-codex-notify"


def get_codex_home() -> Path:
    """Return the active Codex home, matching Codex's own ``CODEX_HOME`` rules.

    ``CODEX_HOME`` is expanded the way a shell would (``~`` and ``$VAR``) before
    use. Resolved on every call rather than at import time, so a hook process
    and an installer in the same session always agree with the CLI.

    An explicitly set home must resolve to an existing directory. That matters
    most for the installer: an invalid override has to fail loudly rather than
    silently wiring tracing into the default ``~/.codex`` profile the user is
    not running.
    """
    raw_home = os.path.expandvars(os.path.expanduser(os.environ.get("CODEX_HOME") or ""))
    if raw_home == "":
        return Path.home() / ".codex"

    try:
        home = Path(raw_home).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"CODEX_HOME points to an invalid path: {raw_home!r}") from exc

    if not home.is_dir():
        raise ValueError(f"CODEX_HOME points to a non-directory: {raw_home!r}")
    return home
