#!/usr/bin/env python3
"""Cursor-specific adapter: deterministic trace IDs, state stack, sanitization.

Cursor is architecturally different from Claude Code and Codex — it uses a
single dispatcher for all 12 hook events, deterministic trace IDs from
generation IDs, and a disk-backed state stack for merging before/after hook
pairs.

Replaces cursor-tracing/hooks/common.sh (195 lines).
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from core.common import FileLock, env, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants from HARNESSES["cursor"] ---
_HARNESS = HARNESSES["cursor"]
SERVICE_NAME = _HARNESS["service_name"]  # "cursor"
SCOPE_NAME = _HARNESS["scope_name"]  # "atatus-cursor-tracing"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.atatus/harness/state/cursor
MAX_ATTR_CHARS = int(os.environ.get("CURSOR_TRACE_MAX_ATTR_CHARS", "100000"))
MAX_THOUGHT_HASHES = 256
MAX_RESPONSE_FRAGMENTS = 64
MAX_RESPONSE_FRAGMENT_CHARS = 10000
MAX_TERMINAL_MARKERS = 4096
MAX_GENERATION_ALIASES = 64

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ATATUS_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def trace_id_from_seed(seed: str) -> str:
    """Deterministic 32-hex trace ID from a Cursor identifier.

    Hashing rather than a random id is what lets separate hook processes — one
    per event, with no shared memory — agree on the trace a span belongs to.

    SHA-256 rather than MD5: the digest is only a deterministic mapping, but
    MD5 raises on a host running OpenSSL in FIPS mode.
    """
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


trace_id_from_generation = trace_id_from_seed


def span_id_16() -> str:
    """Generate 16-hex random span ID.

    Replaces bash: od -An -tx1 -N8 /dev/urandom | tr -d ' \\n' | cut -c1-16
    """
    return os.urandom(8).hex()


def sanitize(s: str) -> str:
    """Replace non-alphanumeric characters (except ._-) with underscore.

    Matches bash: printf '%s' "$1" | tr -c '[:alnum:]._-' '_'
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def truncate_attr(s: str, max_chars: "int | None" = None) -> str:
    limit = max_chars if max_chars is not None else MAX_ATTR_CHARS
    return s[:limit] if len(s) > limit else s


def conversation_state_key(conversation_id: str) -> str:
    digest = hashlib.sha256(conversation_id.encode()).hexdigest()[:10]
    readable = sanitize(conversation_id)[:80]
    return f"{readable}_{digest}"




def _active_turn_paths(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"active_{safe}.json", STATE_DIR / f".lock_active_{safe}"


def _read_json_dict(path) -> "dict | None":
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path, value: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def active_turn_save(conversation_id: str, value: dict) -> bool:
    if not conversation_id or not isinstance(value, dict):
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            _write_json_atomic(path, value)
        return True
    except OSError:
        return False


def active_turn_get(conversation_id: str) -> "dict | None":
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            return _read_json_dict(path)
    except OSError:
        return None


def active_turn_update(conversation_id: str, updates: dict) -> "dict | None":
    if not conversation_id or not isinstance(updates, dict):
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            state.update(updates)
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_add_response(
    conversation_id: str,
    text: str,
    observed_ms: int,
    model: str = "",
    user_id: str = "",
    expected_root_span_id: str = "",
    login_id: str = "",
    effort: str = "",
    thinking: bool = False,
) -> "dict | None":
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return None
            responses = state.get("responses")
            if not isinstance(responses, list):
                responses = []
            response_start_ms = int(state.get("last_activity_ms") or state.get("start_ms") or observed_ms)
            responses.append(
                {
                    "text": truncate_attr(text, MAX_RESPONSE_FRAGMENT_CHARS),
                    "start_ms": response_start_ms,
                    "observed_ms": observed_ms,
                }
            )
            state["responses"] = responses[-MAX_RESPONSE_FRAGMENTS:]
            state["last_activity_ms"] = observed_ms
            if model:
                state["model"] = model
            if user_id:
                state["user_id"] = user_id
            if login_id:
                state["login_id"] = login_id
            if effort:
                state["effort"] = effort
            if thinking:
                state["thinking"] = thinking
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_add_thought_hash(
    conversation_id: str,
    thought_hash: str,
    observed_ms: int,
    expected_root_span_id: str = "",
) -> bool:
    if not conversation_id or not thought_hash:
        return True
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return True
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return False
            hashes = state.get("thought_hashes")
            if not isinstance(hashes, list):
                hashes = []
            if thought_hash in hashes:
                return False
            hashes.append(thought_hash)
            state["thought_hashes"] = hashes[-MAX_THOUGHT_HASHES:]
            state["last_activity_ms"] = observed_ms
            _write_json_atomic(path, state)
            return True
    except OSError:
        return True


def active_turn_clear(conversation_id: str) -> "dict | None":
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            path.unlink(missing_ok=True)
            return state
    except OSError:
        return None


def turn_matches_generation(turn: dict, generation_id: str) -> bool:
    if not generation_id:
        return True
    if generation_id == turn.get("generation_id"):
        return True
    aliases = turn.get("generation_aliases")
    return isinstance(aliases, list) and generation_id in aliases


def active_turn_record_alias(
    conversation_id: str,
    generation_id: str,
    observed_ms: int,
    expected_root_span_id: str = "",
) -> "dict | None":
    if not conversation_id or not generation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return None
            if generation_id == state.get("generation_id"):
                state["last_activity_ms"] = observed_ms
                _write_json_atomic(path, state)
                return state
            aliases = state.get("generation_aliases")
            if not isinstance(aliases, list):
                aliases = []
            if generation_id not in aliases:
                aliases.append(generation_id)
                state["generation_aliases"] = aliases[-MAX_GENERATION_ALIASES:]
            state["last_activity_ms"] = observed_ms
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_clear_if_matches(
    conversation_id: str,
    root_span_id: str = "",
    generation_id: str = "",
) -> "dict | None":
    if not conversation_id or not (root_span_id or generation_id):
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            root_matches = bool(root_span_id) and state.get("root_span_id") == root_span_id
            gen_matches = bool(generation_id) and turn_matches_generation(state, generation_id)
            if not (root_matches or gen_matches):
                return None
            path.unlink(missing_ok=True)
            return state
    except OSError:
        return None


def _terminal_paths(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"terminal_{safe}", STATE_DIR / f".lock_terminal_{safe}"


def _merge_terminal_markers(existing, candidates) -> list:
    merged = list(existing) if isinstance(existing, list) else []
    for value in candidates:
        if value and value not in merged:
            merged.append(value)
    return merged[-MAX_TERMINAL_MARKERS:]


def terminal_turn_claim_many(conversation_id: str, generation_ids) -> bool:
    if not conversation_id:
        return True
    candidates = [value for value in dict.fromkeys(generation_ids) if value]
    if not candidates:
        return True
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text(encoding="utf-8").strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            if isinstance(existing, list) and any(value in existing for value in candidates):
                return False
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(_merge_terminal_markers(existing, candidates)), encoding="utf-8")
            tmp.replace(path)
            return True
    except OSError:
        return True


def terminal_turn_claim(
    conversation_id: str,
    generation_id: str,
    alternate_generation_id: str = "",
) -> bool:
    return terminal_turn_claim_many(conversation_id, (generation_id, alternate_generation_id))


def terminal_turn_mark_many(conversation_id: str, generation_ids) -> None:
    if not conversation_id:
        return
    candidates = [value for value in dict.fromkeys(generation_ids) if value]
    if not candidates:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text(encoding="utf-8").strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            merged = _merge_terminal_markers(existing, candidates)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(merged), encoding="utf-8")
            tmp.replace(path)
    except OSError:
        return


def terminal_turn_is_marked(conversation_id: str, generation_id: str) -> bool:
    if not conversation_id or not generation_id:
        return False
    try:
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text(encoding="utf-8").strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            return isinstance(existing, list) and generation_id in existing
    except OSError:
        return False


def terminal_turn_mark(
    conversation_id: str,
    generation_id: str,
    alternate_generation_id: str = "",
) -> None:
    terminal_turn_mark_many(conversation_id, (generation_id, alternate_generation_id))


def _terminal_nogen_path(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"terminal_nogen_{safe}"


def terminal_nogen_claim(conversation_id: str) -> bool:
    if not conversation_id:
        return True
    path, lock_path = _terminal_paths(conversation_id)
    sentinel = _terminal_nogen_path(conversation_id)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(lock_path):
            if sentinel.exists():
                return False
            sentinel.write_text("1", encoding="utf-8")
            return True
    except OSError:
        return True


def terminal_nogen_clear(conversation_id: str) -> None:
    if not conversation_id:
        return
    try:
        _, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            _terminal_nogen_path(conversation_id).unlink(missing_ok=True)
    except OSError:
        return


def conversation_cleanup(conversation_id: str) -> None:
    if not conversation_id:
        return
    active_path, active_lock = _active_turn_paths(conversation_id)
    try:
        with FileLock(active_lock):
            active_path.unlink(missing_ok=True)
    except OSError:
        pass


# --- Disk-backed state stack (LIFO) ---
# Replaces bash state_push/state_pop at lines 59-132.
# Used to merge before/after hook pairs (e.g., beforeShellExecution pushes
# command + start time, afterShellExecution pops it to create a merged span).


def state_push(key: str, value: dict) -> None:
    """Push a dict onto a named stack.

    Stack file: STATE_DIR/{key}.stack.json — a JSON list.
    Uses FileLock for concurrent access.

    Matches bash state_push() at lines 59-87.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"

    with FileLock(lock_path):
        if stack_file.exists():
            try:
                data = json.loads(stack_file.read_text(encoding="utf-8")) or []
            except json.JSONDecodeError:
                data = []
        else:
            data = []

        if not isinstance(data, list):
            data = []

        data.append(value)

        tmp = stack_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(stack_file)


def state_pop(key: str) -> "dict | None":
    """Pop the last value from a named stack. Returns None if empty.

    Matches bash state_pop() at lines 91-132.
    """
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"

    with FileLock(lock_path):
        if not stack_file.exists():
            return None

        try:
            data = json.loads(stack_file.read_text(encoding="utf-8")) or []
        except json.JSONDecodeError:
            return None

        if not isinstance(data, list) or len(data) == 0:
            return None

        value = data[-1]
        data = data[:-1]

        tmp = stack_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(stack_file)

    return value if isinstance(value, dict) else None


# --- Root span tracking per generation ---
# Replaces bash lines 138-155.


def gen_root_span_save(gen_id: str, span_id: str) -> None:
    """Save the root span ID for a generation.

    Written by beforeSubmitPrompt, read by all other events to set parent_span_id.
    File: STATE_DIR/root_{sanitized_gen_id}
    Contains: just the span_id as plain text.

    Written via a temp file and rename: Cursor runs one process per hook event
    concurrently, and a reader that caught a plain write half-done would use a
    truncated span id as a parent.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = sanitize(gen_id)
    root_file = STATE_DIR / f"root_{safe}"
    tmp = root_file.with_name(f"{root_file.name}.tmp.{os.getpid()}")
    tmp.write_text(span_id, encoding="utf-8")
    tmp.replace(root_file)


def gen_root_span_get(gen_id: str) -> str:
    """Get the root span ID for a generation. Returns "" if not found."""
    if not gen_id:
        return ""
    safe = sanitize(gen_id)
    root_file = STATE_DIR / f"root_{safe}"
    if root_file.exists():
        return root_file.read_text(encoding="utf-8").strip()
    return ""


# --- Generation cleanup ---
# Replaces bash state_cleanup_generation() at lines 159-176.


def state_cleanup_generation(gen_id: str) -> None:
    """Remove all state files for a generation (called by stop hook).

    Cleans up:
    1. Root span file: root_{sanitized_gen_id}
    2. Stack files: *{sanitized_gen_id}*.stack.json
    3. Lock dirs: .lock_*{sanitized_gen_id}*

    Matches bash lines 159-176.
    """
    safe = sanitize(gen_id)
    if not safe:
        # A blank id makes the glob "**", which Path.glob rejects outright.
        return

    # Root span file
    root_file = STATE_DIR / f"root_{safe}"
    root_file.unlink(missing_ok=True)

    # Stack files containing this generation ID
    for f in STATE_DIR.glob(f"*{safe}*.stack.json"):
        f.unlink(missing_ok=True)

    # Locks containing this generation ID. FileLock only creates a directory in
    # its mkdir fallback; with fcntl or msvcrt available it creates a plain
    # file, which is every platform we actually run on.
    for path in STATE_DIR.glob(f".lock_*{safe}*"):
        _remove_lock(path)


def _remove_lock(path: Path) -> None:
    """Remove a lock left behind by FileLock, whether file or directory."""
    try:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def gc_stale_state_files(max_age_seconds: int = 6 * 60 * 60) -> None:
    """Delete state left by turns that never reached their closing hook.

    A cancelled turn, or one whose stop hook carried a different generation id,
    leaves its stack and root files behind with nothing that will ever pop them.
    Cursor's state keys hold no pid, so age is the only signal available —
    unlike the harnesses whose keys let them check whether the process is alive.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - max_age_seconds
    for path in STATE_DIR.iterdir():
        name = path.name
        if not (
            name.endswith(".stack.json")
            or name.startswith(("root_", "active_", "terminal_"))
            or name.startswith(".lock_")
        ):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if name.startswith(".lock_"):
            _remove_lock(path)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# --- Requirements check ---


def check_requirements() -> bool:
    """Check tracing enabled, ensure state directory exists."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True
