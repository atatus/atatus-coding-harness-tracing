#!/usr/bin/env python3
"""Shared library for coding-harness-tracing: state management, file locking, and span building.

Provides FileLock (cross-platform file locking), StateManager (per-session
key-value state backed by JSON files), and OTLP span building functions.
Replaces the jq-based state functions in common.sh lines 46-109 and
build_span/build_multi_span from common.sh lines 277-317 / codex common.sh lines 110-145.
"""

import atexit
import functools
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Optional

# ---------------------------------------------------------------------------
# Content-capture defaults (ADR-011)
# ---------------------------------------------------------------------------

# Prompts and tool *details* are captured; tool *output* is not. Tool output is
# the broadest surface — file bodies, shell stdout, anything pasted into a
# session — so it is opt-in.
#
# Single source of truth. The setup wizard imports this to decide each prompt's
# default AND to decide which answers are worth persisting: it writes only the
# keys that deviate from these values, so a future change here reaches existing
# installs instead of being pinned by a stale config.json. Do not inline these
# booleans anywhere else.
LOG_FLAG_DEFAULTS = {
    "prompts": True,
    "tool_details": True,
    "tool_content": False,
}

# Stamped into the stored `logging:` block as `_v`. Bump when the meaning of a
# stored block changes; a block without a matching `_v` is re-prompted once.
#
# v2 (2026-08-07): v1 blocks were written by a wizard that prompted `[Y/n]` for
# tool content and persisted every answer, so accepting the defaults stored
# `"tool_content": true` — which outranks LOG_FLAG_DEFAULTS. Installers skip the
# wizard when a block exists, so v1 machines can never be repaired by
# re-installing. The version bump is what forces the one re-prompt that fixes
# them. Do not remove it without a replacement migration.
LOG_CONFIG_VERSION = 2

# ---------------------------------------------------------------------------
# Environment helper — reads tracing-related env vars with defaults
# ---------------------------------------------------------------------------


class _Env:
    """Lazy accessor for tracing-related environment variables.

    Property reads are live (not cached) so tests can monkeypatch os.environ.
    """

    @property
    def trace_enabled(self) -> bool:
        return os.environ.get("ATATUS_TRACE_ENABLED", "true").lower() == "true"

    @property
    def project_name(self) -> str:
        return os.environ.get("ATATUS_PROJECT_NAME", "")

    def get_user_id(self, service_name: str = "") -> str:
        """Resolve user id, checking highest precedence first and returning on
        the first hit:

        1. ``ATATUS_USER_ID`` env var (env always wins; an explicit empty value
           blanks it)
        2. ``harnesses.<service_name>.user_id`` in config.json (per-harness)
        3. top-level ``user_id`` in config.json (global)
        → ``""`` if none set
        """
        raw = os.environ.get("ATATUS_USER_ID")
        if raw is not None:
            return raw
        cfg = self._top_level_config
        if service_name:
            harnesses = cfg.get("harnesses")
            if isinstance(harnesses, dict):
                entry = harnesses.get(service_name)
                if isinstance(entry, dict) and entry.get("user_id"):
                    return str(entry["user_id"])
        val = cfg.get("user_id")
        return str(val) if val else ""

    @property
    def user_id(self) -> str:
        return self.get_user_id()

    @property
    def verbose(self) -> bool:
        return os.environ.get("ATATUS_VERBOSE", "").lower() == "true"

    @property
    def dry_run(self) -> bool:
        return os.environ.get("ATATUS_DRY_RUN", "false").lower() == "true"

    @property
    def log_file(self) -> str:
        # Each harness adapter sets ATATUS_LOG_FILE to its per-harness path under
        # ~/.atatus/harness/logs/ at import time; the env var also acts as the
        # explicit user override. Fall back to the shared kit log only when no
        # adapter has run (e.g. ad-hoc imports of core.common).
        override = os.environ.get("ATATUS_LOG_FILE")
        if override:
            return override
        from core.constants import LOG_DIR

        return str(LOG_DIR / "agent-kit.log")

    @property
    def otlp_endpoint(self) -> str:
        """OTLP collector endpoint override.

        Env > config.json > DEFAULT_OTLP_ENDPOINT. Set this only if you have been
        given a collector URL other than the default.
        """
        return os.environ.get("ATATUS_OTLP_ENDPOINT", "")

    @property
    def api_key(self) -> str:
        """Atatus license key. Sent as the ``api-key`` header on every export."""
        return os.environ.get("ATATUS_API_KEY", "")

    @functools.cached_property
    def _top_level_config(self) -> dict:
        """Full top-level config.json mapping. Loaded once per process."""
        try:
            from core.config import load_config

            data = load_config()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @functools.cached_property
    def _logging_config(self) -> dict:
        """Top-level `logging` block from config.json. Loaded once per process."""
        try:
            from core.config import load_config

            block = load_config().get("logging")
            return block if isinstance(block, dict) else {}
        except Exception:
            return {}

    # cached_property attributes that read config.json once per process. Kept in
    # one place so invalidate_caches() stays correct if a new cached read is added.
    _CACHED_CONFIG_PROPERTIES = ("_top_level_config", "_logging_config")

    def invalidate_caches(self) -> None:
        """Drop cached config reads so the next access reloads from disk.

        ``functools.cached_property`` stores its result in the instance
        ``__dict__``; popping the key forces recomputation. Used by tests (and
        any code that mutates config.json at runtime) to avoid serving stale
        config. Safe to call when nothing is cached.
        """
        for name in self._CACHED_CONFIG_PROPERTIES:
            self.__dict__.pop(name, None)

    def _resolve_log_flag(self, env_key: str, config_key: str, default: bool) -> bool:
        """env var > config.json `logging.<key>` > default."""
        raw = os.environ.get(env_key)
        if raw is not None:
            return raw.lower() == "true"
        val = self._logging_config.get(config_key)
        if isinstance(val, bool):
            return val
        return default

    @staticmethod
    def _parse_otel_resource_attributes() -> dict:
        """Parse OTEL_RESOURCE_ATTRIBUTES ("k1=v1,k2=v2") into a dict of str->str.

        Splits on ',', then on the first '=' per token. Trims whitespace from
        key and value. Tokens with no '=' or an empty key are skipped silently
        (fail-soft). Returns {} when the env var is unset or empty.
        """
        raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        result: dict = {}
        if not raw:
            return result
        for token in raw.split(","):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            result[key] = value
        return result

    def custom_attributes(self, service_name: str = "") -> dict:
        """Resolve custom span attributes for a harness.

        Layered, later wins:
          1. top-level ``attributes:`` in config.json (global — all harnesses)
          2. ``harnesses.<service_name>.attributes:`` in config.json (per-harness)
          3. ``OTEL_RESOURCE_ATTRIBUTES`` env var (env always wins)

        Config values keep their JSON type (an int stays an int, bool stays
        bool); env values are always strings. Returns a fresh dict every call.
        """
        cfg = self._top_level_config
        merged: dict = {}

        glob = cfg.get("attributes")
        if isinstance(glob, dict):
            merged.update(glob)

        if service_name:
            harnesses = cfg.get("harnesses")
            if isinstance(harnesses, dict):
                entry = harnesses.get(service_name)
                if isinstance(entry, dict):
                    per = entry.get("attributes")
                    if isinstance(per, dict):
                        merged.update(per)

        merged.update(self._parse_otel_resource_attributes())

        return merged

    @property
    def log_prompts(self) -> bool:
        return self._resolve_log_flag("ATATUS_LOG_PROMPTS", "prompts", LOG_FLAG_DEFAULTS["prompts"])

    @property
    def log_tool_details(self) -> bool:
        return self._resolve_log_flag(
            "ATATUS_LOG_TOOL_DETAILS", "tool_details", LOG_FLAG_DEFAULTS["tool_details"]
        )

    @property
    def log_tool_content(self) -> bool:
        return self._resolve_log_flag(
            "ATATUS_LOG_TOOL_CONTENT", "tool_content", LOG_FLAG_DEFAULTS["tool_content"]
        )


env = _Env()


def redact_content(allowed: bool, content: str) -> str:
    """Return content if logging is enabled, else a length-only placeholder.

    Used to keep size/debugging signal while stripping sensitive payloads
    (prompts, tool arguments, tool output) from exported spans.
    """
    text = content or ""
    if allowed:
        return text
    return f"<redacted ({len(text)} chars)>"


# ---------------------------------------------------------------------------
# ID and timestamp generation
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Generate a 32-hex-char trace ID (replaces uuidgen | tr -d '-')."""
    return os.urandom(16).hex()


def generate_span_id() -> str:
    """Generate a 16-hex-char span ID (replaces uuidgen | tr -d '-' | cut -c1-16)."""
    return os.urandom(8).hex()


def get_timestamp_ms() -> int:
    """Current time in milliseconds since epoch (replaces date +%s%3N)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _is_verbose() -> bool:
    return os.environ.get("ATATUS_VERBOSE", "").lower() == "true"


def log(msg: str) -> None:
    """Verbose log — only written when ATATUS_VERBOSE=true. Goes to stderr."""
    if _is_verbose():
        print(f"[atatus] {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    """Error log — always written. Goes to stderr."""
    print(f"[atatus:error] {msg}", file=sys.stderr, flush=True)


# Module-level handles for the active stderr redirect. Kept so tests (and any
# future caller) can restore the original stderr via restore_stderr_from_log_file().
_original_stderr: Optional[IO] = None
_redirected_log_fh: Optional[IO] = None


def redirect_stderr_to_log_file() -> None:
    """Tee ``sys.stderr`` to ``ATATUS_LOG_FILE`` (append, line-buffered).

    Each hook invocation is a separate process. Adapters should call this
    once at module import time so every entry point in the harness benefits
    without needing to wrap every function. With the redirect in place:

      - ``error(...)`` always writes → always lands in the log file.
      - ``log(...)`` writes only when ``ATATUS_VERBOSE=true`` → verbose
        noise lands in the file only when explicitly opted in.

    Idempotent: if a redirect is already active, this is a no-op. The original
    ``sys.stderr`` and the open file handle are stashed at module scope so
    ``restore_stderr_from_log_file()`` can reverse the redirect — useful in
    tests where adapter imports would otherwise leak stderr state across
    cases.

    Fail-soft: if the path can't be opened, stderr is left untouched and
    output falls back to whatever the host CLI does with hook stderr.
    No-op when ``ATATUS_LOG_FILE`` is unset.
    """
    global _original_stderr, _redirected_log_fh

    if _redirected_log_fh is not None:
        return  # already redirected

    log_file = os.environ.get("ATATUS_LOG_FILE")
    if not log_file:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return

    _original_stderr = sys.stderr
    _redirected_log_fh = fh
    sys.stderr = fh
    # Restore on interpreter shutdown so atexit hooks (and any late prints)
    # don't write to an FD that's about to close.
    atexit.register(restore_stderr_from_log_file)


def restore_stderr_from_log_file() -> None:
    """Reverse ``redirect_stderr_to_log_file()``. Idempotent.

    Restores the saved ``sys.stderr`` and closes the log file handle. Safe to
    call multiple times. No-op if no redirect is active.
    """
    global _original_stderr, _redirected_log_fh

    if _redirected_log_fh is None:
        return

    if _original_stderr is not None:
        sys.stderr = _original_stderr

    try:
        _redirected_log_fh.close()
    except OSError:
        pass

    _redirected_log_fh = None
    _original_stderr = None


def debug_dump(label: str, data: object) -> None:
    """Trace-level debug dump — only when ATATUS_TRACE_DEBUG=true.

    Writes JSON files to {STATE_DIR}/debug/{label}_{timestamp}.json.
    Used by Codex hooks for detailed payload inspection.
    """
    if os.environ.get("ATATUS_TRACE_DEBUG", "").lower() != "true":
        return
    try:
        from core.constants import STATE_BASE_DIR

        debug_dir = STATE_BASE_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        dump_file = debug_dir / f"{label}_{ts}.json"
        dump_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # debug dumps must never cause failures


# ---------------------------------------------------------------------------
# Target detection and span sending
# ---------------------------------------------------------------------------


#: Default Atatus OTLP collector. Override per-harness with ``ATATUS_OTLP_ENDPOINT``
#: or an ``endpoint`` key in ``~/.atatus/harness/config.json``.
DEFAULT_OTLP_ENDPOINT = "https://otel-rx.atatus.com"


def get_target() -> str:
    """Detect backend target from env vars.

    Returns "atatus" or "none".
    """
    if env.api_key:
        return "atatus"
    return "none"


def resolve_backend(span_dict: dict) -> dict:
    """Resolve backend config for a span payload.

    Precedence per field: env var > harnesses.<service_name> in
    ~/.atatus/harness/config.json > defaults. Used so marketplace-installed
    plugins (which skip the interactive wizard) can supply credentials
    purely via the runtime env block in ~/.claude/settings.json.

    Env vars consulted:
      - ATATUS_API_KEY        → license key
      - ATATUS_OTLP_ENDPOINT  → collector URL override
      - ATATUS_PROJECT_NAME   → project_name override

    service_name is pulled from the span's resource attributes
    (resource.attributes[service.name]).

    Returns:
      {"target": "atatus", "endpoint", "api_key", "project_name"}

    On any missing required field, logs a clear error via error() and
    returns {"target": "none", "project_name": project_name_or_""}.
    """
    _none = {"target": "none", "project_name": ""}

    # Extract service.name from span resource attributes
    service_name = ""
    for rs in span_dict.get("resourceSpans", []):
        for attr in rs.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                service_name = attr.get("value", {}).get("stringValue", "")
                break
        if service_name:
            break

    if not service_name:
        error("No service.name attribute found on span — cannot resolve harness config.")
        return _none

    # Load config (may be empty or missing the harness entry)
    try:
        from core.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}

    harness_cfg = cfg.get("harnesses", {}).get(service_name) or {}

    # Resolve project_name: env > config > service_name
    project_name = env.project_name or harness_cfg.get("project_name", "") or service_name

    # Endpoint: env > config > default. Only the license key is mandatory.
    endpoint = env.otlp_endpoint or harness_cfg.get("endpoint", "") or DEFAULT_OTLP_ENDPOINT
    api_key = env.api_key or harness_cfg.get("api_key", "")

    if not api_key:
        error(
            f"No Atatus license key for harness '{service_name}': set ATATUS_API_KEY "
            f"in env, or add a 'harnesses.{service_name}' entry with an 'api_key' to "
            f"~/.atatus/harness/config.json."
        )
        return {"target": "none", "project_name": project_name}

    return {
        "target": "atatus",
        "endpoint": endpoint,
        "api_key": api_key,
        "project_name": project_name,
    }


def _extract_span_name(span_dict: dict) -> str:
    """Extract the first span name from an OTLP payload."""
    try:
        return span_dict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
    except (KeyError, IndexError, TypeError):
        return "unknown"


def _set_resource_attr(resource: dict, key: str, value: str) -> None:
    """Set (upsert) a string resource attribute in OTLP JSON form."""
    attrs = resource.setdefault("attributes", [])
    for attr in attrs:
        if attr.get("key") == key:
            attr["value"] = {"stringValue": value}
            return
    attrs.append({"key": key, "value": {"stringValue": value}})


def _stamp_atatus_identity(span_dict: dict, project_name: str) -> dict:
    """Stamp Atatus project identity onto a payload's resource attributes.

    Atatus resolves (and auto-creates) a project from ``service.name`` on the
    OTLP resource, so ``service.name`` must carry the **user's project name** —
    not the harness slug. The harness slug moves to ``atatus.agent.harness``,
    where the receiver reads it to pick the project's subType.

    Resource attributes after this call:
      service.name         = <project name>       (project identity)
      atatus.project.type  = "llm"                (route to /llm, not /apm)
      atatus.agent.harness = <harness slug>       (subType, e.g. "claude-code")
      telemetry.sdk.name   = "atatus-coding-harness"

    Mirrors what the upstream Arize path sent as ``arize.project.name``: the
    project identity travels *in the payload*, not only in config. Kept at the
    resource level rather than duplicated onto every span — Atatus reads it from
    the resource, and each POST carries a single project.

    Returns a deep copy; never mutates the caller's dict.
    """
    import copy

    payload = copy.deepcopy(span_dict)
    for rs in payload.get("resourceSpans", []):
        resource = rs.setdefault("resource", {})

        harness = ""
        for attr in resource.get("attributes", []):
            if attr.get("key") == "service.name":
                harness = attr.get("value", {}).get("stringValue", "")
                break

        if project_name:
            _set_resource_attr(resource, "service.name", project_name)
        if harness:
            _set_resource_attr(resource, "atatus.agent.harness", harness)
        _set_resource_attr(resource, "atatus.project.type", "llm")
        _set_resource_attr(resource, "telemetry.sdk.name", "atatus-coding-harness")

    return payload


def send_span(span_dict: dict) -> bool:
    """Send a span payload directly to the configured backend.

    Backend is resolved per harness via ``resolve_backend()``, which checks
    env vars (ATATUS_API_KEY, ATATUS_OTLP_ENDPOINT, ATATUS_PROJECT_NAME) before
    falling back to ``~/.atatus/harness/config.json``. If neither path yields a
    license key, the span is dropped and an error is logged.

    Transport is OTLP/JSON over HTTP POST to ``<endpoint>/v1/traces``, with the
    license key in the ``api-key`` header. No dependencies beyond urllib.

    Before sending, ``_stamp_atatus_identity()`` rewrites the resource so
    ``service.name`` carries the project name (Atatus resolves and auto-creates
    projects from it) and the harness slug moves to ``atatus.agent.harness``.

    Never raises. Returns True on success, False on failure.
    """
    try:
        if env.dry_run:
            log(f"[dry-run] would send span: {_extract_span_name(span_dict)}")
            return True

        if env.verbose:
            log(f"span payload: {json.dumps(span_dict)}")

        backend = resolve_backend(span_dict)
        target = backend["target"]

        if target != "atatus":
            error("No backend configured (set ATATUS_API_KEY)")
            return False

        endpoint = backend.get("endpoint", DEFAULT_OTLP_ENDPOINT)
        api_key = backend["api_key"]

        # Stamp project identity onto the resource. Atatus resolves/auto-creates
        # the project from service.name, so this must run before serialization.
        payload = _stamp_atatus_identity(span_dict, backend.get("project_name", ""))

        # Normalize endpoint to an absolute URL for HTTP/JSON transport.
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = f"{endpoint.rstrip('/')}/v1/traces"
        else:
            url = f"https://{endpoint}/v1/traces"

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "api-key": api_key,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            error(f"Atatus send failed: HTTP {e.code}: {detail or e.reason}")
            return False
        except Exception as e:
            error(f"Atatus send failed: {e}")
            return False
    except Exception as e:
        error(f"send_span failed: {e}")
        return False


# --- Platform-specific lock implementation detection ---
try:
    import fcntl

    _LOCK_IMPL = "fcntl"
except ImportError:
    try:
        import msvcrt

        _LOCK_IMPL = "msvcrt"
    except ImportError:
        _LOCK_IMPL = "mkdir"


class FileLock:
    """Cross-platform file lock.

    Uses fcntl.flock on Unix, msvcrt.locking on Windows.
    Falls back to mkdir-based locking if neither is available.

    Usage:
        with FileLock(Path("/path/to/.lock"), timeout=3.0):
            # exclusive access

    The lock_path can be a file or directory path:
    - fcntl/msvcrt mode: creates/opens lock_path as a file
    - mkdir fallback: creates lock_path as a directory (matches bash behavior)
    """

    def __init__(self, lock_path: Path, timeout: float = 3.0) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._fd: Optional[IO[str]] = None
        self._method = _LOCK_IMPL

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self._method == "fcntl":
            self._acquire_fcntl()
        elif self._method == "msvcrt":
            self._acquire_msvcrt()
        else:
            self._acquire_mkdir()
        return self

    def __exit__(self, *args) -> None:
        if self._method == "fcntl":
            self._release_fcntl()
        elif self._method == "msvcrt":
            self._release_msvcrt()
        else:
            self._release_mkdir()

    def _acquire_fcntl(self) -> None:
        self._fd = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    # Force-acquire: close, remove, reopen
                    self._fd.close()
                    try:
                        self.lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._fd = open(self.lock_path, "w")
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                time.sleep(0.1)

    def _release_fcntl(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    def _acquire_msvcrt(self) -> None:
        self._fd = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    self._fd.close()
                    try:
                        self.lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._fd = open(self.lock_path, "w")
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    return
                time.sleep(0.1)

    def _release_msvcrt(self) -> None:
        if self._fd is not None:
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLOCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    def _acquire_mkdir(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.lock_path.mkdir()
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    # Force-acquire: remove and recreate (matches bash lines 67-70)
                    try:
                        shutil.rmtree(self.lock_path)
                    except OSError:
                        pass
                    try:
                        self.lock_path.mkdir()
                    except FileExistsError:
                        pass
                    return
                time.sleep(0.1)

    def _release_mkdir(self) -> None:
        try:
            self.lock_path.rmdir()
        except OSError:
            pass


class StateManager:
    """Per-session key-value state backed by a JSON file.

    All values are stored as strings (matching bash behavior where jq
    reads/writes everything as string arguments via --arg).

    The state_file and lock_path are set by the adapter when resolving
    the session (e.g., state_<session_id>.json with .lock_<session_id>).
    """

    def __init__(
        self,
        state_dir: Path,
        state_file: "Path | None" = None,
        lock_path: "Path | None" = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_file = Path(state_file) if state_file is not None else None
        self._lock_path = Path(lock_path) if lock_path is not None else None

    def init_state(self) -> None:
        """Create state directory and file.

        If file doesn't exist, create with empty dict.
        If file exists but is corrupted, overwrite with empty dict.
        Matches bash init_state() at common.sh:49-59.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file is None:
            return
        if not self.state_file.exists():
            self._write({})
        else:
            # Validate existing file; overwrite if corrupted
            try:
                data = self._read()
                if not isinstance(data, dict):
                    self._write({})
            except Exception:
                self._write({})

    def get(self, key: str) -> "str | None":
        """Read a value by key. Returns None if key missing or file missing.

        Does NOT acquire lock (read-only, matches bash get_state which
        doesn't call _lock_state).
        """
        data = self._read_safe()
        val = data.get(key)
        if val is None:
            return None
        return str(val)

    def set(self, key: str, value: str) -> None:
        """Set a key-value pair. Acquires lock.

        Value is always stored as string (matches bash: jq --arg v "$2").
        Uses atomic write: write to .tmp.{pid} then rename.
        """
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                data[key] = str(value)
                self._write(data)
        except Exception as e:
            error(f"set_state failed for key={key}: {e}")

    def delete(self, key: str) -> None:
        """Remove a key. No-op if missing. Acquires lock."""
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                data.pop(key, None)
                self._write(data)
        except Exception as e:
            error(f"del_state failed for key={key}: {e}")

    def increment(self, key: str) -> None:
        """Increment a numeric string value. Acquires lock.

        Missing key treated as "0" -> becomes "1".
        Non-numeric value treated as 0 -> becomes "1".
        Matches bash inc_state() at common.sh:101-108.
        """
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                current = data.get(key, "0")
                try:
                    num = int(current)
                except (ValueError, TypeError):
                    num = 0
                data[key] = str(num + 1)
                self._write(data)
        except Exception as e:
            error(f"inc_state failed for key={key}: {e}")

    def _lock(self) -> FileLock:
        """Return a FileLock for this state file."""
        if self._lock_path is not None:
            return FileLock(self._lock_path)
        if self.state_file is None:
            raise RuntimeError("StateManager has neither lock_path nor state_file")
        return FileLock(self.state_file.with_suffix(".lock"))

    def _read_safe(self) -> dict:
        """Read state file, return {} on any error (missing, corrupt, permission)."""
        try:
            return self._read()
        except Exception:
            return {}

    def _read(self) -> dict:
        """Read state file, raise on error."""
        if self.state_file is None:
            return {}
        text = self.state_file.read_text(encoding="utf-8")
        data = json.loads(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"State file is not a mapping: {type(data)}")
        return data

    def _write(self, data: dict) -> None:
        """Write dict to state file atomically via tmp+rename."""
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ── OTLP Span Building ────────────────────────────────────────────────────

# Map string kind names to OTLP SpanKind integer values.
# Case-insensitive lookup (caller passes "LLM", "TOOL", etc.)
SPAN_KIND_MAP: dict = {
    # kind 1 = SPAN_KIND_INTERNAL (used for LLM, CHAIN, TOOL, INTERNAL in OpenInference)
    "": 1,
    "llm": 1,
    "chain": 1,
    "tool": 1,
    "internal": 1,
    "span_kind_internal": 1,
    # kind 2 = SPAN_KIND_SERVER
    "server": 2,
    "span_kind_server": 2,
    # kind 3 = SPAN_KIND_CLIENT
    "client": 3,
    "span_kind_client": 3,
    # kind 4 = SPAN_KIND_PRODUCER
    "producer": 4,
    "span_kind_producer": 4,
    # kind 5 = SPAN_KIND_CONSUMER
    "consumer": 5,
    "span_kind_consumer": 5,
    # kind 0 = SPAN_KIND_UNSPECIFIED
    "unspecified": 0,
    "span_kind_unspecified": 0,
}


def _resolve_kind(kind: str) -> int:
    """Resolve a span kind string to an OTLP SpanKind integer.

    Case-insensitive lookup in SPAN_KIND_MAP. If not found and numeric, parse
    as int. Otherwise default to 1 (SPAN_KIND_INTERNAL).
    """
    lookup = SPAN_KIND_MAP.get(kind.lower())
    if lookup is not None:
        return lookup
    # Numeric string (matches bash: if [[ "$kind" =~ ^[0-9]+$ ]])
    try:
        return int(kind)
    except (ValueError, TypeError):
        return 1


def _to_otlp_attr_value(value) -> dict:
    """Convert a Python value to OTLP attribute value dict.

    Matches the jq type-detection logic in build_span:
    - bool → {"boolValue": v}          (check BEFORE int — bool is subclass of int)
    - int → {"intValue": v}
    - float with no fractional part → {"intValue": int(v)}   (matches jq: floor == value)
    - float with fractional part → {"doubleValue": v}
    - everything else → {"stringValue": str(v)}
    """
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        if value == int(value):
            return {"intValue": int(value)}
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attrs_to_otlp(attrs: dict) -> list:
    """Convert a flat Python dict to OTLP attribute list.

    Input:  {"session.id": "abc", "llm.token_count.prompt": 100}
    Output: [{"key": "session.id", "value": {"stringValue": "abc"}},
             {"key": "llm.token_count.prompt", "value": {"intValue": 100}}]
    """
    return [{"key": k, "value": _to_otlp_attr_value(v)} for k, v in attrs.items()]


# ---------------------------------------------------------------------------
# Emit-time hygiene (M4.1)
#
# Ported from the Arize collector, which needed these because it read Claude
# Code's native telemetry. They apply to transcript-derived data too, and are
# also implemented defensively in the atatus-go consumer -- either layer alone
# would do, but the agent is the cheaper place to fix it and the consumer
# protects producers we do not control.
#
# Applied centrally in build_span() rather than at each emit site: there are 16
# places across 9 harness handlers that set a model name, and a rule enforced in
# one of them is a rule that silently lapses in the other fifteen.
# ---------------------------------------------------------------------------

#: Claude Code appends a context-window marker to the model id it reports, e.g.
#: ``claude-sonnet-4-5[1m]``. Cost lookup is an exact-string match against the
#: pricing table, so the suffix makes the model unknown and the span prices at
#: $0 -- silently, because a missing price is indistinguishable from a free turn.
_REDACTED_PLACEHOLDER = "<REDACTED>"


def normalize_model_name(model: str) -> str:
    """Strip a bracketed suffix from a wire model id.

    ``claude-sonnet-4-5[1m]`` -> ``claude-sonnet-4-5``. Mirrors the collector's
    ``normalizeModelName`` (mapper.go:221-227).
    """
    model = (model or "").strip()
    idx = model.find("[")
    if idx >= 0:
        model = model[:idx].strip()
    return model


def strip_system_reminders(text: str) -> str:
    """Remove ``<system-reminder>...</system-reminder>`` blocks from *text*.

    Claude Code injects these into the user turn; they are harness scaffolding,
    not something the user typed, and they dominate prompt text if kept. Mirrors
    the collector's ``stripSystemReminderText`` (outputs.go:518-534), including
    its tolerance of an unclosed tag -- an unterminated block is left alone
    rather than swallowing the rest of the prompt.
    """
    text = text or ""
    start_tag, end_tag = "<system-reminder>", "</system-reminder>"
    removed = False
    while True:
        start = text.find(start_tag)
        if start < 0:
            break
        end = text.find(end_tag, start)
        if end < 0:
            break
        text = text[:start] + text[end + len(end_tag) :]
        removed = True
    # Trim only when something was actually removed. The collector trims
    # unconditionally, but it only ever called this on user prompt text -- we
    # apply it to every span's input/output, so an unconditional strip would
    # silently eat trailing newlines from tool output that never contained a
    # reminder at all.
    return text.strip() if removed else text


def _apply_hygiene(attrs: dict) -> dict:
    """Normalize model ids and clean prompt text on an outgoing attribute set."""
    model = attrs.get("llm.model_name")
    if isinstance(model, str):
        normalized = normalize_model_name(model)
        if normalized:
            attrs["llm.model_name"] = normalized
        else:
            # An all-suffix or blank model is not a model; omitting it keeps
            # "unknown" distinguishable from "known and unpriced" downstream.
            attrs.pop("llm.model_name", None)

    for key in ("input.value", "output.value"):
        val = attrs.get(key)
        if not isinstance(val, str):
            continue
        # The collector treats a bare <REDACTED> as absent rather than storing
        # the literal string as if it were content (mapper.go:82).
        if val.strip() == _REDACTED_PLACEHOLDER:
            attrs.pop(key, None)
            continue
        cleaned = strip_system_reminders(val)
        if cleaned != val:
            attrs[key] = cleaned
    return attrs


def build_span(
    name: str,
    kind: str,
    span_id: str,
    trace_id: str,
    parent_span_id: str = "",
    start_ms: "int | str" = 0,
    end_ms: "int | str" = 0,
    attrs: "dict | None" = None,
    service_name: str = "coding-harness-tracing",
    scope_name: str = "coding-harness-tracing",
    status_code: int = 1,
    status_message: str = "",
) -> dict:
    """Build an OTLP JSON span payload.

    Returns a dict matching the exact structure produced by core/common.sh:build_span().

    Timestamp handling: start_ms and end_ms are in milliseconds. The OTLP format
    requires nanoseconds as strings. Bash appends "000000" (line 312):
        "startTimeUnixNano":"${start}000000"
    Python does the same: f"{int(start_ms)}000000"

    If end_ms is empty/None/0, defaults to start_ms (matches bash: end="${7:-$start}").
    """
    attrs = {} if attrs is None else dict(attrs)
    for k, v in env.custom_attributes(service_name).items():
        attrs.setdefault(k, v)

    start = int(start_ms) if start_ms else 0
    end = int(end_ms) if end_ms else start

    kind_value = _resolve_kind(kind or "")

    status: dict = {"code": status_code}
    if status_message:
        status["message"] = status_message

    span_obj = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind_value,
        "startTimeUnixNano": f"{start}000000",
        "endTimeUnixNano": f"{end}000000",
        "attributes": _attrs_to_otlp(_apply_hygiene(attrs)),
        "status": status,
    }

    # parentSpanId only included if non-empty (matches bash conditional)
    if parent_span_id:
        span_obj["parentSpanId"] = parent_span_id

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                "scopeSpans": [{"scope": {"name": scope_name}, "spans": [span_obj]}],
            }
        ]
    }


def build_multi_span(
    span_payloads: list,
    service_name: str = "coding-harness-tracing",
    scope_name: str = "coding-harness-tracing",
) -> dict:
    """Merge multiple build_span() outputs into a single resourceSpans payload.

    Extracts the span object from each payload's
    resourceSpans[0].scopeSpans[0].spans[0] and combines them under
    one resource/scope envelope.

    Returns {} if no valid spans found (matches bash: echo "{}"; return 1).
    """
    spans = []
    for payload in span_payloads:
        try:
            span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            spans.append(span)
        except (KeyError, IndexError, TypeError):
            continue

    if not spans:
        return {}

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                "scopeSpans": [{"scope": {"name": scope_name}, "spans": spans}],
            }
        ]
    }
