"""Kiro CLI adapter — single-mode session resolution, init, GC, sidecar mining.

Kiro CLI provides KIRO_SESSION_ID as an env var on every hook invocation
(also echoed in the stdin payload as session_id). State files are keyed by
this UUID; no PID fallback needed because session_id is stable across all
hooks of one CLI run.

This module also exposes helpers for reading the per-session sidecar
(`~/.kiro/sessions/cli/<session_id>.json`) used to enrich LLM spans with
model name, token counts, and metering usage. All sidecar helpers are
fail-soft — they return `None` on any error.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from core.common import StateManager, env, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import STATE_BASE_DIR
from core.identity import read_kiro_login_probe, start_kiro_login_probe
from tracing.kiro.constants import HARNESS_NAME, KIRO_SESSIONS_DIR

# How long to keep checking the background login probe before giving up and
# caching "" for the rest of the session. Real observed latency is ~1.9s;
# this is a generous multiple, not a tight bound — unlike the subprocess
# timeouts elsewhere, nothing here is ever blocked waiting on this deadline.
_LOGIN_PROBE_GRACE_MS = 10_000

STATE_DIR: Path = STATE_BASE_DIR / HARNESS_NAME
SCOPE_NAME = "atatus-kiro-tracing"
SERVICE_NAME = HARNESS_NAME

# Kiro writes the session sidecar asynchronously, so the stop hook can beat it
# to disk. Poll briefly rather than reading once and losing the metering data.
_SIDECAR_RETRY_SECS = 1.0
_SIDECAR_POLL_INTERVAL = 0.1

# Route hook stderr to a per-harness log file unless ATATUS_LOG_FILE is set.
os.environ.setdefault(
    "ATATUS_LOG_FILE",
    str(Path.home() / ".atatus" / "harness" / "logs" / "kiro.log"),
)
redirect_stderr_to_log_file()


def check_requirements() -> bool:
    """Return True if env.trace_enabled. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed off the Kiro session UUID.

    Order: payload.session_id → KIRO_SESSION_ID env → "unknown-<pid>".
    The fallback is a last-resort guard so we never crash; it logs a warning.
    """
    key = input_json.get("session_id") or os.environ.get("KIRO_SESSION_ID", "")
    if not key:
        key = f"unknown-{os.getpid()}"
        log(f"resolve_session: no session_id in payload or env; using {key}")

    state_file = STATE_DIR / f"state_{key}.json"
    lock_path = STATE_DIR / f".lock_{key}"

    sm = StateManager(state_dir=STATE_DIR, state_file=state_file, lock_path=lock_path)
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session initialization.

    On first call, populate session_id (Kiro's UUID — preserves correlation),
    session_start_time, trace_count, tool_count, user_id.
    Subsequent calls are no-ops.
    """
    if state.get("session_id") is not None:
        return

    # Preserve Kiro's payload UUID as the Atatus session.id. This is what lets
    # users find a Kiro session in Atatus. NEVER substitute a fresh trace ID.
    session_id = input_json.get("session_id") or os.environ.get("KIRO_SESSION_ID") or ""

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("trace_count", "0")
    state.set("tool_count", "0")
    state.set("user_id", env.get_user_id(SERVICE_NAME) or "")

    # user_login_id is resolved asynchronously — see resolve_user_login_id().
    # Kick off the background probe now so it has the most time to finish
    # before any span needs the value; never wait on it here.
    probe_path = STATE_DIR / f".login_probe_{session_id or os.getpid()}"
    state.set("user_login_id_probe_path", str(probe_path))
    state.set("user_login_id_probe_started_at", str(get_timestamp_ms()))
    start_kiro_login_probe(probe_path)

    log(f"Session initialized: {session_id}")


def resolve_user_login_id(state: StateManager) -> str:
    """Non-blocking read of the async login-id probe started at session init.

    Returns the cached value once resolved. Until then, returns "" for the
    current span without giving up — the next hook call in this session will
    check again — except once _LOGIN_PROBE_GRACE_MS has passed with no result,
    at which point it caches "" permanently so a probe that never finishes
    (kiro-cli missing, not logged in, hung) doesn't get re-checked forever.
    """
    cached = state.get("user_login_id")
    if cached is not None:
        return cached

    probe_path_str = state.get("user_login_id_probe_path")
    if not probe_path_str:
        return ""

    result = read_kiro_login_probe(Path(probe_path_str))
    if result is not None:
        state.set("user_login_id", result)
        return result

    started_at = int(state.get("user_login_id_probe_started_at") or "0")
    if started_at and get_timestamp_ms() - started_at > _LOGIN_PROBE_GRACE_MS:
        state.set("user_login_id", "")
    return ""


def gc_stale_state_files() -> None:
    """Remove state and lock files older than 24h. Mirrors the gemini pattern."""
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for f in STATE_DIR.glob("state_*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                lock = STATE_DIR / f".lock_{f.stem.replace('state_', '', 1)}"
                lock.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Session sidecar mining
# ---------------------------------------------------------------------------


def _sidecar_has_metering(data: dict) -> bool:
    """True once the most recent turn carries metering usage."""
    turns = data.get("session_state", {}).get("conversation_metadata", {}).get("user_turn_metadatas", [])
    return bool(turns) and bool(turns[-1].get("metering_usage"))


def load_session_sidecar(session_id: str) -> dict | None:
    """Load `~/.kiro/sessions/cli/<session_id>.json`.

    Retries for up to `_SIDECAR_RETRY_SECS`, because Kiro flushes the sidecar
    *after* the stop hook fires — reading once wins that race only sometimes,
    and losing it drops the turn's token counts and metering cost silently.
    Returns early as soon as metering data is present.

    Returns the parsed dict (possibly without metering, if it never arrived),
    or None if the file is missing, malformed, or not a JSON object. NEVER
    raises — callers rely on fail-soft semantics.
    """
    if not session_id:
        return None
    path = KIRO_SESSIONS_DIR / f"{session_id}.json"
    deadline = time.monotonic() + _SIDECAR_RETRY_SECS
    last: dict | None = None
    last_exc: Exception | None = None

    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log(f"sidecar for {session_id} is not a JSON object")
                return None
            last, last_exc = data, None
            if _sidecar_has_metering(data):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            last_exc = exc

        if time.monotonic() >= deadline:
            break
        time.sleep(_SIDECAR_POLL_INTERVAL)

    if last_exc is not None:
        log(f"sidecar load failed for {session_id}: {last_exc!r}")
    else:
        log(f"sidecar for {session_id}: metering_usage not present after {_SIDECAR_RETRY_SECS}s")
    # Whatever we last parsed is still worth enriching with (model name, token
    # counts) even when metering never landed.
    return last


def extract_sidecar_attrs(sidecar: dict | None, turn_index: int = -1) -> dict[str, Any]:
    """Distill enrichment span attributes from a sidecar.

    `turn_index = -1` selects the most recent completed turn.

    Returns a dict of attribute name → value for any fields that are
    present and meaningful. Token counts of 0 are treated as unknown
    and omitted. Cost is the sum of metering values, attached only when
    > 0. Always fail-soft — silently skip missing or malformed branches.
    """
    out: dict[str, Any] = {}
    if not isinstance(sidecar, dict):
        return out

    state = sidecar.get("session_state")
    if not isinstance(state, dict):
        return out

    agent_name = state.get("agent_name")
    if isinstance(agent_name, str) and agent_name:
        out["kiro.agent_name"] = agent_name

    rts = state.get("rts_model_state")
    if isinstance(rts, dict):
        model_info = rts.get("model_info")
        if isinstance(model_info, dict):
            model_id = model_info.get("model_id")
            if isinstance(model_id, str) and model_id:
                out["llm.model_name"] = model_id
        ctx_pct = rts.get("context_usage_percentage")
        if isinstance(ctx_pct, (int, float)):
            out["kiro.context_usage_percentage"] = float(ctx_pct)

    conv_meta = state.get("conversation_metadata")
    if not isinstance(conv_meta, dict):
        return out
    turns = conv_meta.get("user_turn_metadatas")
    if not isinstance(turns, list) or not turns:
        return out
    try:
        turn = turns[turn_index]
    except IndexError:
        return out
    if not isinstance(turn, dict):
        return out

    in_tok = turn.get("input_token_count")
    out_tok = turn.get("output_token_count")
    if isinstance(in_tok, int) and in_tok > 0:
        out["llm.token_count.prompt"] = in_tok
    if isinstance(out_tok, int) and out_tok > 0:
        out["llm.token_count.completion"] = out_tok
    if isinstance(in_tok, int) and in_tok > 0 and isinstance(out_tok, int) and out_tok > 0:
        out["llm.token_count.total"] = in_tok + out_tok

    metering = turn.get("metering_usage")
    if isinstance(metering, list) and metering:
        out["kiro.metering_usage"] = json.dumps(metering)
        cost = 0.0
        for entry in metering:
            if isinstance(entry, dict):
                v = entry.get("value")
                if isinstance(v, (int, float)):
                    cost += float(v)
        if cost > 0:
            out["kiro.cost.credits"] = cost

    duration = turn.get("turn_duration")
    if isinstance(duration, dict):
        secs = duration.get("secs")
        nanos = duration.get("nanos")
        if isinstance(secs, int) and isinstance(nanos, int):
            out["kiro.turn_duration_ms"] = secs * 1000 + nanos // 1_000_000

    return out
