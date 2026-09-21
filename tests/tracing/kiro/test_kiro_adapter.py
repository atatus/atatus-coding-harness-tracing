#!/usr/bin/env python3
"""Tests for tracing.kiro.hooks.adapter — session resolution, init, GC, sidecar mining.

Mirrors tests/test_gemini_adapter.py structure but adapted for Kiro's
stable session_id UUID (no PID fallback needed) and session sidecar enrichment.
"""
from __future__ import annotations

import copy
import json
import os
import time

import pytest

from core.common import StateManager

# ---------------------------------------------------------------------------
# We import the adapter module itself so we can monkeypatch its module-level
# constants.  The actual functions under test are attributes of this module.
# ---------------------------------------------------------------------------
from tracing.kiro.hooks import adapter

# ── Paths to fixture files ──────────────────────────────────────────────────

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
PROBE_PAYLOADS_DIR = os.path.join(FIXTURES_DIR, "probe_payloads")
SIDECARS_DIR = os.path.join(FIXTURES_DIR, "sidecars")


def _load_fixture(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def kiro_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "kiro" / "state" / "kiro"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("ATATUS_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ATATUS_USER_ID", raising=False)
    monkeypatch.delenv("KIRO_SESSION_ID", raising=False)
    monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")


@pytest.fixture
def agent_spawn_payload():
    """Load the agentSpawn probe payload fixture."""
    return _load_fixture(os.path.join(PROBE_PAYLOADS_DIR, "agent_spawn.json"))


@pytest.fixture
def sidecar_complete():
    """Load session_complete.json sidecar fixture."""
    return _load_fixture(os.path.join(SIDECARS_DIR, "session_complete.json"))


@pytest.fixture
def sidecar_no_turns():
    """Load session_no_turns.json sidecar fixture."""
    return _load_fixture(os.path.join(SIDECARS_DIR, "session_no_turns.json"))


@pytest.fixture
def sidecar_dir(tmp_path, monkeypatch):
    """Point adapter.KIRO_SESSIONS_DIR to a temp directory and return it."""
    monkeypatch.setattr(adapter, "KIRO_SESSIONS_DIR", tmp_path)
    return tmp_path


# ── check_requirements tests ────────────────────────────────────────────────


class TestCheckRequirements:
    def test_disabled_returns_false_no_dir(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False and STATE_DIR not created."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "kiro-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()

    def test_enabled_creates_dir(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "kiro-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()


# ── resolve_session tests ───────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_payload_session_id(self, kiro_state_dir, disable_env_vars):
        """Payload session_id is used as the state file key."""
        sm = adapter.resolve_session({"session_id": "abc-123"})
        assert sm.state_file == kiro_state_dir / "state_abc-123.json"
        assert sm.state_file.exists()

    def test_uses_env_var_when_payload_missing(self, kiro_state_dir, disable_env_vars, monkeypatch):
        """KIRO_SESSION_ID env var is used when payload has no session_id."""
        monkeypatch.setenv("KIRO_SESSION_ID", "env-456")
        sm = adapter.resolve_session({})
        assert sm.state_file == kiro_state_dir / "state_env-456.json"

    def test_payload_takes_precedence_over_env(self, kiro_state_dir, disable_env_vars, monkeypatch):
        """Payload session_id wins over KIRO_SESSION_ID env var."""
        monkeypatch.setenv("KIRO_SESSION_ID", "from-env")
        sm = adapter.resolve_session({"session_id": "from-payload"})
        assert sm.state_file == kiro_state_dir / "state_from-payload.json"

    def test_unknown_fallback_when_both_missing(self, kiro_state_dir, disable_env_vars):
        """Falls back to unknown-<pid> when neither payload nor env has session_id."""
        adapter.resolve_session({})
        # State file should match the unknown-* pattern
        assert list(kiro_state_dir.glob("state_unknown-*.json"))


# ── ensure_session_initialized tests ────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, kiro_state_dir, key="test"):
        sm = StateManager(
            state_dir=kiro_state_dir,
            state_file=kiro_state_dir / f"state_{key}.json",
            lock_path=kiro_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_first_call_sets_all_keys(self, kiro_state_dir, disable_env_vars, agent_spawn_payload):
        """First call sets all six expected keys."""
        sm = self._make_state(kiro_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, agent_spawn_payload)
        assert sm.get("session_id") is not None
        assert sm.get("session_start_time") is not None
        assert sm.get("trace_count") is not None
        assert sm.get("tool_count") is not None
        assert sm.get("user_id") is not None

    def test_session_id_preserves_kiro_uuid(self, kiro_state_dir, disable_env_vars):
        """session_id in state preserves the Kiro UUID, not a generated trace ID.

        This is the comment-4 pattern from PR #28: the Kiro UUID is the
        correlation ID that lets users find a Kiro session in Atatus.
        """
        sm = self._make_state(kiro_state_dir, "uuid-preserve")
        kiro_uuid = "00000000-0000-0000-0000-000000000001"
        adapter.ensure_session_initialized(sm, {"session_id": kiro_uuid})
        stored = sm.get("session_id")
        assert stored == kiro_uuid
        # Must NOT be a 32-hex-char generated trace ID
        assert "-" in stored  # UUIDs have dashes; generated trace IDs do not

    def test_idempotent(self, kiro_state_dir, disable_env_vars):
        """Second call with different payload session_id does NOT overwrite."""
        sm = self._make_state(kiro_state_dir, "idempotent")
        original_uuid = "00000000-0000-0000-0000-000000000001"
        adapter.ensure_session_initialized(sm, {"session_id": original_uuid})
        original_start = sm.get("session_start_time")

        # Second call with a different session_id — must be a no-op
        adapter.ensure_session_initialized(sm, {"session_id": "different-uuid"})
        assert sm.get("session_id") == original_uuid
        assert sm.get("session_start_time") == original_start

    def test_counters_start_at_zero(self, kiro_state_dir, disable_env_vars, agent_spawn_payload):
        """trace_count and tool_count start at '0' strings."""
        sm = self._make_state(kiro_state_dir, "counters")
        adapter.ensure_session_initialized(sm, agent_spawn_payload)
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"


# ── login_id async probe tests ──────────────────────────────────────────────


class TestResolveUserLoginId:
    def _make_state(self, kiro_state_dir, key="test"):
        sm = StateManager(
            state_dir=kiro_state_dir,
            state_file=kiro_state_dir / f"state_{key}.json",
            lock_path=kiro_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_session_init_never_blocks_on_kiro_cli(self, kiro_state_dir, disable_env_vars, monkeypatch):
        """Real kiro-cli takes ~1.9s; ensure_session_initialized must return
        immediately regardless — it only ever starts the probe, never waits."""
        started = {}

        def fake_start_probe(dest_path):
            started["dest_path"] = dest_path

        monkeypatch.setattr(adapter, "start_kiro_login_probe", fake_start_probe)
        sm = self._make_state(kiro_state_dir, "no-block")
        adapter.ensure_session_initialized(sm, {"session_id": "sess-1"})
        assert "dest_path" in started
        assert sm.get("user_login_id_probe_path") == str(started["dest_path"])
        # Not yet resolved — that only happens via resolve_user_login_id().
        assert sm.get("user_login_id") is None

    def test_returns_empty_while_probe_still_pending(self, kiro_state_dir, disable_env_vars, monkeypatch):
        monkeypatch.setattr(adapter, "start_kiro_login_probe", lambda dest_path: None)
        sm = self._make_state(kiro_state_dir, "pending")
        adapter.ensure_session_initialized(sm, {"session_id": "sess-2"})
        assert adapter.resolve_user_login_id(sm) == ""
        # Still uncached — a later call should try reading the probe again.
        assert sm.get("user_login_id") is None

    def test_resolves_once_probe_file_appears(self, kiro_state_dir, disable_env_vars, monkeypatch):
        monkeypatch.setattr(adapter, "start_kiro_login_probe", lambda dest_path: None)
        sm = self._make_state(kiro_state_dir, "resolves")
        adapter.ensure_session_initialized(sm, {"session_id": "sess-3"})
        probe_path = sm.get("user_login_id_probe_path")
        assert probe_path is not None

        # First call before the probe file exists.
        assert adapter.resolve_user_login_id(sm) == ""

        # Background process "finishes".
        from pathlib import Path

        Path(probe_path).write_text("Logged in with Google\nEmail: alice@example.com\n")

        assert adapter.resolve_user_login_id(sm) == "alice@example.com"
        # Cached — a second call must not need the file anymore.
        Path(probe_path).unlink()
        assert adapter.resolve_user_login_id(sm) == "alice@example.com"

    def test_gives_up_after_grace_window_and_caches_empty(self, kiro_state_dir, disable_env_vars, monkeypatch):
        monkeypatch.setattr(adapter, "start_kiro_login_probe", lambda dest_path: None)
        sm = self._make_state(kiro_state_dir, "gives-up")
        adapter.ensure_session_initialized(sm, {"session_id": "sess-4"})
        # Backdate the probe's start time past the grace window so the very
        # next call gives up, without a real sleep in the test.
        past = adapter.get_timestamp_ms() - adapter._LOGIN_PROBE_GRACE_MS - 1000
        sm.set("user_login_id_probe_started_at", str(past))

        assert adapter.resolve_user_login_id(sm) == ""
        assert sm.get("user_login_id") == ""  # now permanently cached, not just returned

    def test_no_probe_started_returns_empty(self, kiro_state_dir, disable_env_vars):
        """Defensive: resolve_user_login_id on a session that never ran
        ensure_session_initialized (or predates this feature) must not raise."""
        sm = self._make_state(kiro_state_dir, "no-probe")
        assert adapter.resolve_user_login_id(sm) == ""


# ── gc_stale_state_files tests ──────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_removes_files_older_than_24h(self, kiro_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = kiro_state_dir / "state_old.json"
        state_file.write_text("{}")
        old_time = time.time() - 90000  # 25 hours
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_keeps_recent_files(self, kiro_state_dir, disable_env_vars):
        """State file younger than 24h is kept."""
        state_file = kiro_state_dir / "state_new.json"
        state_file.write_text("{}")
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_removes_matching_lock(self, kiro_state_dir, disable_env_vars):
        """Lock file is removed when corresponding state file is removed."""
        state_file = kiro_state_dir / "state_old.json"
        state_file.write_text("{}")
        lock_file = kiro_state_dir / ".lock_old"
        lock_file.write_text("")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_nonexistent_dir_no_error(self, tmp_harness_dir, monkeypatch):
        """Non-existent STATE_DIR causes no errors."""
        state_dir = tmp_harness_dir / "state" / "kiro-nonexistent"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        adapter.gc_stale_state_files()  # should not raise


# ── load_session_sidecar tests ──────────────────────────────────────────────


class TestLoadSessionSidecar:
    def test_returns_none_for_empty_id(self, sidecar_dir):
        """Empty session_id returns None."""
        assert adapter.load_session_sidecar("") is None

    def test_returns_none_when_file_missing(self, sidecar_dir, monkeypatch):
        """Missing sidecar file returns None."""
        monkeypatch.setattr(adapter, "_SIDECAR_RETRY_SECS", 0.0)
        assert adapter.load_session_sidecar("nonexistent-session") is None

    def test_returns_none_for_malformed_json(self, sidecar_dir, monkeypatch):
        """Malformed JSON in sidecar file returns None."""
        monkeypatch.setattr(adapter, "_SIDECAR_RETRY_SECS", 0.0)
        sid = "malformed-session"
        (sidecar_dir / f"{sid}.json").write_text("not json")
        assert adapter.load_session_sidecar(sid) is None

    def test_returns_none_for_non_object_root(self, sidecar_dir):
        """Valid JSON but non-object root (list) returns None."""
        sid = "list-root"
        (sidecar_dir / f"{sid}.json").write_text("[]")
        assert adapter.load_session_sidecar(sid) is None

    def test_loads_complete_sidecar(self, sidecar_dir, sidecar_complete):
        """Successfully loads a complete sidecar file."""
        sid = "complete-session"
        (sidecar_dir / f"{sid}.json").write_text(json.dumps(sidecar_complete))
        result = adapter.load_session_sidecar(sid)
        assert isinstance(result, dict)
        assert "session_state" in result

    def test_returns_immediately_when_metering_present(self, sidecar_dir, sidecar_complete, monkeypatch):
        """A sidecar that already has metering must not cost a single sleep."""
        sleeps: list = []
        monkeypatch.setattr(adapter.time, "sleep", lambda s: sleeps.append(s))

        sid = "early-exit"
        (sidecar_dir / f"{sid}.json").write_text(json.dumps(sidecar_complete))

        assert adapter.load_session_sidecar(sid) is not None
        assert sleeps == []

    def test_retries_until_metering_arrives(self, sidecar_dir, sidecar_complete, monkeypatch):
        """Kiro flushes metering after the stop hook fires — poll until it lands."""
        no_metering = copy.deepcopy(sidecar_complete)
        del no_metering["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]["metering_usage"]

        sid = "late-metering"
        path = sidecar_dir / f"{sid}.json"
        path.write_text(json.dumps(no_metering))

        sleeps: list = []
        # Rewrite the file with the flushed version on the 3rd read, the way
        # Kiro would have written it while we were polling.
        reads = [0]
        real_read_text = type(path).read_text

        def _counting_read(self, *a, **kw):
            reads[0] += 1
            if reads[0] >= 3:
                return json.dumps(sidecar_complete)
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(type(path), "read_text", _counting_read)
        monkeypatch.setattr(adapter.time, "sleep", lambda s: sleeps.append(s))

        result = adapter.load_session_sidecar(sid)

        assert result is not None
        turns = result["session_state"]["conversation_metadata"]["user_turn_metadatas"]
        assert turns[-1].get("metering_usage")
        assert len(sleeps) == 2  # two polls before the flush landed

    def test_returns_partial_sidecar_when_metering_never_arrives(self, sidecar_dir, sidecar_complete, monkeypatch):
        """Timing out must still yield the model name and token counts we did read."""
        no_metering = copy.deepcopy(sidecar_complete)
        del no_metering["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]["metering_usage"]

        sid = "no-metering"
        (sidecar_dir / f"{sid}.json").write_text(json.dumps(no_metering))
        monkeypatch.setattr(adapter, "_SIDECAR_RETRY_SECS", 0.0)

        result = adapter.load_session_sidecar(sid)

        assert isinstance(result, dict)
        assert "session_state" in result

    def test_malformed_read_after_a_good_one_keeps_the_good_one(self, sidecar_dir, sidecar_complete, monkeypatch):
        """A truncated re-read mid-flush must not throw away what we already parsed."""
        no_metering = copy.deepcopy(sidecar_complete)
        del no_metering["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]["metering_usage"]

        sid = "torn-write"
        path = sidecar_dir / f"{sid}.json"
        path.write_text(json.dumps(no_metering))

        reads = [0]

        def _read(self, *a, **kw):
            reads[0] += 1
            return json.dumps(no_metering) if reads[0] == 1 else "{ truncated"

        # Deterministic clock so the loop ends after a known number of polls
        # instead of depending on how fast the machine runs.
        ticks = [0.0]

        def _monotonic():
            ticks[0] += 0.1
            return ticks[0]

        monkeypatch.setattr(type(path), "read_text", _read)
        monkeypatch.setattr(adapter.time, "sleep", lambda s: None)
        monkeypatch.setattr(adapter.time, "monotonic", _monotonic)
        monkeypatch.setattr(adapter, "_SIDECAR_RETRY_SECS", 0.25)

        result = adapter.load_session_sidecar(sid)

        assert isinstance(result, dict)
        assert "session_state" in result


# ── extract_sidecar_attrs tests ─────────────────────────────────────────────


class TestExtractSidecarAttrs:
    def test_returns_empty_for_none(self):
        """None input returns empty dict."""
        assert adapter.extract_sidecar_attrs(None) == {}

    def test_returns_empty_for_missing_session_state(self):
        """Input without session_state returns empty dict."""
        assert adapter.extract_sidecar_attrs({}) == {}

    def test_full_sidecar_yields_all_attrs(self, sidecar_complete):
        """Complete sidecar yields all expected span attributes."""
        attrs = adapter.extract_sidecar_attrs(sidecar_complete)

        assert attrs["llm.model_name"] == "claude-sonnet-4"
        assert attrs["kiro.agent_name"] == "atatus-traced"
        assert attrs["llm.token_count.prompt"] == 1234
        assert attrs["llm.token_count.completion"] == 567
        assert attrs["llm.token_count.total"] == 1801
        assert abs(attrs["kiro.cost.credits"] - 0.103) < 1e-9
        assert attrs["kiro.context_usage_percentage"] == 4.7

        # kiro.metering_usage is a JSON string of a 2-element list
        metering = json.loads(attrs["kiro.metering_usage"])
        assert isinstance(metering, list)
        assert len(metering) == 2

        # turn duration: 10 secs + 151949125 nanos = 10151 ms
        assert attrs["kiro.turn_duration_ms"] == 10151

    def test_zero_token_counts_omitted(self, sidecar_complete):
        """Zero token counts are treated as unknown and omitted."""
        sidecar = copy.deepcopy(sidecar_complete)
        turn = sidecar["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
        turn["input_token_count"] = 0
        turn["output_token_count"] = 0

        attrs = adapter.extract_sidecar_attrs(sidecar)

        assert "llm.token_count.prompt" not in attrs
        assert "llm.token_count.completion" not in attrs
        assert "llm.token_count.total" not in attrs

    def test_no_turns_yields_session_level_only(self, sidecar_no_turns):
        """Sidecar with no turns yields session-level attrs only."""
        attrs = adapter.extract_sidecar_attrs(sidecar_no_turns)

        assert attrs["llm.model_name"] == "auto"
        assert attrs["kiro.agent_name"] == "atatus-traced"

        # No turn-level attributes
        assert "llm.token_count.prompt" not in attrs
        assert "llm.token_count.completion" not in attrs
        assert "llm.token_count.total" not in attrs
        assert "kiro.cost.credits" not in attrs
        assert "kiro.metering_usage" not in attrs
        assert "kiro.turn_duration_ms" not in attrs

    def test_zero_cost_credits_omitted(self, sidecar_complete):
        """Zero metering values → kiro.cost.credits omitted but raw JSON still attached."""
        sidecar = copy.deepcopy(sidecar_complete)
        turn = sidecar["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
        for entry in turn["metering_usage"]:
            entry["value"] = 0

        attrs = adapter.extract_sidecar_attrs(sidecar)

        assert "kiro.cost.credits" not in attrs
        # Raw metering_usage JSON is still attached (non-empty list)
        assert "kiro.metering_usage" in attrs
