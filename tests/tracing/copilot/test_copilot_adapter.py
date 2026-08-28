#!/usr/bin/env python3
"""Tests for tracing.copilot.hooks.adapter — single-mode session resolution, init, GC, requirements."""

import json
import os
import subprocess
from unittest.mock import mock_open, patch

import pytest

from core.common import StateManager
from tracing.copilot.hooks import adapter

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def copilot_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "copilot"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("ATATUS_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ATATUS_USER_ID", raising=False)
    monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")


# ── payload_get tests ────────────────────────────────────────────────────────


class TestPayloadGet:
    """Copilot emits camelCase natively and snake_case in editor-compat mode.

    Reading only one spelling silently yields empty fields for half the installs,
    so every field read must accept both.
    """

    @pytest.mark.parametrize(
        "name,key",
        [
            ("session_id", "session_id"),
            ("session_id", "sessionId"),
            ("transcript_path", "transcript_path"),
            ("transcript_path", "transcriptPath"),
            ("tool_name", "tool_name"),
            ("tool_name", "toolName"),
            ("text_result_for_llm", "text_result_for_llm"),
            ("text_result_for_llm", "textResultForLlm"),
        ],
    )
    def test_both_spellings_read(self, name, key):
        assert adapter.payload_get({key: "value"}, name) == "value"

    def test_first_named_field_wins(self):
        """Names are tried in order, so the caller's preferred field wins."""
        payload = {"tool_args": {"a": 1}, "tool_input": {"b": 2}}
        assert adapter.payload_get(payload, "tool_args", "tool_input") == {"a": 1}

    def test_falls_through_to_later_name(self):
        payload = {"toolInput": {"b": 2}}
        assert adapter.payload_get(payload, "tool_args", "tool_input") == {"b": 2}

    def test_empty_value_is_skipped(self):
        """An empty string is "not provided", so the next spelling gets a turn."""
        assert adapter.payload_get({"tool_name": "", "toolName": "bash"}, "tool_name") == "bash"

    def test_default_returned_when_absent(self):
        assert adapter.payload_get({"other": 1}, "tool_name", default="unknown") == "unknown"

    def test_non_dict_payload_returns_default(self):
        assert adapter.payload_get(None, "tool_name", default="unknown") == "unknown"

    def test_single_word_name_is_unchanged_by_camel_casing(self):
        assert adapter.payload_get({"prompt": "hi"}, "prompt") == "hi"

    def test_falsy_but_present_values_survive(self):
        """Zero and False are real values, unlike "" — they must not be skipped."""
        assert adapter.payload_get({"outputTokens": 0}, "output_tokens", default=-1) == 0
        assert adapter.payload_get({"enabled": False}, "enabled", default=None) is False


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_snake_case_session_id_used_as_key(self, copilot_state_dir, disable_env_vars):
        """session_id from payload used as state file key."""
        sm = adapter.resolve_session({"session_id": "sess-42", "hook_event_name": "SessionStart"})
        assert sm.state_file == copilot_state_dir / "state_sess-42.json"
        assert sm.state_file.exists()

    def test_fallback_to_pid_when_no_session_id(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """Falls back to PID-based key when session_id absent."""
        monkeypatch.setattr(adapter, "_get_grandparent_pid", lambda: "54321")
        sm = adapter.resolve_session({"cwd": "/tmp/project"})
        assert sm.state_file == copilot_state_dir / "state_54321.json"

    def test_fallback_to_pid_when_session_id_empty(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """Falls back to PID-based key when session_id is empty string."""
        monkeypatch.setattr(adapter, "_get_grandparent_pid", lambda: "11111")
        sm = adapter.resolve_session({"session_id": "", "cwd": "/tmp/project"})
        assert sm.state_file == copilot_state_dir / "state_11111.json"

    def test_init_state_called(self, copilot_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists with {})."""
        sm = adapter.resolve_session({"session_id": "test-init", "hook_event_name": "SessionStart"})
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_same_input_same_file(self, copilot_state_dir, disable_env_vars):
        """Calling resolve_session twice with same input returns same file path."""
        inp = {"session_id": "stable", "hook_event_name": "Stop"}
        sm1 = adapter.resolve_session(inp)
        sm2 = adapter.resolve_session(inp)
        assert sm1.state_file == sm2.state_file

    def test_uuid_session_id(self, copilot_state_dir, disable_env_vars):
        """UUID-style session_id (as seen in real payloads) works correctly."""
        uuid = "d4870649-2f69-472d-96a2-599e55ab13f0"
        sm = adapter.resolve_session({"session_id": uuid, "hook_event_name": "SessionStart"})
        assert sm.state_file == copilot_state_dir / f"state_{uuid}.json"
        assert sm.state_file.exists()

    def test_windows_fallback_uses_ppid(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """On Windows, falls back to os.getppid() when no session_id."""
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(os, "getppid", lambda: 12345)
        sm = adapter.resolve_session({"cwd": "/tmp/project"})
        assert sm.state_file == copilot_state_dir / "state_12345.json"

    def test_camel_case_session_id_used_as_key(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """camelCase sessionId is Copilot's native spelling and must be honoured.

        Ignoring it sent every native-mode session to the PID fallback, so two
        sessions under the same Copilot process shared one state file.
        """
        monkeypatch.setattr(adapter, "_get_grandparent_pid", lambda: "77777")
        sm = adapter.resolve_session({"sessionId": "camel-case-id", "hookEventName": "SessionStart"})
        assert sm.state_file == copilot_state_dir / "state_camel-case-id.json"
        assert sm.state_file.exists()


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, copilot_state_dir, key="test"):
        sm = StateManager(
            state_dir=copilot_state_dir,
            state_file=copilot_state_dir / f"state_{key}.json",
            lock_path=copilot_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, copilot_state_dir, disable_env_vars):
        """First call sets all expected keys."""
        sm = self._make_state(copilot_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-1"})
        assert sm.get("session_id") == "sid-1"
        assert sm.get("session_start_time") is not None
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"
        assert sm.get("user_id") is not None

    def test_idempotent(self, copilot_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(copilot_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-2"})
        start_time = sm.get("session_start_time")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-different"})
        assert sm.get("session_id") == "sid-2"
        assert sm.get("session_start_time") == start_time

    def test_session_id_from_snake_case_payload(self, copilot_state_dir, disable_env_vars):
        """session_id from snake_case payload is used as session_id."""
        sm = self._make_state(copilot_state_dir, "from-payload")
        adapter.ensure_session_initialized(sm, {"session_id": "payload-session"})
        assert sm.get("session_id") == "payload-session"

    def test_session_id_generated_when_missing(self, copilot_state_dir, disable_env_vars):
        """session_id is generated (32-hex) when not in input."""
        sm = self._make_state(copilot_state_dir, "generated")
        adapter.ensure_session_initialized(sm, {"cwd": "/tmp/project"})
        sid = sm.get("session_id")
        assert sid is not None
        assert len(sid) == 32
        int(sid, 16)  # should not raise

    def test_session_id_generated_when_empty(self, copilot_state_dir, disable_env_vars):
        """session_id is generated when payload has empty string session_id."""
        sm = self._make_state(copilot_state_dir, "empty-sid")
        adapter.ensure_session_initialized(sm, {"session_id": ""})
        sid = sm.get("session_id")
        assert sid is not None
        assert len(sid) == 32
        int(sid, 16)  # should not raise

    def test_camel_case_session_id_used(self, copilot_state_dir, disable_env_vars):
        """camelCase sessionId is honoured, not replaced by a generated id.

        Copilot emits camelCase natively; generating an id instead meant the
        session's spans carried an id nothing else in the transcript shared.
        """
        sm = self._make_state(copilot_state_dir, "camel-honoured")
        adapter.ensure_session_initialized(sm, {"sessionId": "camel-case-id"})
        assert sm.get("session_id") == "camel-case-id"

    def test_counters_start_at_zero(self, copilot_state_dir, disable_env_vars):
        """trace_count and tool_count start at '0'."""
        sm = self._make_state(copilot_state_dir, "counters")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"

    def test_user_id_from_env(self, copilot_state_dir, monkeypatch):
        """user_id is taken from env.user_id."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        monkeypatch.setenv("ATATUS_USER_ID", "env-user-42")
        monkeypatch.delenv("ATATUS_PROJECT_NAME", raising=False)
        sm = self._make_state(copilot_state_dir, "user-env")
        adapter.ensure_session_initialized(sm, {"session_id": "s1"})
        assert sm.get("user_id") == "env-user-42"

    def test_full_payload_schema(self, copilot_state_dir, disable_env_vars):
        """Full verified payload schema (snake_case) works end-to-end."""
        sm = self._make_state(copilot_state_dir, "full-schema")
        payload = {
            "cwd": "/tmp/test-project",
            "hook_event_name": "SessionStart",
            "session_id": "d4870649-2f69-472d-96a2-599e55ab13f0",
            "timestamp": "2026-05-04T23:25:33.735Z",
            "initial_prompt": "fix the bug",
            "source": "new",
        }
        adapter.ensure_session_initialized(sm, payload)
        assert sm.get("session_id") == "d4870649-2f69-472d-96a2-599e55ab13f0"


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_dead_pid_removed(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """state file for a dead PID is removed."""
        dead_pid = 99999
        state_file = copilot_state_dir / f"state_{dead_pid}.json"
        state_file.write_text("{}")
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_live_pid_kept(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """state file for a live PID is kept."""
        live_pid = os.getpid()
        state_file = copilot_state_dir / f"state_{live_pid}.json"
        state_file.write_text("{}")
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: pid == live_pid)
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_non_numeric_key_kept(self, copilot_state_dir, disable_env_vars):
        """state file with non-numeric key (UUID sessionId) is never GC'd."""
        state_file = copilot_state_dir / "state_d4870649-2f69-472d-96a2-599e55ab13f0.json"
        state_file.write_text("{}")
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """Lock dir is removed when state file is removed."""
        dead_pid = 99998
        state_file = copilot_state_dir / f"state_{dead_pid}.json"
        state_file.write_text("{}")
        lock_dir = copilot_state_dir / f".lock_{dead_pid}"
        lock_dir.mkdir()
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_lock_file_removed(self, copilot_state_dir, disable_env_vars, monkeypatch):
        """Lock file (fcntl-style) is removed when state file is removed."""
        dead_pid = 99997
        state_file = copilot_state_dir / f"state_{dead_pid}.json"
        state_file.write_text("{}")
        lock_file = copilot_state_dir / f".lock_{dead_pid}"
        lock_file.write_text("")  # fcntl creates lock as a regular file
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_empty_dir_no_error(self, copilot_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in copilot_state_dir.glob("state_*.json"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "copilot-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "copilot-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── _get_grandparent_pid tests ───────────────────────────────────────────────


class TestGetGrandparentPid:
    def test_reads_from_proc_stat(self, monkeypatch):
        """Linux path: reads grandparent PID from /proc/{ppid}/stat."""
        monkeypatch.setattr(os, "getppid", lambda: 100)
        fake_stat = "100 (python) S 456 0 0 0"
        with patch("builtins.open", mock_open(read_data=fake_stat)):
            result = adapter._get_grandparent_pid()
        assert result == "456"

    def test_falls_back_to_ps_command(self, monkeypatch):
        """When /proc read fails, falls back to ps command."""
        monkeypatch.setattr(os, "getppid", lambda: 100)
        with patch("builtins.open", side_effect=OSError("no /proc")):
            with patch("subprocess.check_output", return_value=b"  789  \n"):
                result = adapter._get_grandparent_pid()
        assert result == "789"

    def test_falls_back_to_ppid(self, monkeypatch):
        """When both /proc and ps fail, falls back to parent PID."""
        monkeypatch.setattr(os, "getppid", lambda: 42)
        with patch("builtins.open", side_effect=OSError("no /proc")):
            with patch(
                "subprocess.check_output",
                side_effect=subprocess.SubprocessError("ps failed"),
            ):
                result = adapter._get_grandparent_pid()
        assert result == "42"

    def test_ppid_zero_returns_own_pid(self, monkeypatch):
        """When ppid is 0, returns current process PID."""
        monkeypatch.setattr(os, "getppid", lambda: 0)
        result = adapter._get_grandparent_pid()
        assert result == str(os.getpid())


# ── _is_pid_alive tests ─────────────────────────────────────────────────────


class TestIsPidAlive:
    def test_own_pid_is_alive(self):
        """Current process PID should be alive."""
        assert adapter._is_pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        """A very high PID that doesn't exist should be dead."""
        assert adapter._is_pid_alive(99999) is False

    def test_zero_returns_false(self):
        """PID 0 should return False."""
        assert adapter._is_pid_alive(0) is False

    def test_negative_returns_false(self):
        """Negative PID should return False."""
        assert adapter._is_pid_alive(-1) is False
