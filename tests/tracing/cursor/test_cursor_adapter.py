#!/usr/bin/env python3
"""Tests for tracing.cursor.hooks.adapter — Cursor-specific adapter module."""
import hashlib
import json
import threading
import time

import pytest

from tracing.cursor.hooks import adapter

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a temp directory for every test."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


# ── trace_id_from_seed ────────────────────────────────────────────────────


class TestTraceIdFromSeed:
    def test_returns_32_hex(self):
        result = adapter.trace_id_from_seed("conv-abc")
        assert len(result) == 32
        int(result, 16)  # must be valid hex

    def test_deterministic(self):
        """Separate hook processes share no memory, so the same seed must map
        to the same trace in each of them."""
        a = adapter.trace_id_from_seed("conv-abc")
        b = adapter.trace_id_from_seed("conv-abc")
        assert a == b

    def test_different_inputs_differ(self):
        a = adapter.trace_id_from_seed("conv-abc")
        b = adapter.trace_id_from_seed("conv-xyz")
        assert a != b

    def test_uses_sha256_not_md5(self):
        """MD5 raises on a host running OpenSSL in FIPS mode."""
        assert adapter.trace_id_from_seed("conv-abc") == hashlib.sha256(b"conv-abc").hexdigest()[:32]
        assert adapter.trace_id_from_seed("conv-abc") != hashlib.md5(b"conv-abc").hexdigest()[:32]


# ── span_id_16 ────────────────────────────────────────────────────────────


class TestSpanId16:
    def test_returns_16_hex(self):
        result = adapter.span_id_16()
        assert len(result) == 16
        int(result, 16)

    def test_unique(self):
        a = adapter.span_id_16()
        b = adapter.span_id_16()
        assert a != b


# ── sanitize ──────────────────────────────────────────────────────────────


class TestSanitize:
    def test_unchanged(self):
        assert adapter.sanitize("hello") == "hello"

    def test_slash(self):
        assert adapter.sanitize("foo/bar") == "foo_bar"

    def test_preserves_dots_hyphens_underscores(self):
        assert adapter.sanitize("foo.bar-baz_qux") == "foo.bar-baz_qux"

    def test_special_chars(self):
        assert adapter.sanitize("a@b#c$d") == "a_b_c_d"

    def test_empty(self):
        assert adapter.sanitize("") == ""


# ── state_push / state_pop ────────────────────────────────────────────────


class TestStateStack:
    def test_push_pop_single(self):
        adapter.state_push("test_key", {"a": 1})
        result = adapter.state_pop("test_key")
        assert result == {"a": 1}

    def test_lifo_order(self):
        adapter.state_push("k", {"val": "A"})
        adapter.state_push("k", {"val": "B"})
        assert adapter.state_pop("k") == {"val": "B"}
        assert adapter.state_pop("k") == {"val": "A"}

    def test_pop_empty_returns_none(self):
        assert adapter.state_pop("nonexistent") is None

    def test_pop_corrupted_returns_none(self):
        stack_file = adapter.STATE_DIR / "bad.stack.json"
        stack_file.write_text(":::not valid json{{{")
        assert adapter.state_pop("bad") is None

    def test_push_creates_file(self):
        adapter.state_push("new_key", {"x": 1})
        stack_file = adapter.STATE_DIR / "new_key.stack.json"
        assert stack_file.exists()

    def test_pop_last_leaves_empty_list(self):
        adapter.state_push("k2", {"x": 1})
        adapter.state_pop("k2")
        stack_file = adapter.STATE_DIR / "k2.stack.json"
        data = json.loads(stack_file.read_text())
        assert data == []

    def test_concurrent_push(self):
        """5 threads push concurrently — all values present, no corruption."""
        errors = []

        def push_val(i):
            try:
                adapter.state_push("concurrent", {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=push_val, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stack_file = adapter.STATE_DIR / "concurrent.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 5
        values = sorted(d["i"] for d in data)
        assert values == [0, 1, 2, 3, 4]

    def test_stack_file_valid_json(self):
        adapter.state_push("json_check", {"a": 1})
        adapter.state_push("json_check", {"b": 2})
        stack_file = adapter.STATE_DIR / "json_check.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 2

    def test_stack_file_is_pretty_indented(self):
        """JSON output uses indent=2 per the project's serialization convention."""
        adapter.state_push("pretty", {"a": 1, "b": [1, 2]})
        stack_file = adapter.STATE_DIR / "pretty.stack.json"
        text = stack_file.read_text()
        # indented JSON contains newlines between entries
        assert "\n" in text
        # confirm round-trip
        assert json.loads(text) == [{"a": 1, "b": [1, 2]}]

    def test_push_then_pop_roundtrip_with_nested(self):
        """JSON encoder must round-trip nested dicts/lists with the same fidelity YAML did."""
        payload = {"cmd": "echo hi", "env": {"FOO": "bar"}, "args": ["a", "b", "c"]}
        adapter.state_push("nested", payload)
        popped = adapter.state_pop("nested")
        assert popped == payload

    def test_empty_file_treated_as_empty_stack(self):
        """An empty stack file should be treated as []. push must still succeed."""
        empty = adapter.STATE_DIR / "empty.stack.json"
        empty.write_text("")
        # push should not raise — empty content → JSONDecodeError → fallback to []
        adapter.state_push("empty", {"k": 1})
        assert json.loads(empty.read_text()) == [{"k": 1}]

    def test_corrupt_file_on_push_resets_to_list(self):
        """If existing file is corrupt JSON, push resets list and appends."""
        corrupt = adapter.STATE_DIR / "corrupt.stack.json"
        corrupt.write_text("{not json")
        adapter.state_push("corrupt", {"k": "v"})
        assert json.loads(corrupt.read_text()) == [{"k": "v"}]


# ── gen_root_span ─────────────────────────────────────────────────────────


class TestGenRootSpan:
    def test_save_and_get(self):
        adapter.gen_root_span_save("gen-1", "span123")
        assert adapter.gen_root_span_get("gen-1") == "span123"

    def test_get_no_save(self):
        assert adapter.gen_root_span_get("gen-missing") == ""

    def test_get_empty_gen_id(self):
        assert adapter.gen_root_span_get("") == ""

    def test_save_overwrites(self):
        adapter.gen_root_span_save("gen-2", "old_span")
        adapter.gen_root_span_save("gen-2", "new_span")
        assert adapter.gen_root_span_get("gen-2") == "new_span"


# ── state_cleanup_generation ──────────────────────────────────────────────


class TestStateCleanupGeneration:
    def test_cleanup_removes_all_files(self):
        gen_id = "gen-cleanup"
        safe = adapter.sanitize(gen_id)

        # Create root file
        adapter.gen_root_span_save(gen_id, "span1")
        # Create stack files
        adapter.state_push(f"before_{safe}_shell", {"cmd": "ls"})
        adapter.state_push(f"before_{safe}_mcp", {"tool": "read"})

        adapter.state_cleanup_generation(gen_id)

        assert not (adapter.STATE_DIR / f"root_{safe}").exists()
        assert not list(adapter.STATE_DIR.glob(f"*{safe}*.stack.json"))

    def test_cleanup_no_files_no_error(self):
        adapter.state_cleanup_generation("gen-nonexistent")  # should not raise

    def test_cleanup_preserves_other_generations(self):
        adapter.gen_root_span_save("gen-keep", "span_keep")
        adapter.gen_root_span_save("gen-remove", "span_remove")

        adapter.state_cleanup_generation("gen-remove")

        assert adapter.gen_root_span_get("gen-keep") == "span_keep"

    def test_cleanup_nonempty_lock_dir(self):
        gen_id = "gen-lockdir"
        safe = adapter.sanitize(gen_id)
        lock_dir = adapter.STATE_DIR / f".lock_before_{safe}_shell"
        lock_dir.mkdir(parents=True)
        # Put a file inside so rmdir fails
        (lock_dir / "stale").write_text("x")

        adapter.state_cleanup_generation(gen_id)
        # dir should still exist (rmdir fails on non-empty), but no crash
        assert lock_dir.exists()


# ── check_requirements ────────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "true")
        assert adapter.check_requirements() is True
        assert adapter.STATE_DIR.exists()

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("ATATUS_TRACE_ENABLED", "false")
        assert adapter.check_requirements() is False


# ── stale state garbage collection ────────────────────────────────────────


class TestGcStaleStateFiles:
    """A cancelled turn, or one whose stop hook carried a different generation
    id, leaves state nothing will ever pop. Cursor's keys hold no pid, so age is
    the only signal available."""

    def _age(self, path, seconds):
        import os

        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_removes_state_older_than_the_cutoff(self, _patch_state_dir):
        stale = _patch_state_dir / "root_gen-old.stack.json"
        stale.write_text("[]")
        self._age(stale, 60 * 60 * 24)

        adapter.gc_stale_state_files()
        assert not stale.exists()

    def test_keeps_state_from_a_live_turn(self, _patch_state_dir):
        fresh = _patch_state_dir / "root_gen-new.stack.json"
        fresh.write_text("[]")

        adapter.gc_stale_state_files()
        assert fresh.exists()

    def test_removes_stale_lock_files_not_just_directories(self, _patch_state_dir):
        """FileLock only makes a directory in its mkdir fallback; with fcntl or
        msvcrt available — which is every platform we run on — it makes a plain
        file, and the old cleanup skipped those entirely."""
        lock_file = _patch_state_dir / ".lock_root_gen-old"
        lock_file.write_text("")
        lock_dir = _patch_state_dir / ".lock_root_gen-older"
        lock_dir.mkdir()
        for path in (lock_file, lock_dir):
            self._age(path, 60 * 60 * 24)

        adapter.gc_stale_state_files()
        assert not lock_file.exists()
        assert not lock_dir.exists()

    def test_leaves_unrelated_files_alone(self, _patch_state_dir):
        other = _patch_state_dir / "notes.txt"
        other.write_text("keep me")
        self._age(other, 60 * 60 * 24)

        adapter.gc_stale_state_files()
        assert other.exists()

    def test_no_state_directory_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "absent")
        adapter.gc_stale_state_files()


class TestStateCleanupGenerationLocks:
    def test_removes_lock_files(self, _patch_state_dir):
        lock_file = _patch_state_dir / ".lock_root_gen-1"
        lock_file.write_text("")
        adapter.state_cleanup_generation("gen-1")
        assert not lock_file.exists()

    def test_blank_generation_id_is_a_no_op(self, _patch_state_dir):
        """A blank id makes the glob '**', which Path.glob rejects outright."""
        keep = _patch_state_dir / "root_gen-1.stack.json"
        keep.write_text("[]")
        adapter.state_cleanup_generation("")
        assert keep.exists()


class TestGenRootSpanSave:
    def test_write_is_atomic(self, _patch_state_dir):
        """One process per hook event runs concurrently; a reader that caught a
        plain write half-done would use a truncated span id as a parent."""
        adapter.gen_root_span_save("gen-1", "a" * 16)
        assert adapter.gen_root_span_get("gen-1") == "a" * 16
        assert not list(_patch_state_dir.glob("*.tmp.*"))

    def test_overwrite_leaves_no_partial_state(self, _patch_state_dir):
        adapter.gen_root_span_save("gen-1", "a" * 16)
        adapter.gen_root_span_save("gen-1", "b" * 16)
        assert adapter.gen_root_span_get("gen-1") == "b" * 16
