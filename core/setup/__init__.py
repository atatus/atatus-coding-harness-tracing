#!/usr/bin/env python3
"""Shared setup utilities for all harness setup wizards."""

from __future__ import annotations

import os
import re
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

    if non_interactive():
        return _backend_from_env(target)

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


def _backend_from_env(target: str) -> tuple[str, dict]:
    """Resolve credentials from the environment, for non-interactive installs.

    There is only one backend, so nothing has to be inferred — the licence key is
    required and the endpoint falls back to the default collector. Exits with an
    actionable message when the key is missing; this path must never fall back to
    a prompt, because there is nobody to answer it.
    """
    api_key = _require_env("ATATUS_API_KEY", "An Atatus licence key")
    endpoint = _env("ATATUS_OTLP_ENDPOINT") or DEFAULT_OTLP_ENDPOINT

    info(f"Licence key: found (from {_source_of('ATATUS_API_KEY')})")
    info(f"OTLP endpoint: {endpoint} (from {_source_of('ATATUS_OTLP_ENDPOINT', fallback='default')})")

    return (target, {"endpoint": endpoint, "api_key": api_key})


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

    Non-interactively the same rule holds: the name comes from the dotenv file
    or the stored value, and there being neither is a hard error rather than an
    invented default.
    """
    if non_interactive():
        # Deliberately not _env(): the ambient ATATUS_PROJECT_NAME belongs to
        # whichever harness is already installed — `claude_code/install.py`
        # bakes it into settings.json and it is exported into every session.
        # Inheriting it would name *this* harness's project after a different
        # one and silently collide their spans.
        name = _dotenv_only("ATATUS_PROJECT_NAME") or default
        if not name:
            err(
                "A project name is required — it is how your spans are grouped in Atatus.\n"
                "        Set ATATUS_PROJECT_NAME in the file named by ATATUS_ENV_FILE."
            )
            sys.exit(1)
        # "default" rather than "harness default": on a re-install the caller
        # passes the stored project name as the default.
        info(f"Project name: {name} (from {_source_of('ATATUS_PROJECT_NAME', 'default', include_env=False)})")
        return name

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

    **Non-interactively every category starts off**, including the two that
    `LOG_FLAG_DEFAULTS` has on. That asymmetry is the point: a `[Y/n]` default is
    a human declining to change an answer they were shown, which is consent; the
    same default with nobody watching is capture of prompts and command output
    that no one agreed to. Reaching it needs no malice — `update` forces
    non-interactive mode whenever there is no terminal, so a cron or CI run
    against a config with no `logging:` block would switch capture on silently.
    Each category needs its `ATATUS_LOG_*` variable to say so explicitly.
    """
    if non_interactive():
        answers = {
            "prompts": env_flag("ATATUS_LOG_PROMPTS", default=False),
            "tool_details": env_flag("ATATUS_LOG_TOOL_DETAILS", default=False),
            "tool_content": env_flag("ATATUS_LOG_TOOL_CONTENT", default=False),
        }
        info("Content logging: " + ", ".join(f"{k}={'on' if v else 'off'}" for k, v in answers.items()))
        if not any(answers.values()):
            info("No ATATUS_LOG_* settings given, so no content is captured — span structure only.")
            info("Set ATATUS_LOG_PROMPTS, ATATUS_LOG_TOOL_DETAILS or ATATUS_LOG_TOOL_CONTENT to true to capture it.")
        deviations = {k: v for k, v in answers.items() if v != LOG_FLAG_DEFAULTS[k]}
        return {"_v": LOG_CONFIG_VERSION, **deviations}

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
    if non_interactive():
        return _env("ATATUS_USER_ID")

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


def non_interactive() -> bool:
    """True when ATATUS_NONINTERACTIVE is set to a truthy value ('1','true','yes').

    In this mode the setup wizards never call ``input()``/``getpass()``: every
    value resolves from the environment (or the dotenv file named by
    ``ATATUS_ENV_FILE``) and a missing required value is a hard error instead of
    a prompt. Deliberately opt-in — without it, an exported ``ATATUS_API_KEY``
    would silently stop the interactive wizard asking its questions, and every
    installed harness exports that variable into every agent session.
    """
    return os.environ.get("ATATUS_NONINTERACTIVE", "").lower() in ("1", "true", "yes")


# Keys we will read out of a dotenv file. Everything else in the file is
# ignored, so pointing at an app's .env cannot inject unrelated settings.
_DOTENV_KEYS = (
    "ATATUS_API_KEY",
    "ATATUS_OTLP_ENDPOINT",
    "ATATUS_PROJECT_NAME",
    "ATATUS_USER_ID",
    "ATATUS_LOG_PROMPTS",
    "ATATUS_LOG_TOOL_DETAILS",
    "ATATUS_LOG_TOOL_CONTENT",
    "ATATUS_KIRO_AGENT",
    "ATATUS_KIRO_SET_DEFAULT",
)

_dotenv_cache: Optional[dict] = None
_dotenv_path: Optional[Path] = None


def _dotenv_file() -> Optional[Path]:
    """The dotenv file to read, from ``ATATUS_ENV_FILE`` only. None when unset.

    Deliberately does **not** fall back to ``./.env`` or ``./.env.local``.
    Values from a dotenv file outrank the process environment (see ``_env``), and
    the working directory is whatever repository the user happens to be sitting
    in. An implicit search would let a cloned repo's dotenv supply the *routing*
    (``ATATUS_OTLP_ENDPOINT``) while the user's real licence key came from the
    ambient environment — writing a config that ships every later session's
    prompts, tool output and bearer key to an endpoint the repo chose.

    An explicit path that cannot be read is a hard error rather than a silent
    fall-back: naming a file states where the credentials are meant to come
    from, and a typo would otherwise quietly install with whatever happened to
    be exported instead.
    """
    explicit = os.environ.get("ATATUS_ENV_FILE", "").strip()
    if not explicit:
        return None
    path = Path(explicit).expanduser()
    if not path.is_file():
        err(f"ATATUS_ENV_FILE points at {path}, which is not a readable file.")
        sys.exit(1)
    return path


def _split_dotenv_value(raw: str) -> str:
    """Return the value part of a dotenv assignment, quotes and comment resolved.

    Raises ``ValueError`` on an unbalanced quote. That is the whole reason this
    is strict: ``ATATUS_API_KEY="abc`` parsed leniently yields a licence key with
    a quote welded on, which reports as "found" and then fails authentication
    with nothing pointing at the typo.
    """
    raw = raw.strip()
    if not raw:
        return ""

    quote = raw[0]
    if quote not in ("'", '"'):
        # Unquoted: a whitespace-preceded '#' starts a comment, per dotenv
        # convention. `KEY=a#b` keeps the '#'.
        cut = re.search(r"\s#", raw)
        return (raw[: cut.start()] if cut else raw).strip()

    body = raw[1:]
    if quote == "'":
        end = body.find("'")
        if end < 0:
            raise ValueError("unbalanced single quote")
        return body[:end]

    # Double-quoted: honour backslash escapes, and do not let an escaped quote
    # close the string.
    out: list[str] = []
    i = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(escapes.get(body[i + 1], "\\" + body[i + 1]))
            i += 2
            continue
        if ch == '"':
            return "".join(out)
        out.append(ch)
        i += 1
    raise ValueError("unbalanced double quote")


def _parse_dotenv(path: Path) -> dict:
    """Extract `_DOTENV_KEYS` from a dotenv file. Unreadable file → {}.

    Handles ``export KEY=value``, both quote styles, inline comments and blank
    lines. Values are not shell-expanded — a literal ``$FOO`` stays literal.

    Hand-rolled rather than ``python-dotenv`` on purpose. This package ships
    ``dependencies = []``; every hook invocation is a fresh short-lived process,
    and a runtime dependency would also have to be bundled for any future
    offline/wheel install to keep working. Only the nine keys above are read, so
    a file holding unrelated application settings is safe to point at.

    An unparseable line for a key we want is **fatal**, not skipped: skipping
    would fall through to the environment and report a value that did not come
    from the file the caller named.
    """
    try:
        text = path.read_text()
    except OSError:
        return {}

    values: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, raw = stripped.partition("=")
        key = key.strip()
        if not sep or key not in _DOTENV_KEYS:
            continue
        try:
            values[key] = _split_dotenv_value(raw).strip()
        except ValueError as exc:
            err(f"{path}: could not parse {key} — {exc}.")
            err("Fix the line rather than leaving it: the value would otherwise be taken from the environment.")
            sys.exit(1)

    return values


def _dotenv_values() -> dict:
    """Load Atatus values from the dotenv file, once per process.

    This is what keeps a licence key out of argv, shell history and a coding
    agent's transcript: the key goes into a file, and the installer reads the
    file. Values found here take precedence over the real environment — see
    ``_env``.
    """
    global _dotenv_cache, _dotenv_path
    if _dotenv_cache is not None:
        return _dotenv_cache

    _dotenv_cache = {}
    path = _dotenv_file()
    if path is not None:
        found = _parse_dotenv(path)
        if found:
            info(f"Reading configuration from {path} ({len(found)} value(s))")
            _dotenv_cache = found
            _dotenv_path = path

    return _dotenv_cache


def _reset_dotenv_cache() -> None:
    """Clear the dotenv cache. For tests, which vary the file and its contents."""
    global _dotenv_cache, _dotenv_path
    _dotenv_cache = None
    _dotenv_path = None


def _dotenv_only(name: str) -> str:
    """Resolve a value from the dotenv file alone, ignoring the environment."""
    return _dotenv_values().get(name, "").strip()


def _source_of(name: str, fallback: str = "unset", include_env: bool = True) -> str:
    """Name where a value was resolved from, for reporting back to the user.

    ``include_env=False`` for values that ignore the environment by design, so
    the label never credits a source that was not consulted.
    """
    if _dotenv_only(name):
        return str(_dotenv_path) if _dotenv_path else "dotenv file"
    if include_env and os.environ.get(name, "").strip():
        return f"${name}"
    return fallback


def _env(name: str) -> str:
    """Resolve a config value: dotenv file first, then the real environment.

    The file deliberately wins. A dotenv file is an explicit, inspectable
    statement of intent; ``ATATUS_*`` variables are frequently *inherited* rather
    than chosen — an installed harness bakes ``ATATUS_API_KEY`` and
    ``ATATUS_PROJECT_NAME`` into its settings file and exports them into every
    agent session. Reading the environment first would let those stale values
    beat credentials the caller had just written.
    """
    return _dotenv_only(name) or os.environ.get(name, "").strip()


def env_value(name: str) -> str:
    """Resolve a config value from the dotenv file or environment.

    Public entry point for harness installers that have a prompt of their own to
    resolve (currently only Kiro's agent name).
    """
    return _env(name)


def env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean setting from the dotenv file or environment.

    Only explicit falsey words turn a default-on setting off, and only explicit
    truthy words turn a default-off setting on.
    """
    raw = _env(name).lower()
    if not raw:
        return default
    if default:
        return raw not in ("0", "false", "no", "n", "off")
    return raw in ("1", "true", "yes", "y", "on")


def _require_env(name: str, what: str) -> str:
    """Resolve a required value, or exit with an actionable message."""
    value = _env(name)
    if not value:
        err(
            f"{what} is required for a non-interactive install — set {name}, or add it to the file\n"
            f"        named by ATATUS_ENV_FILE."
        )
        sys.exit(1)
    return value


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
