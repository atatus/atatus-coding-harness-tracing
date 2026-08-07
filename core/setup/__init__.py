#!/usr/bin/env python3
"""Shared setup utilities for all harness setup wizards."""

from __future__ import annotations

import os
import shutil
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

from core.common import DEFAULT_OTLP_ENDPOINT, LOG_CONFIG_VERSION, LOG_FLAG_DEFAULTS
from core.config import delete_value, load_config, save_config, set_value

# ---------------------------------------------------------------------------
# Shared path constants
# ---------------------------------------------------------------------------

INSTALL_DIR = Path.home() / ".atatus" / "harness"
VENV_DIR = INSTALL_DIR / "venv"
CONFIG_FILE = INSTALL_DIR / "config.json"
BIN_DIR = INSTALL_DIR / "bin"
RUN_DIR = INSTALL_DIR / "run"
LOG_DIR = INSTALL_DIR / "logs"
STATE_DIR = INSTALL_DIR / "state"

# Legacy collector artefacts to clean up
_LEGACY_ARTEFACTS = ("bin/atatus-collector", "run/collector.pid", "logs/collector.log")


# ---------------------------------------------------------------------------
# Output helpers (unchanged)
# ---------------------------------------------------------------------------


def print_color(msg: str, color: str = "") -> None:
    """Print with ANSI color. No-op on Windows if terminal doesn't support it."""
    codes = {
        "green": "\033[0;32m",
        "yellow": "\033[1;33m",
        "blue": "\033[0;34m",
        "red": "\033[0;31m",
    }
    nc = "\033[0m"

    use_color = color in codes and sys.stdout.isatty() and os.name != "nt"
    if use_color:
        print(f"{codes[color]}{msg}{nc}")
    else:
        print(msg)


def info(msg: str) -> None:
    """Print an info message with [atatus] prefix."""
    if sys.stdout.isatty() and os.name != "nt":
        print(f"\033[0;32m[atatus]\033[0m {msg}")
    else:
        print(f"[atatus] {msg}")


def err(msg: str) -> None:
    """Print an error message with [atatus] prefix to stderr."""
    if sys.stderr.isatty() and os.name != "nt":
        sys.stderr.write(f"\033[0;31m[atatus]\033[0m {msg}\n")
    else:
        sys.stderr.write(f"[atatus] {msg}\n")


# ---------------------------------------------------------------------------
# Harness presence check (soft signal)
# ---------------------------------------------------------------------------


def is_harness_installed(
    home_subdir: Optional[str] = None,
    bin_name: Optional[str] = None,
) -> bool:
    """True if ``~/<home_subdir>`` exists OR ``<bin_name>`` is on PATH.

    ``Path.home()`` is resolved at call time so tests can monkeypatch it.
    """
    if home_subdir and (Path.home() / home_subdir).exists():
        return True
    if bin_name and shutil.which(bin_name):
        return True
    return False


def ensure_harness_installed(
    display_name: str,
    home_subdir: Optional[str] = None,
    bin_name: Optional[str] = None,
) -> bool:
    """Soft check that the harness appears installed on this machine.

    If yes, return ``True`` silently.  If no, warn and either prompt the user
    (interactive) or proceed with a note (non-interactive).  Return ``True`` to
    proceed with install, ``False`` to abort.
    """
    if is_harness_installed(home_subdir=home_subdir, bin_name=bin_name):
        return True

    print_color(f"warning: {display_name} does not appear to be installed", "yellow")
    checks = []
    if home_subdir:
        checks.append(str(Path.home() / home_subdir))
    if bin_name:
        checks.append(f"'{bin_name}' on PATH")
    if checks:
        info(f"  (not found: {', '.join(checks)})")

    if not sys.stdout.isatty():
        info("  non-interactive — proceeding anyway")
        return True

    try:
        reply = input(f"Install tracing for {display_name} anyway? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes")


# ---------------------------------------------------------------------------
# Interactive prompts (unchanged)
# ---------------------------------------------------------------------------


def prompt_backend(
    existing_harnesses: dict | None = None,
) -> tuple[str, dict]:
    """Interactive credential setup with optional copy-from.

    existing_harnesses is the value of cfg['harnesses'] (or None).  If any
    entry already holds usable Atatus credentials, offer a menu to copy from
    one instead of retyping the license key.

    Returns (target, credentials).  credentials keys:
      {"endpoint", "api_key"}
    """
    target = "atatus"

    # --- copy-from logic ---
    copied = _try_copy_from(target, existing_harnesses)
    if copied is not None:
        return (target, copied)

    # --- fresh credential prompts ---
    print("")
    api_key = getpass("Atatus License Key: ").strip()

    if not api_key:
        err("A license key is required.")
        sys.exit(1)

    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[1;33mOTLP Endpoint\033[0m (leave blank for the default collector):")
    else:
        print("OTLP Endpoint (leave blank for the default collector):")
    otlp_endpoint = input(f"OTLP Endpoint [{DEFAULT_OTLP_ENDPOINT}]: ").strip()
    if not otlp_endpoint:
        otlp_endpoint = DEFAULT_OTLP_ENDPOINT

    return (
        target,
        {
            "endpoint": otlp_endpoint,
            "api_key": api_key,
        },
    )


def _try_copy_from(target: str, existing_harnesses: dict | None) -> dict | None:
    """Show copy-from menu if matching harnesses exist.  Returns credentials or None."""
    if not existing_harnesses:
        return None

    _required = {"endpoint", "api_key"}

    def _valid(entry: dict) -> bool:
        return all(k in entry and entry[k] for k in _required)

    matches: list[tuple[str, dict]] = []
    for name, entry in existing_harnesses.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("target") != target:
            continue
        if not _valid(entry):
            continue
        matches.append((name, entry))

    if not matches:
        return None

    # Display menu
    print("")
    print("Found existing harnesses already configured for Atatus:")
    for i, (name, entry) in enumerate(matches, 1):
        detail = f"endpoint: {entry.get('endpoint', '')}"
        print(f"  {i}) {name}  ({detail})")
    last = len(matches) + 1
    print(f"  {last}) Enter new credentials")
    print("")

    attempts = 0
    while attempts < 2:
        raw = input(f"Copy from [1-{last}]: ").strip()
        try:
            idx = int(raw)
            if idx == last:
                return None  # fall through to fresh prompts
            if 1 <= idx <= len(matches):
                name, entry = matches[idx - 1]
                info(f"Reusing {target} credentials from '{name}'.")
                return {"endpoint": entry["endpoint"], "api_key": entry["api_key"]}
        except (ValueError, TypeError):
            pass
        attempts += 1
        if attempts < 2:
            print("Invalid input, please try again.")

    # Two invalid attempts — default to new credentials
    return None


def prompt_project_name(default: str = "") -> str:
    """Prompt for the Atatus project name.

    **The user-supplied name IS the grouping key.** The receiver
    auto-creates one project per ``service.name``, so a silent default means
    every install of a given harness collapses into a single shared project --
    every engineer's sessions in one bucket, which is not recoverable after the
    fact.

    So a fresh install has **no default** and an empty answer is rejected. A
    re-install passes the name already chosen, which may be accepted with a
    blank line; that is a confirmation, not a silent default.
    """
    print("")
    if default:
        name = input(f"Project name [{default}]: ").strip()
        return name or default

    for _ in range(3):
        name = input("Project name (required): ").strip()
        if name:
            return name
        err("A project name is required — it is how your spans are grouped in Atatus.")
    err("No project name given after 3 attempts. Run setup again.")
    sys.exit(1)


def _prompt_bool(question: str, default: bool) -> bool:
    """Ask a yes/no question whose hint and blank-line answer follow `default`.

    A blank line means "accept the default", so the parse has to be asymmetric:
    with a True default only an explicit no flips it, and with a False default
    only an explicit yes does. Getting this backwards is how the tool-content
    default was silently defeated before — see the note in
    `prompt_content_logging`.
    """
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {question} {hint}: ").strip().lower()
    if default:
        return answer not in ("n", "no")
    return answer in ("y", "yes")


def prompt_content_logging() -> dict:
    """Prompt for content logging settings. Returns the dict to write under `logging:`.

    Defaults come from `LOG_FLAG_DEFAULTS`: prompts and tool *details* on,
    tool *output* off.

    **Only answers that deviate from those defaults are returned**, and
    `write_logging_config` replaces the whole `logging:` block, so accepting a
    default removes any previously-stored override. That is what makes a
    re-install repair an existing config.json rather than preserve it.

    Both halves matter. An earlier version wrote every answer explicitly and
    prompted `[Y/n]` for tool content, so pressing Enter stored
    `"tool_content": true`, which outranks the code default in
    `_resolve_log_flag` — the privacy default was never in effect on an
    installed machine. Keep the hint tied to the default, and keep defaulted
    answers out of the file.
    """
    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[1;33mSecurity:\033[0m Traces can contain sensitive data — credentials, PII, file contents.")
    else:
        print("Security: Traces can contain sensitive data — credentials, PII, file contents.")
    print("Prompts and tool details are captured by default; tool output is not.")
    print("Press Enter to accept each default.")
    print("")

    answers = {
        "prompts": _prompt_bool("Log user prompts?", LOG_FLAG_DEFAULTS["prompts"]),
        "tool_details": _prompt_bool(
            "Log what tools were asked to do (commands, file paths, URLs)?",
            LOG_FLAG_DEFAULTS["tool_details"],
        ),
        "tool_content": _prompt_bool(
            "Log what tools returned (file contents, command output)?",
            LOG_FLAG_DEFAULTS["tool_content"],
        ),
    }

    deviations = {k: v for k, v in answers.items() if v != LOG_FLAG_DEFAULTS[k]}
    return {"_v": LOG_CONFIG_VERSION, **deviations}


def needs_content_logging_prompt(config: Optional[dict]) -> bool:
    """Whether the content-logging wizard should run.

    True when no `logging:` block exists (fresh install) **or** the stored block
    predates `LOG_CONFIG_VERSION`. The second case is the repair path: the
    installers deliberately skip this wizard once a block exists, so without a
    version check a machine carrying a v1 block keeps its bad `tool_content`
    override through every future re-install.

    Side effect by design: prints a one-line notice when re-prompting over a
    stale block, so the user understands why a question they already answered is
    coming back. Keeping it here is what lets all eight installers share one
    identical call site.
    """
    logging_block = (config or {}).get("logging")
    if not isinstance(logging_block, dict):
        return True
    if logging_block.get("_v") == LOG_CONFIG_VERSION:
        return False
    info("Content-logging settings need re-confirming — the defaults changed (tool output is now off unless you opt in).")
    return True


def write_logging_config(logging_block: dict, config_path: str | None = None) -> None:
    """Write the top-level `logging:` key in config.json.

    **Replaces** the block rather than merging into it — `set_value` assigns.
    That is load-bearing: `prompt_content_logging` returns only deviations from
    `LOG_FLAG_DEFAULTS`, so replacement is what clears a stale override when a
    user re-runs setup and accepts the default. A merge here would make those
    overrides permanent.
    """
    config = load_config(config_path)
    if not config:
        config = {}
    set_value(config, "logging", logging_block)
    if dry_run():
        info("would write logging block to config.json")
        return
    save_config(config, config_path)


def prompt_user_id() -> str:
    """Optional user ID prompt. Returns "" if skipped."""
    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[0;34mOptional:\033[0m Set a user ID to identify your spans (useful for teams).")
    else:
        print("Optional: Set a user ID to identify your spans (useful for teams).")
    user_id = input("User ID (leave blank to skip): ").strip()
    return user_id


def write_config(
    target: str,
    credentials: dict,
    harness_name: str,
    project_name: str,
    user_id: str = "",
    collector: dict | None = None,
    config_path: Optional[str] = None,
) -> None:
    """Write or merge config.json with a fully-flattened harnesses.<name> entry.

    Writes harnesses.<harness_name>.{project_name, target, endpoint, api_key,
    [collector]}.  If user_id is non-empty, sets top-level user_id.
    Read-merge-write: preserves other harnesses and top-level keys.
    """
    config = load_config(config_path)

    if not config:
        config = {"harnesses": {}}

    # Strip legacy top-level keys if they leaked in from a prior save
    config.pop("backend", None)
    config.pop("collector", None)

    # Build the harness entry
    entry: dict = {
        "project_name": project_name,
        "target": target,
        "endpoint": credentials.get("endpoint", ""),
        "api_key": credentials.get("api_key", ""),
    }

    if collector is not None:
        entry["collector"] = collector

    set_value(config, f"harnesses.{harness_name}", entry)

    if user_id:
        set_value(config, "user_id", user_id)

    save_config(config, config_path)


# ---------------------------------------------------------------------------
# New shared helpers
# ---------------------------------------------------------------------------


def dry_run() -> bool:
    """True when ATATUS_DRY_RUN env var is set to a truthy value ('1','true','yes')."""
    return os.environ.get("ATATUS_DRY_RUN", "").lower() in ("1", "true", "yes")


def ensure_shared_runtime() -> None:
    """Create ~/.atatus/harness/{bin,run,logs,state} if missing. Idempotent.

    Also removes any legacy collector artefacts (bin/atatus-collector,
    run/collector.pid, logs/collector.log) left over from pre-buffer-service
    installs.
    """
    install_dir = INSTALL_DIR
    subdirs = [BIN_DIR, RUN_DIR, LOG_DIR, STATE_DIR]

    for d in subdirs:
        if not d.exists():
            if dry_run():
                info(f"would create {d}")
            else:
                d.mkdir(parents=True, exist_ok=True)

    # Remove legacy collector artefacts
    for rel in _LEGACY_ARTEFACTS:
        legacy = install_dir / rel
        if legacy.exists():
            if dry_run():
                info(f"would remove legacy artefact {legacy}")
            else:
                legacy.unlink()


def venv_bin(name: str) -> Path:
    """Return the full path to a venv binary.

    On POSIX: VENV_DIR/bin/<name>. On Windows: VENV_DIR/Scripts/<name>.exe.
    Does NOT verify the file exists.
    """
    if os.name == "nt":
        return VENV_DIR / "Scripts" / f"{name}.exe"
    return VENV_DIR / "bin" / name


def merge_harness_entry(
    name: str,
    project_name: str,
    target: str | None = None,
    credentials: dict | None = None,
    collector: dict | None = None,
) -> None:
    """Read config.json, add/update harnesses.<name>, write back with 0o600.

    If target + credentials are provided, writes the full entry.
    If only project_name, updates only that field (leaves other fields alone).
    If the file doesn't exist, creates it with just this entry under
    harnesses:.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        config = {"harnesses": {}}

    if target is not None and credentials is not None:
        entry: dict = {
            "project_name": project_name,
            "target": target,
            "endpoint": credentials.get("endpoint", ""),
            "api_key": credentials.get("api_key", ""),
        }
        if collector is not None:
            entry["collector"] = collector
        set_value(config, f"harnesses.{name}", entry)
    else:
        set_value(config, f"harnesses.{name}.project_name", project_name)
        if collector is not None:
            set_value(config, f"harnesses.{name}.collector", collector)

    if dry_run():
        info(f"would write harness entry '{name}' to {config_path}")
        return

    save_config(config, config_path)


def remove_harness_entry(name: str) -> None:
    """Read config.json, remove harnesses.<name> if present, write back.

    No-op if the file doesn't exist or the key isn't present.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        return

    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict) or name not in harnesses:
        return

    if dry_run():
        info(f"would remove harness entry '{name}' from {config_path}")
        return

    delete_value(config, f"harnesses.{name}")
    save_config(config, config_path)


def list_installed_harnesses() -> list[str]:
    """Return the list of keys under harnesses.* in config.json.

    Returns empty list if config is missing.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        return []

    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict):
        return []

    return list(harnesses.keys())


def harness_dir(harness: str) -> Path:
    """Return the absolute path of <install-dir>/tracing/<harness>/.

    Maps a harness alias (e.g. ``claude-code``) to its directory name
    (``claude_code``) under ``~/.atatus/harness/tracing/``.
    """
    sub_name = harness.replace("-", "_")
    return INSTALL_DIR / "tracing" / sub_name


def symlink_skills(harness: str, target_dir: Path | None = None) -> None:
    """Symlink <install-dir>/tracing/<harness>/skills/* into target_dir/.agents/skills/.

    target_dir defaults to the current working directory. Idempotent (skip
    existing links pointing at the right target). Does nothing if the harness
    has no skills/ directory.
    """
    hdir = harness_dir(harness)
    skills_src = hdir / "skills"

    if not skills_src.is_dir():
        return

    if target_dir is None:
        target_dir = Path.cwd()

    dest = target_dir / ".agents" / "skills"

    if dry_run():
        for item in skills_src.iterdir():
            info(f"would symlink {dest / item.name} -> {item}")
        return

    dest.mkdir(parents=True, exist_ok=True)

    for item in skills_src.iterdir():
        link = dest / item.name
        if link.is_symlink():
            if link.resolve() == item.resolve():
                continue  # already correct
            link.unlink()
        elif link.exists():
            continue  # regular file — don't overwrite
        link.symlink_to(item)


def unlink_skills(harness: str, target_dir: Path | None = None) -> None:
    """Remove symlinks created by symlink_skills() for <harness>.

    Only removes symlinks, never regular files. Idempotent.
    """
    hdir = harness_dir(harness)
    skills_src = hdir / "skills"

    if not skills_src.is_dir():
        return

    if target_dir is None:
        target_dir = Path.cwd()

    dest = target_dir / ".agents" / "skills"

    if not dest.is_dir():
        return

    for item in skills_src.iterdir():
        link = dest / item.name
        if link.is_symlink():
            if dry_run():
                info(f"would unlink {link}")
            else:
                link.unlink()
