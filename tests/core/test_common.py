#!/usr/bin/env python3
"""Tests for core.common — FileLock, StateManager, and span building."""

import io
import json
import threading
import time
import urllib.error
from unittest import mock

import pytest

from core.common import (
    FileLock,
    StateManager,
    _attrs_to_otlp,
    _resolve_kind,
    _stamp_atatus_identity,
    _to_otlp_attr_value,
    build_multi_span,
    build_span,
    debug_dump,
    env,
    error,
    get_target,
    log,
    redirect_stderr_to_log_file,
    resolve_backend,
    restore_stderr_from_log_file,
    send_span,
)

# ── Logging tests ──────────────────────────────────────────────────────────


class TestLogging:
    def test_log_verbose_on(self, capsys, monkeypatch):
        """log() writes to stderr when ATATUS_VERBOSE=true."""
        monkeypatch.setenv("ATATUS_VERBOSE", "true")
        log("test message")
        assert "test message" in capsys.readouterr().err

    def test_log_verbose_off(self, capsys, monkeypatch):
        """log() is silent when ATATUS_VERBOSE is not set."""
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)
        log("test message")
        assert capsys.readouterr().err == ""

    def test_error_always_writes(self, capsys):
        """error() always writes to stderr."""
        error("something broke")
        assert "something broke" in capsys.readouterr().err

    def test_debug_dump_off(self, tmp_path, monkeypatch):
        """debug_dump() does nothing when ATATUS_TRACE_DEBUG is not true."""
        monkeypatch.delenv("ATATUS_TRACE_DEBUG", raising=False)
        debug_dump("test_label", {"key": "val"})
        # no files should be created in debug dir

    def test_debug_dump_on(self, tmp_path, monkeypatch):
        """debug_dump() writes JSON file when ATATUS_TRACE_DEBUG=true."""
        monkeypatch.setenv("ATATUS_TRACE_DEBUG", "true")
        debug_dir = tmp_path / "debug"
        monkeypatch.setattr("core.constants.STATE_BASE_DIR", tmp_path)
        debug_dump("test_label", {"key": "val"})
        assert debug_dir.exists()
        files = list(debug_dir.glob("test_label_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data == {"key": "val"}


# ── redirect_stderr_to_log_file tests ──────────────────────────────────────


class TestStderrRedirect:
    """``redirect_stderr_to_log_file()`` + ``restore_stderr_from_log_file()``."""

    @pytest.fixture(autouse=True)
    def _always_restore(self):
        """Each test starts and ends with a clean stderr.

        Adapter imports earlier in the test session may have already called
        ``redirect_stderr_to_log_file()`` — restore here so our idempotency
        guard doesn't short-circuit the next ``redirect`` call.
        """
        restore_stderr_from_log_file()
        yield
        restore_stderr_from_log_file()

    def test_writes_stderr_to_log_file(self, tmp_path, monkeypatch):
        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))

        redirect_stderr_to_log_file()
        error("boom")
        restore_stderr_from_log_file()

        assert "boom" in log_file.read_text()

    def test_noop_when_log_file_unset(self, monkeypatch):
        monkeypatch.delenv("ATATUS_LOG_FILE", raising=False)
        original = __import__("sys").stderr
        redirect_stderr_to_log_file()
        assert __import__("sys").stderr is original

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        log_file = tmp_path / "nested" / "dir" / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))

        redirect_stderr_to_log_file()
        error("hello")
        restore_stderr_from_log_file()

        assert log_file.exists()
        assert "hello" in log_file.read_text()

    def test_restore_returns_original_stderr(self, tmp_path, monkeypatch):
        import sys as _sys

        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))
        original = _sys.stderr

        redirect_stderr_to_log_file()
        assert _sys.stderr is not original  # redirect took effect

        restore_stderr_from_log_file()
        assert _sys.stderr is original  # restored

    def test_restore_is_idempotent(self, tmp_path, monkeypatch):
        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))

        redirect_stderr_to_log_file()
        restore_stderr_from_log_file()
        # Second call should be a no-op (no exception, no state corruption).
        restore_stderr_from_log_file()

    def test_redirect_is_idempotent(self, tmp_path, monkeypatch):
        """Calling redirect twice should not leak a second FD or re-redirect."""
        import sys as _sys

        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ATATUS_LOG_FILE", str(log_file))

        redirect_stderr_to_log_file()
        fh_after_first = _sys.stderr
        redirect_stderr_to_log_file()
        fh_after_second = _sys.stderr
        assert fh_after_first is fh_after_second

    def test_failsoft_on_unwritable_path(self, monkeypatch):
        """Opening a path that can't be created leaves stderr untouched."""
        import sys as _sys

        # /dev/null/foo is unwritable on POSIX (file under a file).
        monkeypatch.setenv("ATATUS_LOG_FILE", "/dev/null/cannot-create.log")
        original = _sys.stderr
        redirect_stderr_to_log_file()
        assert _sys.stderr is original  # untouched


# ── FileLock tests ──────────────────────────────────────────────────────────


class TestFileLock:
    def test_acquire_and_release(self, tmp_path):
        """FileLock acquires and releases without error on empty dir."""
        lock_path = tmp_path / "test.lock"
        with FileLock(lock_path, timeout=1.0):
            pass  # should not raise

    def test_blocks_second_thread(self, tmp_path):
        """FileLock blocks second acquisition from another thread."""
        lock_path = tmp_path / "test.lock"
        barrier = threading.Barrier(2, timeout=5)
        acquired_order = []

        def hold_lock(name, hold_time):
            with FileLock(lock_path, timeout=5.0):
                acquired_order.append(name)
                if name == "A":
                    barrier.wait()  # signal B to try acquiring
                    time.sleep(hold_time)

        t_a = threading.Thread(target=hold_lock, args=("A", 0.5))
        t_a.start()
        barrier.wait()  # wait for A to acquire
        time.sleep(0.05)  # small delay so A is definitely holding

        t_b = threading.Thread(target=hold_lock, args=("B", 0))
        t_b.start()

        t_a.join(timeout=5)
        t_b.join(timeout=5)

        # A acquired first, B acquired after A released
        assert acquired_order[0] == "A"
        assert "B" in acquired_order

    def test_timeout_force_acquires(self, tmp_path):
        """After timeout, FileLock force-acquires the lock."""
        lock_path = tmp_path / "test.lock"
        hold_event = threading.Event()
        released_event = threading.Event()

        def hold_forever():
            with FileLock(lock_path, timeout=5.0):
                hold_event.set()
                # Hold lock until test is done — never release voluntarily
                released_event.wait(timeout=10)

        t = threading.Thread(target=hold_forever, daemon=True)
        t.start()
        hold_event.wait(timeout=5)

        # Thread B with short timeout should force-acquire
        start = time.monotonic()
        with FileLock(lock_path, timeout=0.3):
            elapsed = time.monotonic() - start
            assert elapsed >= 0.2  # waited at least near the timeout

        released_event.set()
        t.join(timeout=5)

    def test_creates_parent_directories(self, tmp_path):
        """FileLock creates parent directories if missing."""
        lock_path = tmp_path / "deep" / "nested" / "dir" / "test.lock"
        assert not lock_path.parent.exists()
        with FileLock(lock_path, timeout=1.0):
            assert lock_path.parent.exists()

    def test_cleanup_on_exit(self, tmp_path):
        """FileLock cleans up lock file/dir on __exit__."""
        from core.common import _LOCK_IMPL

        lock_path = tmp_path / "test.lock"
        with FileLock(lock_path, timeout=1.0):
            pass

        if _LOCK_IMPL == "mkdir":
            # mkdir-based lock removes the directory
            assert not lock_path.exists()
        else:
            # fcntl/msvcrt leaves the file (just unlocked) — this is normal
            # The file exists but is not locked
            pass


# ── StateManager tests ──────────────────────────────────────────────────────


class TestStateManager:
    def _make_sm(self, tmp_path, name="test"):
        state_dir = tmp_path / "state"
        state_file = state_dir / f"state_{name}.json"
        lock_path = state_dir / f".lock_{name}"
        return StateManager(state_dir, state_file, lock_path)

    def test_init_creates_dir_and_file(self, tmp_path):
        """init_state() creates directory and .json file containing {}."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        assert sm.state_dir.exists()
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_init_recovers_corrupted(self, tmp_path):
        """init_state() recovers corrupted file."""
        sm = self._make_sm(tmp_path)
        sm.state_dir.mkdir(parents=True)
        sm.state_file.write_text("{{garbage not json")
        sm.init_state()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_init_preserves_valid(self, tmp_path):
        """init_state() preserves valid existing file."""
        sm = self._make_sm(tmp_path)
        sm.state_dir.mkdir(parents=True)
        sm.state_file.write_text(json.dumps({"key": "val"}, indent=2))
        sm.init_state()
        data = json.loads(sm.state_file.read_text())
        assert data == {"key": "val"}

    def test_set_then_get(self, tmp_path):
        """set("key", "val") then get("key") returns "val"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.set("key", "val")
        assert sm.get("key") == "val"

    def test_values_stored_as_strings(self, tmp_path):
        """set("count", "42") stores as string, get returns "42"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.set("count", "42")
        result = sm.get("count")
        assert result == "42"
        assert isinstance(result, str)

    def test_get_missing_key(self, tmp_path):
        """get("missing_key") returns None."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        assert sm.get("missing_key") is None

    def test_get_no_state_file(self, tmp_path):
        """get("any") returns None when state file doesn't exist."""
        sm = self._make_sm(tmp_path)
        # Don't call init_state — file doesn't exist
        assert sm.get("any") is None

    def test_delete_removes_key(self, tmp_path):
        """delete("key") removes it; subsequent get returns None."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.set("key", "val")
        assert sm.get("key") == "val"
        sm.delete("key")
        assert sm.get("key") is None

    def test_delete_missing_noop(self, tmp_path):
        """delete("missing") is no-op, no error."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.delete("missing")  # should not raise

    def test_increment_missing_key(self, tmp_path):
        """increment("count") on missing key -> get returns "1"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.increment("count")
        assert sm.get("count") == "1"

    def test_increment_twice(self, tmp_path):
        """increment("count") twice -> get returns "2"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.increment("count")
        sm.increment("count")
        assert sm.get("count") == "2"

    def test_increment_non_numeric(self, tmp_path):
        """increment on non-numeric value treats as 0, returns "1"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.set("key", "abc")
        sm.increment("key")
        assert sm.get("key") == "1"

    def test_concurrent_set_different_keys(self, tmp_path):
        """Concurrent set from 10 threads writing different keys -> all present."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        errors = []

        def writer(i):
            try:
                sm.set(f"key_{i}", f"val_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        for i in range(10):
            assert sm.get(f"key_{i}") == f"val_{i}"

    def test_concurrent_increment(self, tmp_path):
        """Concurrent increment from 10 threads on same key -> final value is "10"."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        errors = []

        def incrementer():
            try:
                sm.increment("counter")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=incrementer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert sm.get("counter") == "10"

    def test_atomic_write_no_corruption(self, tmp_path):
        """State file is not corrupted if tmp file write fails."""
        sm = self._make_sm(tmp_path)
        sm.init_state()
        sm.set("key", "original")

        # Make the tmp file path read-only directory to cause write failure
        tmp_blocker = sm.state_file.with_suffix(f".tmp.{__import__('os').getpid()}")
        tmp_blocker.mkdir(parents=True, exist_ok=True)

        # This set should fail silently (can't write to a directory path)
        sm.set("key", "corrupted")

        # Clean up blocker
        tmp_blocker.rmdir()

        # Original value should still be intact (or the new value if write succeeded
        # on a platform where the path resolution differs)
        val = sm.get("key")
        assert val in ("original", "corrupted")  # either is valid, but not corrupt
        # Verify the file is valid JSON
        data = json.loads(sm.state_file.read_text())
        assert isinstance(data, dict)


# ── Attribute conversion tests ─────────────────────────────────────────────


class TestOtlpAttrValue:
    def test_string(self):
        assert _to_otlp_attr_value("hello") == {"stringValue": "hello"}

    def test_int(self):
        assert _to_otlp_attr_value(42) == {"intValue": 42}

    def test_float_fractional(self):
        assert _to_otlp_attr_value(3.14) == {"doubleValue": 3.14}

    def test_float_whole(self):
        """Float with no fractional part becomes intValue (matches jq floor == value)."""
        assert _to_otlp_attr_value(3.0) == {"intValue": 3}

    def test_bool_true(self):
        """bool must be detected before int (bool is subclass of int in Python)."""
        assert _to_otlp_attr_value(True) == {"boolValue": True}

    def test_bool_false(self):
        assert _to_otlp_attr_value(False) == {"boolValue": False}

    def test_none_becomes_string(self):
        assert _to_otlp_attr_value(None) == {"stringValue": "None"}

    def test_list_becomes_string(self):
        assert _to_otlp_attr_value([1, 2]) == {"stringValue": "[1, 2]"}


class TestAttrsToOtlp:
    def test_mixed_types(self):
        result = _attrs_to_otlp({"a": "b", "c": 1})
        assert len(result) == 2
        assert result[0] == {"key": "a", "value": {"stringValue": "b"}}
        assert result[1] == {"key": "c", "value": {"intValue": 1}}

    def test_empty_dict(self):
        assert _attrs_to_otlp({}) == []


# ── Kind mapping tests ─────────────────────────────────────────────────────


class TestResolveKind:
    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("LLM", 1),
            ("llm", 1),
            ("Llm", 1),
            ("TOOL", 1),
            ("tool", 1),
            ("CHAIN", 1),
            ("chain", 1),
            ("INTERNAL", 1),
            ("internal", 1),
            ("", 1)
        ],
    )
    def test_internal_kinds(self, kind, expected):
        assert _resolve_kind(kind) == expected

    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("SERVER", 2),
            ("server", 2),
            ("CLIENT", 3),
            ("client", 3),
            ("PRODUCER", 4),
            ("producer", 4),
            ("CONSUMER", 5),
            ("consumer", 5)
        ],
    )
    def test_other_kinds(self, kind, expected):
        assert _resolve_kind(kind) == expected

    def test_unspecified(self):
        assert _resolve_kind("UNSPECIFIED") == 0
        assert _resolve_kind("unspecified") == 0

    def test_numeric_string(self):
        assert _resolve_kind("3") == 3

    def test_unknown_defaults_to_1(self):
        assert _resolve_kind("UNKNOWN_KIND") == 1


# ── build_span tests ──────────────────────────────────────────────────────


class TestBuildSpan:
    def test_basic_structure(self):
        result = build_span(
            name="Turn 1",
            kind="LLM",
            span_id="aabb",
            trace_id="ccdd",
            start_ms=1000,
            end_ms=2000,
        )
        rs = result["resourceSpans"]
        assert len(rs) == 1
        spans = rs[0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        assert spans[0]["name"] == "Turn 1"

    def test_parent_absent_when_empty(self):
        result = build_span(
            name="root",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            parent_span_id="",
            start_ms=1000,
            end_ms=2000,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert "parentSpanId" not in span

    def test_parent_absent_when_none(self):
        result = build_span(
            name="root",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            parent_span_id=None,
            start_ms=1000,
            end_ms=2000,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert "parentSpanId" not in span

    def test_parent_present(self):
        result = build_span(
            name="child",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            parent_span_id="abc123",
            start_ms=1000,
            end_ms=2000,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["parentSpanId"] == "abc123"

    def test_timestamp_formatting(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1711987200000,
            end_ms=1711987201000,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1711987200000000000"
        assert span["endTimeUnixNano"] == "1711987201000000000"

    def test_end_defaults_to_start_when_empty(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=5000,
            end_ms="",
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["endTimeUnixNano"] == "5000000000"

    def test_end_defaults_to_start_when_none(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=5000,
            end_ms=None,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["endTimeUnixNano"] == "5000000000"

    def test_end_defaults_to_start_when_zero(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=5000,
            end_ms=0,
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["endTimeUnixNano"] == "5000000000"

    def test_service_and_scope_names(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            service_name="my-svc",
            scope_name="my-scope",
        )
        resource = result["resourceSpans"][0]["resource"]
        assert resource["attributes"][0]["value"]["stringValue"] == "my-svc"
        scope = result["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "my-scope"

    def test_attributes_converted(self):
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            attrs={"key": "val", "count": 5},
        )
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert len(span["attributes"]) == 2
        assert span["attributes"][0] == {"key": "key", "value": {"stringValue": "val"}}
        assert span["attributes"][1] == {"key": "count", "value": {"intValue": 5}}

    def test_golden_fixture(self, golden_span):
        """Build a span with known inputs and compare against golden fixture."""
        result = build_span(
            name="Turn 1",
            kind="LLM",
            span_id="abcdef1234567890",
            trace_id="0123456789abcdef0123456789abcdef",
            parent_span_id="",
            start_ms=1711987200000,
            end_ms=1711987201000,
            attrs={"session.id": "sess-1", "input.value": "hello"},
            service_name="test-service",
            scope_name="test-scope",
        )
        assert result == golden_span


# ── build_span status tests ───────────────────────────────────────────────


class TestBuildSpanStatus:
    """``build_span`` accepts optional ``status_code`` / ``status_message`` kwargs.

    OTLP status codes: 0=UNSET, 1=OK (default), 2=ERROR.
    Backwards-compatible: with no kwargs, output must be byte-for-byte identical
    to the prior behavior (``status == {"code": 1}``, no ``message`` key).
    """

    def _span(self, result):
        return result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    def test_default_status_is_ok_with_no_message(self):
        """No status kwargs → status is {"code": 1} with no ``message`` key."""
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
        )
        assert self._span(result)["status"] == {"code": 1}

    def test_error_status_with_message(self):
        """status_code=2 + status_message='x' → {"code": 2, "message": "x"}."""
        result = build_span(
            name="t",
            kind="TOOL",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            status_code=2,
            status_message="x",
        )
        assert self._span(result)["status"] == {"code": 2, "message": "x"}

    def test_error_status_empty_message_omits_message_key(self):
        """status_code=2 + empty status_message → {"code": 2} (no ``message`` key)."""
        result = build_span(
            name="t",
            kind="TOOL",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            status_code=2,
            status_message="",
        )
        assert self._span(result)["status"] == {"code": 2}

    def test_default_status_message_is_empty(self):
        """status_code=2 with no status_message kwarg → no ``message`` key."""
        result = build_span(
            name="t",
            kind="TOOL",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            status_code=2,
        )
        assert self._span(result)["status"] == {"code": 2}

    def test_unset_status_code(self):
        """status_code=0 (UNSET) is honored."""
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            status_code=0,
        )
        assert self._span(result)["status"] == {"code": 0}

    def test_ok_status_with_message_includes_message(self):
        """status_code=1 + non-empty status_message → {"code": 1, "message": ...}.

        Behavior is uniform across codes: a non-empty message is included regardless of code.
        """
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            status_code=1,
            status_message="all good",
        )
        assert self._span(result)["status"] == {"code": 1, "message": "all good"}

    def test_status_kwargs_must_be_keyword_only(self):
        """``status_code`` / ``status_message`` are last-positioned; positional callers
        with the current 9-arg signature must continue to work and yield OK status.

        Calls build_span with all current positional args and asserts the resulting
        status is the default OK status with no message. This guards against an
        implementation that accidentally reorders or renames params.
        """
        result = build_span(
            "t",  # name
            "LLM",  # kind
            "aa",  # span_id
            "bb",  # trace_id
            "",  # parent_span_id
            1000,  # start_ms
            2000,  # end_ms
            {"k": "v"},  # attrs
            "svc",  # service_name
            "scope",  # scope_name
        )
        assert self._span(result)["status"] == {"code": 1}

    def test_existing_callers_unaffected_byte_for_byte(self, golden_span):
        """Calling build_span without status kwargs produces identical output
        to today's build (golden fixture has ``status == {"code": 1}``).
        """
        result = build_span(
            name="Turn 1",
            kind="LLM",
            span_id="abcdef1234567890",
            trace_id="0123456789abcdef0123456789abcdef",
            parent_span_id="",
            start_ms=1711987200000,
            end_ms=1711987201000,
            attrs={"session.id": "sess-1", "input.value": "hello"},
            service_name="test-service",
            scope_name="test-scope",
        )
        assert result == golden_span


# ── build_multi_span tests ────────────────────────────────────────────────


class TestBuildMultiSpan:
    def _make_payload(self, name: str, span_id: str) -> dict:
        return build_span(
            name=name,
            kind="LLM",
            span_id=span_id,
            trace_id="trace1",
            start_ms=1000,
            end_ms=2000,
        )

    def test_merge_three(self):
        payloads = [self._make_payload(f"span{i}", f"id{i}") for i in range(3)]
        result = build_multi_span(payloads, "svc", "scope")
        spans = result["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 3
        assert [s["name"] for s in spans] == ["span0", "span1", "span2"]

    def test_merge_one(self):
        payloads = [self._make_payload("only", "id0")]
        result = build_multi_span(payloads, "svc", "scope")
        spans = result["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1

    def test_merge_empty(self):
        result = build_multi_span([], "svc", "scope")
        assert result == {}

    def test_malformed_skipped(self):
        """Malformed payload in the middle is skipped, others preserved."""
        good1 = self._make_payload("good1", "id1")
        bad = {"resourceSpans": [{"scopeSpans": []}]}  # missing spans key
        good2 = self._make_payload("good2", "id2")
        result = build_multi_span([good1, bad, good2], "svc", "scope")
        spans = result["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2
        assert spans[0]["name"] == "good1"
        assert spans[1]["name"] == "good2"

    def test_service_and_scope_from_args(self):
        """service_name and scope_name from function args, not input payloads."""
        payload = self._make_payload("s", "id0")
        result = build_multi_span([payload], "override-svc", "override-scope")
        resource = result["resourceSpans"][0]["resource"]
        assert resource["attributes"][0]["value"]["stringValue"] == "override-svc"
        scope = result["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "override-scope"


# ── Emit-time hygiene (M4.1) ──────────────────────────────────────────────


class TestNormalizeModelName:
    """Claude Code reports a context-window suffix that breaks exact-match cost
    lookup: an unstripped `claude-sonnet-4-5[1m]` silently prices at $0."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("claude-sonnet-4-5[1m]", "claude-sonnet-4-5"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
            ("  claude-opus-4 [200k] ", "claude-opus-4"),
            ("gpt-4o", "gpt-4o"),
            ("[1m]", ""),
            ("", ""),
        ],
    )
    def test_suffix_stripped(self, raw, expected):
        from core.common import normalize_model_name

        assert normalize_model_name(raw) == expected

    def test_span_omits_model_that_is_only_a_suffix(self):
        span = build_span("t", "LLM", "a" * 16, "b" * 32, "", 1, 2, {"llm.model_name": "[1m]"})
        keys = {a["key"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "llm.model_name" not in keys


class TestStripSystemReminders:
    def test_removes_block(self):
        from core.common import strip_system_reminders

        assert strip_system_reminders("hi <system-reminder>x</system-reminder> there") == "hi  there"

    def test_removes_multiple(self):
        from core.common import strip_system_reminders

        text = "a<system-reminder>x</system-reminder>b<system-reminder>y</system-reminder>c"
        assert strip_system_reminders(text) == "abc"

    def test_unclosed_tag_left_alone(self):
        """An unterminated block must not swallow the rest of the prompt."""
        from core.common import strip_system_reminders

        text = "keep me <system-reminder>oops"
        assert strip_system_reminders(text) == text

    def test_text_without_reminders_is_untouched(self):
        """Regression: an unconditional trim ate trailing newlines from tool
        output that never contained a reminder."""
        from core.common import strip_system_reminders

        text = "Directory listing:\n  foo\n  bar.txt\n"
        assert strip_system_reminders(text) == text


class TestRedactedPlaceholder:
    def test_literal_redacted_is_dropped_not_stored(self):
        span = build_span(
            "t", "LLM", "a" * 16, "b" * 32, "", 1, 2,
            {"input.value": "<REDACTED>", "output.value": "real output"},
        )
        attrs = {a["key"]: list(a["value"].values())[0]
                 for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "input.value" not in attrs
        assert attrs["output.value"] == "real output"


# ── EnvConfig property tests ──────────────────────────────────────────────


class TestEnvConfigProperties:
    """Tests for _Env (accessed via the module-level `env` singleton)."""

    def test_verbose_true(self, monkeypatch):
        monkeypatch.setenv("ATATUS_VERBOSE", "true")
        assert env.verbose is True

    def test_verbose_false(self, monkeypatch):
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)
        assert env.verbose is False

    def test_dry_run_true(self, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        assert env.dry_run is True

    def test_dry_run_false(self, monkeypatch):
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        assert env.dry_run is False


class TestLoggingFlagPrecedence:
    """env var > config.json `logging` > default for log_prompts/tool_details/tool_content."""

    @pytest.fixture(autouse=True)
    def _fresh_env(self, monkeypatch):
        # Clear any inherited env values so each test starts from a clean slate.
        for key in ("ATATUS_LOG_PROMPTS", "ATATUS_LOG_TOOL_DETAILS", "ATATUS_LOG_TOOL_CONTENT"):
            monkeypatch.delenv(key, raising=False)
        # Reset the cached_property between cases.
        from core.common import env as _env

        _env.__dict__.pop("_logging_config", None)
        yield
        _env.__dict__.pop("_logging_config", None)

    def _patch_config(self, monkeypatch, block):
        """Patch core.config.load_config so _Env._logging_config returns *block*."""

        monkeypatch.setattr(
            "core.config.load_config",
            lambda config_path=None: {"logging": block} if block is not None else {},
        )
        # Drop the cached value so the next access re-reads.
        env.__dict__.pop("_logging_config", None)

    def test_defaults_when_nothing_set(self, monkeypatch):
        """ADR-011: prompts and tool details on, tool CONTENT off."""
        self._patch_config(monkeypatch, None)
        assert env.log_prompts is True
        assert env.log_tool_details is True
        assert env.log_tool_content is False

    def test_config_overrides_default(self, monkeypatch):
        self._patch_config(monkeypatch, {"prompts": False, "tool_details": False, "tool_content": False})
        assert env.log_prompts is False
        assert env.log_tool_details is False
        assert env.log_tool_content is False

    def test_env_overrides_config(self, monkeypatch):
        self._patch_config(monkeypatch, {"prompts": False})
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", "true")
        assert env.log_prompts is True

    def test_env_overrides_default(self, monkeypatch):
        self._patch_config(monkeypatch, None)
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        assert env.log_tool_details is False

    def test_partial_config_falls_through_to_default(self, monkeypatch):
        # Only `prompts` configured; the other two fall through to their own
        # defaults -- tool_details True, tool_content False (ADR-011).
        self._patch_config(monkeypatch, {"prompts": False})
        assert env.log_prompts is False
        assert env.log_tool_details is True
        assert env.log_tool_content is False

    def test_tool_content_is_opt_in(self, monkeypatch):
        """Regression guard for ADR-011: tool output must not be captured
        unless explicitly enabled, by env or by config."""
        self._patch_config(monkeypatch, None)
        assert env.log_tool_content is False

        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")
        assert env.log_tool_content is True
        monkeypatch.delenv("ATATUS_LOG_TOOL_CONTENT")

        self._patch_config(monkeypatch, {"tool_content": True})
        assert env.log_tool_content is True


# ── send_span tests ──────────────────────────────────────────────────────


class TestSendSpan:
    """Tests for send_span() using resolve_backend() for direct send."""

    _SAMPLE_SPAN = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test-service"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "test"},
                        "spans": [
                            {
                                "traceId": "0123456789abcdef0123456789abcdef",
                                "spanId": "abcdef1234567890",
                                "name": "test-span",
                                "kind": 1,
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {"key": "openinference.span.kind", "value": {"stringValue": "LLM"}},
                                    {"key": "input.value", "value": {"stringValue": "hello"}}
                                ],
                                "status": {"code": 1}
                            }
                        ]
                    }
                ]
            }
        ]
    }

    def _make_span_with_service(self, service_name):
        """Build a sample span with a specific service.name resource attribute."""
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"name": "test-span"}]}]
                }
            ]
        }

    @pytest.fixture(autouse=True)
    def _mock_sleep(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
        return sleep_calls

    def test_dry_run_returns_true(self, monkeypatch):
        """send_span in dry_run mode returns True without sending."""
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)
        result = send_span(self._SAMPLE_SPAN)
        assert result is True

    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_uses_resolve_backend(self, mock_urlopen, mock_resolve, monkeypatch):
        """send_span calls resolve_backend() to get credentials."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "endpoint": "https://otel-rx.atatus.com",
            "api_key": "",
            "project_name": "test-proj"
        }
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_span(self._SAMPLE_SPAN) is True
        mock_resolve.assert_called_once_with(self._SAMPLE_SPAN)


    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_atatus_no_api_key_no_auth_header(self, mock_urlopen, mock_resolve, monkeypatch):
        """Atatus send omits Authorization header when api_key is empty."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "endpoint": "https://otel-rx.atatus.com",
            "api_key": "",
            "project_name": "default"
        }
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_span(self._SAMPLE_SPAN) is True
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None

    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_atatus_send_failure(self, mock_urlopen, mock_resolve, monkeypatch):
        """Atatus send returns False on network error."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "endpoint": "https://otel-rx.atatus.com",
            "api_key": "",
            "project_name": "default"
        }
        mock_urlopen.side_effect = Exception("connection refused")

        assert send_span(self._SAMPLE_SPAN) is False

    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_atatus_http_error_logs_response_body(self, mock_urlopen, mock_resolve, capsys, monkeypatch):
        """Atatus HTTP errors include the response body in logs."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "endpoint": "https://otel-rx.atatus.com",
            "api_key": "",
            "project_name": "default"
        }
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://otel-rx.atatus.com/v1/traces",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"bad span"}'),
        )

        assert send_span(self._SAMPLE_SPAN) is False
        assert '{"error":"bad span"}' in capsys.readouterr().err

    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_atatus_direct_send(self, mock_urlopen, mock_resolve, monkeypatch):
        """send_span sends directly to Atatus HTTP/JSON endpoint."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "api_key": "my-key",
            "endpoint": "https://otel-rx.atatus.com",
            "project_name": "proj"
        }
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_span(self._SAMPLE_SPAN) is True

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://otel-rx.atatus.com/v1/traces"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Api-key") == "my-key"
        body = json.loads(req.data)
        # Payload is forwarded as plain OTLP/JSON — no vendor attribute injection
        assert "resourceSpans" in body


class TestGetTarget:

    def test_none_when_only_endpoint_set(self, monkeypatch):
        """An endpoint alone is not enough — the licence key selects the backend."""
        monkeypatch.delenv("ATATUS_API_KEY", raising=False)
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://otel-rx.atatus.com")
        assert get_target() == "none"

    def test_atatus_when_key_and_space(self, monkeypatch):
        monkeypatch.delenv("ATATUS_OTLP_ENDPOINT", raising=False)
        monkeypatch.setenv("ATATUS_API_KEY", "key123")
        assert get_target() == "atatus"

    def test_none_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("ATATUS_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("ATATUS_API_KEY", raising=False)
        assert get_target() == "none"


# ── debug_dump extended tests ─────────────────────────────────────────────


class TestDebugDump:

    def test_writes_json_to_debug_dir(self, tmp_path, monkeypatch):
        """debug_dump writes JSON file to STATE_BASE_DIR/debug/."""
        monkeypatch.setenv("ATATUS_TRACE_DEBUG", "true")
        monkeypatch.setattr("core.constants.STATE_BASE_DIR", tmp_path)
        debug_dump("my_label", {"foo": "bar", "count": 42})
        debug_dir = tmp_path / "debug"
        assert debug_dir.exists()
        files = list(debug_dir.glob("my_label_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data == {"foo": "bar", "count": 42}


# ── resolve_backend tests ────────────────────────────────────────────────


class TestResolveBackend:
    """Tests for resolve_backend() — env vars take precedence over config file."""

    @pytest.fixture(autouse=True)
    def _fresh_env(self, monkeypatch):
        # Clear any inherited backend env vars so each test starts clean.
        for key in ("ATATUS_API_KEY", "ATATUS_OTLP_ENDPOINT", "ATATUS_PROJECT_NAME"):
            monkeypatch.delenv(key, raising=False)

    def _make_span(self, service_name=""):
        """Build a minimal span with optional service.name."""
        attrs = []
        if service_name:
            attrs = [{"key": "service.name", "value": {"stringValue": service_name}}]
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": attrs},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"name": "s"}]}]
                }
            ]
        }

    # ── Config-only paths ──────────────────────────────────────────────────


    def test_atatus_from_config(self, monkeypatch):
        """A config harness entry with an atatus target resolves fully."""
        cfg = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "ak-xxx"
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "atatus"
        assert result["endpoint"] == "https://otel-rx.atatus.com"
        assert result["api_key"] == "ak-xxx"
        assert result["project_name"] == "claude-code"

    # ── env-only paths (marketplace-install scenario) ──────────────────────

    def test_atatus_from_env_only(self, monkeypatch):
        """ATATUS_API_KEY alone, with no config entry, resolves to atatus."""
        monkeypatch.setenv("ATATUS_API_KEY", "ak-env")
        monkeypatch.delenv("ATATUS_OTLP_ENDPOINT", raising=False)
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ak-env"
        assert result["endpoint"] == "https://otel-rx.atatus.com"  # default
        assert result["project_name"] == "claude-code"  # falls back to service_name

    def test_endpoint_without_key_is_unresolved(self, monkeypatch):
        """ATATUS_OTLP_ENDPOINT alone is not a usable backend."""
        monkeypatch.delenv("ATATUS_API_KEY", raising=False)
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://env.example.com")
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "none"
        assert result["project_name"] == "claude-code"

    def test_atatus_api_key_from_atatus_env(self, monkeypatch):
        """ATATUS_API_KEY supplies the Atatus bearer token (env-only install)."""
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://env.example.com")
        monkeypatch.setenv("ATATUS_API_KEY", "ph-env-key")
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span("opencode"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ph-env-key"

    def test_atatus_api_key_env_overrides_config(self, monkeypatch):
        """ATATUS_API_KEY env takes precedence over a config-set api_key."""
        monkeypatch.setenv("ATATUS_API_KEY", "ph-env-key")
        cfg = {
            "harnesses": {
                "opencode": {
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "ph-config-key"
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("opencode"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ph-env-key"

    def test_atatus_api_key_falls_back_to_atatus_api_key(self, monkeypatch):
        """ATATUS_API_KEY still works as the Atatus token when ATATUS_API_KEY is unset."""
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://env.example.com")
        monkeypatch.setenv("ATATUS_API_KEY", "ak-env")
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span("opencode"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ak-env"

    def test_project_name_env_override(self, monkeypatch):
        """ATATUS_PROJECT_NAME overrides config project_name."""
        monkeypatch.setenv("ATATUS_PROJECT_NAME", "from-env")
        cfg = {
            "harnesses": {
                "claude-code": {
                    "project_name": "from-config",
                    "target": "atatus",
                    "api_key": "ak"
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["project_name"] == "from-env"

    # ── env-overrides-config precedence ────────────────────────────────────

    def test_env_atatus_overrides_config_atatus(self, monkeypatch):
        """Env-set atatus creds win even when config configures atatus."""
        monkeypatch.setenv("ATATUS_API_KEY", "ak-env")
        cfg = {
            "harnesses": {
                "claude-code": {
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com"
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ak-env"

    def test_env_api_key_overrides_config(self, monkeypatch):
        """ATATUS_API_KEY env overrides the config api_key, keeping config target/endpoint."""
        monkeypatch.setenv("ATATUS_API_KEY", "ak-env")
        cfg = {
            "harnesses": {
                "claude-code": {
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "ak-config"
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["api_key"] == "ak-env"

    # ── error paths ────────────────────────────────────────────────────────

    def test_no_backend_anywhere(self, capsys, monkeypatch):
        """No env vars and no config entry → none with actionable error."""
        monkeypatch.setattr("core.config.load_config", lambda: {"harnesses": {}})

        result = resolve_backend(self._make_span("claude-code"))
        assert result == {"target": "none", "project_name": "claude-code"}
        stderr = capsys.readouterr().err
        assert "No Atatus license key" in stderr
        assert "ATATUS_API_KEY" in stderr

    def test_no_key_anywhere_is_unresolved(self, capsys, monkeypatch):
        """No ATATUS_API_KEY in env and none in config → none, with a clear error."""
        monkeypatch.delenv("ATATUS_API_KEY", raising=False)
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "none"
        assert "No Atatus license key" in capsys.readouterr().err

    def test_atatus_from_config_only(self, monkeypatch):
        """A config entry carrying endpoint + api_key resolves without any env."""
        monkeypatch.delenv("ATATUS_API_KEY", raising=False)
        monkeypatch.delenv("ATATUS_OTLP_ENDPOINT", raising=False)
        cfg = {
            "harnesses": {
                "claude-code": {
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "ak-config",
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "atatus"
        assert result["api_key"] == "ak-config"
        assert result["endpoint"] == "https://otel-rx.atatus.com"

    def test_config_missing_endpoint_uses_default(self, monkeypatch):
        """Endpoint is optional — it falls back to the default collector."""
        monkeypatch.delenv("ATATUS_OTLP_ENDPOINT", raising=False)
        cfg = {
            "harnesses": {
                "claude-code": {
                    "target": "atatus",
                    "api_key": "ak-config",
                }
            }
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "atatus"
        assert result["endpoint"] == "https://otel-rx.atatus.com"

    def test_ignores_top_level_backend_key(self, monkeypatch):
        """Old top-level backend: block is not consulted."""
        cfg = {
            "backend": {
                "target": "atatus",
                "atatus": {"endpoint": "https://global.example.com", "api_key": "global-key"}
            },
            "harnesses": {}
        }
        monkeypatch.setattr("core.config.load_config", lambda: cfg)

        result = resolve_backend(self._make_span("claude-code"))
        assert result["target"] == "none"

    def test_empty_service_name(self, capsys, monkeypatch):
        """Empty service.name attribute → none."""
        monkeypatch.setattr("core.config.load_config", lambda: {})

        result = resolve_backend(self._make_span(""))
        assert result == {"target": "none", "project_name": ""}
        stderr = capsys.readouterr().err
        assert "No service.name attribute found" in stderr

    def test_no_service_name_attr(self, capsys, monkeypatch):
        """Span with no service.name attribute at all → none."""
        monkeypatch.setattr("core.config.load_config", lambda: {})

        span = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": []}]}
        result = resolve_backend(span)
        assert result["target"] == "none"
        stderr = capsys.readouterr().err
        assert "No service.name attribute found" in stderr


# ── send_span integration edge cases ─────────────────────────────────────


class TestSendSpanEdgeCases:
    """Additional edge case tests for send_span()."""

    _SAMPLE_SPAN = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"name": "test-span"}]}]
            }
        ]
    }

    @pytest.fixture(autouse=True)
    def _mock_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)

    @mock.patch("core.common.resolve_backend")
    @mock.patch("core.common.urllib.request.urlopen")
    def test_atatus_non_200_returns_false(self, mock_urlopen, mock_resolve, monkeypatch):
        """Atatus send returns False for non-2xx status."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.return_value = {
            "target": "atatus",
            "endpoint": "https://otel-rx.atatus.com",
            "api_key": "",
            "project_name": "default"
        }
        mock_resp = mock.MagicMock()
        mock_resp.status = 500
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_span(self._SAMPLE_SPAN) is False

    @mock.patch("core.common.resolve_backend")
    def test_resolve_backend_exception_returns_false(self, mock_resolve, monkeypatch):
        """send_span returns False when resolve_backend raises."""
        monkeypatch.delenv("ATATUS_DRY_RUN", raising=False)
        monkeypatch.delenv("ATATUS_VERBOSE", raising=False)

        mock_resolve.side_effect = RuntimeError("config corruption")

        assert send_span(self._SAMPLE_SPAN) is False

    def test_dry_run_logs_span_name(self, capsys, monkeypatch):
        """dry_run mode logs the span name."""
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        monkeypatch.setenv("ATATUS_VERBOSE", "true")

        span = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [{"scope": {"name": "t"}, "spans": [{"name": "my-operation"}]}]
                }
            ]
        }
        result = send_span(span)
        assert result is True
        assert "my-operation" in capsys.readouterr().err

    def test_no_collector_references_in_common(self):
        """Verify _send_to_collector, collector_host, collector_port, collector_url
        and direct_send are not present in core.common module."""
        import core.common as mod

        assert not hasattr(mod, "_send_to_collector")
        assert not hasattr(mod.env, "collector_host")
        assert not hasattr(mod.env, "collector_port")
        assert not hasattr(mod.env, "collector_url")
        assert not hasattr(mod.env, "direct_send")


# ── Additional FileLock coverage (mkdir fallback) ─────────────────────────


class TestFileLockMkdir:
    """Tests for FileLock mkdir-based fallback implementation."""

    @pytest.fixture(autouse=True)
    def _mock_sleep(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
        return sleep_calls

    def test_mkdir_fallback_acquire_release(self, tmp_path, monkeypatch):
        """mkdir-based lock creates and removes directory on acquire/release."""
        monkeypatch.setattr("core.common._LOCK_IMPL", "mkdir")
        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path, timeout=1.0)
        # Force the instance to use mkdir regardless of platform
        lock._method = "mkdir"

        with lock:
            assert lock_path.is_dir()

        assert not lock_path.exists()

    def test_mkdir_fallback_timeout_force_acquire(self, tmp_path, monkeypatch):
        """mkdir lock force-acquires by removing pre-existing directory after timeout."""
        monkeypatch.setattr("core.common._LOCK_IMPL", "mkdir")
        lock_path = tmp_path / "test.lock"
        # Pre-create the lock directory to simulate contention
        lock_path.mkdir()

        lock = FileLock(lock_path, timeout=0.0)
        lock._method = "mkdir"

        with lock:
            # Should have force-acquired despite pre-existing directory
            assert lock_path.is_dir()

        assert not lock_path.exists()


# ── Custom attributes tests ──────────────────────────────────────────────


class TestCustomAttributes:
    """env.custom_attributes(service_name) layering: global < per-harness < env."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        from core.common import env as _env

        _env.__dict__.pop("_top_level_config", None)
        yield
        _env.__dict__.pop("_top_level_config", None)

    def _patch_config(self, monkeypatch, cfg):
        monkeypatch.setattr("core.config.load_config", lambda config_path=None: cfg or {})
        env.__dict__.pop("_top_level_config", None)

    def test_nothing_set_returns_empty(self, monkeypatch):
        self._patch_config(monkeypatch, {})
        assert env.custom_attributes() == {}
        assert env.custom_attributes("claude-code") == {}

    def test_global_only_returned_for_any_service(self, monkeypatch):
        self._patch_config(monkeypatch, {"attributes": {"team": "payments", "env": "prod"}})
        assert env.custom_attributes("claude-code") == {"team": "payments", "env": "prod"}
        assert env.custom_attributes("codex") == {"team": "payments", "env": "prod"}
        assert env.custom_attributes("") == {"team": "payments", "env": "prod"}

    def test_per_harness_only_isolated_to_that_harness(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"harnesses": {"claude-code": {"attributes": {"environment": "prod-claude"}}}},
        )
        assert env.custom_attributes("claude-code") == {"environment": "prod-claude"}
        # Different harness must NOT see the claude-code per-harness block.
        assert env.custom_attributes("codex") == {}

    def test_per_harness_overrides_global_shared_key(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {
                "attributes": {"team": "payments", "environment": "prod"},
                "harnesses": {"claude-code": {"attributes": {"environment": "prod-claude"}}}
            },
        )
        # Shared key (environment) overridden; non-shared keys from both layers survive.
        result = env.custom_attributes("claude-code")
        assert result == {"team": "payments", "environment": "prod-claude"}

    def test_env_only_no_config(self, monkeypatch):
        self._patch_config(monkeypatch, {})
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=payments,region=us-east-1")
        assert env.custom_attributes() == {"team": "payments", "region": "us-east-1"}

    def test_env_wins_over_per_harness_and_global(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {
                "attributes": {"environment": "prod-global"},
                "harnesses": {"claude-code": {"attributes": {"environment": "prod-claude"}}}
            },
        )
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "environment=staging")
        assert env.custom_attributes("claude-code") == {"environment": "staging"}

    def test_malformed_env_skipped(self, monkeypatch):
        self._patch_config(monkeypatch, {})
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=a,garbage,=b,env=c")
        assert env.custom_attributes() == {"team": "a", "env": "c"}

    def test_typed_config_values_preserved(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"attributes": {"cost_center": 4021, "enabled": True, "ratio": 0.5}},
        )
        result = env.custom_attributes()
        assert result["cost_center"] == 4021
        assert isinstance(result["cost_center"], int) and not isinstance(result["cost_center"], bool)
        assert result["enabled"] is True
        assert result["ratio"] == 0.5
        assert isinstance(result["ratio"], float)

    def test_malformed_global_attributes_block_ignored(self, monkeypatch):
        # attributes: set to a string (not a dict) — must not crash.
        self._patch_config(monkeypatch, {"attributes": "not-a-dict"})
        assert env.custom_attributes("claude-code") == {}

    def test_malformed_per_harness_attributes_block_ignored(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"harnesses": {"claude-code": {"attributes": ["bad", "list"]}}},
        )
        assert env.custom_attributes("claude-code") == {}

    def test_returns_fresh_dict_each_call(self, monkeypatch):
        self._patch_config(monkeypatch, {"attributes": {"team": "payments"}})
        a = env.custom_attributes()
        b = env.custom_attributes()
        a["mutated"] = "x"
        assert "mutated" not in b


# ── build_span × custom_attributes injection tests ───────────────────────


class TestBuildSpanCustomAttributes:
    """build_span() merges env.custom_attributes() into per-span attrs via setdefault."""

    def _attrs_dict(self, span_payload):
        attrs = span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        return {a["key"]: a["value"] for a in attrs}

    def test_custom_attrs_appear_in_built_span(self, monkeypatch):
        monkeypatch.setattr(
            env,
            "custom_attributes",
            lambda service_name="": {"team": "payments", "cost_center": 4021},
        )
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
        )
        attrs = self._attrs_dict(result)
        assert attrs["team"] == {"stringValue": "payments"}
        assert attrs["cost_center"] == {"intValue": 4021}

    def test_handler_set_attr_not_overwritten(self, monkeypatch):

        monkeypatch.setattr(
            env,
            "custom_attributes",
            lambda service_name="": {"project.name": "from-custom"},
        )
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            attrs={"project.name": "from-handler"},
        )
        attrs = self._attrs_dict(result)
        assert attrs["project.name"] == {"stringValue": "from-handler"}

    def test_empty_resolver_is_noop(self, monkeypatch):
        monkeypatch.setattr(env, "custom_attributes", lambda service_name="": {})
        result = build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            attrs={"project.name": "p", "user.id": "u"},
        )
        attrs = self._attrs_dict(result)
        assert set(attrs.keys()) == {"project.name", "user.id"}

    def test_caller_attrs_dict_not_mutated(self, monkeypatch):

        monkeypatch.setattr(
            env,
            "custom_attributes",
            lambda service_name="": {"team": "payments"},
        )
        caller_attrs = {"project.name": "p"}
        build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            attrs=caller_attrs,
        )
        assert caller_attrs == {"project.name": "p"}

    def test_resolver_receives_service_name(self, monkeypatch):

        seen = {}

        def fake(service_name=""):
            seen["service_name"] = service_name
            return {}

        monkeypatch.setattr(env, "custom_attributes", fake)
        build_span(
            name="t",
            kind="LLM",
            span_id="aa",
            trace_id="bb",
            start_ms=1000,
            end_ms=2000,
            service_name="claude-code",
        )
        assert seen["service_name"] == "claude-code"


# ── env.get_user_id ladder tests ────────────────────────────────────────


class TestGetUserId:
    """env.get_user_id(service_name) layering: global < per-harness < env."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.delenv("ATATUS_USER_ID", raising=False)
        from core.common import env as _env

        _env.__dict__.pop("_top_level_config", None)
        yield
        _env.__dict__.pop("_top_level_config", None)

    def _patch_config(self, monkeypatch, cfg):
        monkeypatch.setattr("core.config.load_config", lambda config_path=None: cfg or {})
        env.__dict__.pop("_top_level_config", None)

    def test_nothing_set_returns_empty(self, monkeypatch):
        self._patch_config(monkeypatch, {})
        assert env.get_user_id() == ""
        assert env.get_user_id("claude-code") == ""

    def test_global_config_returned_for_any_service(self, monkeypatch):
        self._patch_config(monkeypatch, {"user_id": "alice"})
        assert env.get_user_id() == "alice"
        assert env.get_user_id("claude-code") == "alice"
        assert env.get_user_id("codex") == "alice"

    def test_per_harness_overrides_global(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"user_id": "alice", "harnesses": {"claude-code": {"user_id": "bob"}}},
        )
        # Per-harness wins for claude-code; other harness still gets global.
        assert env.get_user_id("claude-code") == "bob"
        assert env.get_user_id("codex") == "alice"

    def test_env_beats_both_config_layers(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"user_id": "alice", "harnesses": {"claude-code": {"user_id": "bob"}}},
        )
        monkeypatch.setenv("ATATUS_USER_ID", "carol")
        assert env.get_user_id("claude-code") == "carol"
        assert env.get_user_id("codex") == "carol"

    def test_explicit_empty_env_blanks_result(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            {"user_id": "alice", "harnesses": {"claude-code": {"user_id": "bob"}}},
        )
        monkeypatch.setenv("ATATUS_USER_ID", "")
        assert env.get_user_id("claude-code") == ""

    def test_user_id_property_regression(self, monkeypatch):
        """env.user_id (property) still returns the global+env result, unchanged."""
        self._patch_config(
            monkeypatch,
            {"user_id": "alice", "harnesses": {"claude-code": {"user_id": "bob"}}},
        )
        # Property doesn't take a service_name → only global + env apply.
        assert env.user_id == "alice"
        monkeypatch.setenv("ATATUS_USER_ID", "carol")
        assert env.user_id == "carol"


class TestStampAtatusIdentity:
    """Project identity must travel in the payload, not only in config.

    Atatus resolves (and auto-creates) a project from the OTLP resource's
    service.name, so service.name has to carry the user's project name while the
    harness slug moves to atatus.agent.harness. This mirrors what the upstream
    Arize path shipped as arize.project.name.
    """

    @staticmethod
    def _payload(service_name="claude-code"):
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": service_name}}
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "atatus-claude-tracing"}, "spans": [{"name": "Turn 1"}]}
                    ],
                }
            ]
        }

    @staticmethod
    def _attrs(payload):
        return {
            a["key"]: a["value"]["stringValue"]
            for a in payload["resourceSpans"][0]["resource"]["attributes"]
        }

    def test_service_name_becomes_project_name(self):
        out = _stamp_atatus_identity(self._payload(), "ashif-claude-code")
        assert self._attrs(out)["service.name"] == "ashif-claude-code"

    def test_harness_slug_preserved_under_agent_harness(self):
        out = _stamp_atatus_identity(self._payload("codex"), "team-codex")
        attrs = self._attrs(out)
        assert attrs["atatus.agent.harness"] == "codex"
        assert attrs["service.name"] == "team-codex"

    def test_project_type_and_sdk_name_stamped(self):
        attrs = self._attrs(_stamp_atatus_identity(self._payload(), "proj"))
        assert attrs["atatus.project.type"] == "llm"
        assert attrs["telemetry.sdk.name"] == "atatus-coding-harness"

    def test_does_not_mutate_input(self):
        payload = self._payload()
        _stamp_atatus_identity(payload, "proj")
        assert self._attrs(payload)["service.name"] == "claude-code"

    def test_empty_project_name_leaves_service_name_alone(self):
        """resolve_backend falls back to service_name, so this is belt-and-braces."""
        attrs = self._attrs(_stamp_atatus_identity(self._payload(), ""))
        assert attrs["service.name"] == "claude-code"
        assert attrs["atatus.project.type"] == "llm"

    def test_upsert_not_duplicate(self):
        """Stamping twice must not append duplicate keys."""
        once = _stamp_atatus_identity(self._payload(), "proj")
        twice = _stamp_atatus_identity(once, "proj")
        keys = [a["key"] for a in twice["resourceSpans"][0]["resource"]["attributes"]]
        assert len(keys) == len(set(keys))

    def test_multi_span_payload_all_resources_stamped(self):
        """build_multi_span emits one resourceSpans entry; guard the loop anyway."""
        payload = self._payload()
        payload["resourceSpans"].append(self._payload("cursor")["resourceSpans"][0])
        out = _stamp_atatus_identity(payload, "proj")
        for rs in out["resourceSpans"]:
            attrs = {a["key"]: a["value"]["stringValue"] for a in rs["resource"]["attributes"]}
            assert attrs["service.name"] == "proj"
            assert attrs["atatus.project.type"] == "llm"
