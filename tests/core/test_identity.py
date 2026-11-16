#!/usr/bin/env python3
"""Tests for core.identity — per-harness login-identity detection.

Every detector must return "" (never raise) on a missing file, malformed
JSON, missing binary, or non-zero/timing-out subprocess — that contract is
what lets core.common.get_user_login_id skip its own try/except being the
only thing standing between a bad detector and a crashed hook.
"""

import base64
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from core.identity import (
    detect_antigravity_login_id,
    detect_claude_code_login_id,
    detect_codex_login_id,
    detect_copilot_login_id,
    detect_gemini_login_id,
    detect_omp_login_id,
    detect_opencode_login_id,
    read_kiro_login_probe,
    start_kiro_login_probe,
)


def _jwt_with_claims(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


class TestClaudeCodeDetector:
    def test_reads_email_from_default_home_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "alice@example.com"}}))
        assert detect_claude_code_login_id() == "alice@example.com"

    def test_reads_from_claude_config_dir_when_set(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "profile-work"
        config_dir.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        (config_dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "work@example.com"}}))
        assert detect_claude_code_login_id() == "work@example.com"

    def test_falls_back_to_cli_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout='{"loggedIn": true, "email": "cli@example.com"}')

        monkeypatch.setattr("core.identity.subprocess.run", fake_run)
        assert detect_claude_code_login_id() == "cli@example.com"

    def test_missing_file_and_missing_binary_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_run(*args, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr("core.identity.subprocess.run", fake_run)
        assert detect_claude_code_login_id() == ""

    def test_cli_timeout_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1.5)

        monkeypatch.setattr("core.identity.subprocess.run", fake_run)
        assert detect_claude_code_login_id() == ""

    def test_malformed_file_falls_through_to_cli(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude.json").write_text("not json")

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout='{"email": "cli@example.com"}')

        monkeypatch.setattr("core.identity.subprocess.run", fake_run)
        assert detect_claude_code_login_id() == "cli@example.com"


class TestCodexDetector:
    def test_reads_email_from_id_token_claim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        token = _jwt_with_claims({"email": "alice@example.com", "email_verified": True})
        (codex_dir / "auth.json").write_text(json.dumps({"tokens": {"id_token": token}}))
        assert detect_codex_login_id() == "alice@example.com"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_codex_login_id() == ""

    def test_token_without_email_claim_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        token = _jwt_with_claims({"sub": "user-123"})
        (codex_dir / "auth.json").write_text(json.dumps({"tokens": {"id_token": token}}))
        assert detect_codex_login_id() == ""

    def test_malformed_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("{not valid json")
        assert detect_codex_login_id() == ""

    def test_malformed_jwt_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text(json.dumps({"tokens": {"id_token": "not-a-jwt"}}))
        assert detect_codex_login_id() == ""


class TestGeminiDetector:
    def test_reads_active_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "alice@gmail.com", "old": ["bob@gmail.com"]})
        )
        assert detect_gemini_login_id() == "alice@gmail.com"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_gemini_login_id() == ""

    def test_malformed_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / "google_accounts.json").write_text("not json")
        assert detect_gemini_login_id() == ""


_COPILOT_CONFIG_WITH_HEADER = """// User settings belong in settings.json.
// This file is managed automatically.
{
  "firstLaunchAt": "2026-01-01T00:00:00.000Z",
  "appTipShown": true,
  "lastLoggedInUser": {
    "host": "https://github.com",
    "login": "octocat"
  },
  "loggedInUsers": [
    {
      "host": "https://github.com",
      "login": "octocat"
    }
  ]
}
"""


class TestCopilotDetector:
    @pytest.fixture(autouse=True)
    def _default_copilot_home(self, monkeypatch):
        monkeypatch.delenv("COPILOT_HOME", raising=False)

    def test_reads_login_from_config_with_comment_header(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "config.json").write_text(_COPILOT_CONFIG_WITH_HEADER)
        assert detect_copilot_login_id() == "octocat"

    def test_reads_from_copilot_home_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        copilot_home = tmp_path / "custom-copilot"
        copilot_home.mkdir()
        (copilot_home / "config.json").write_text(_COPILOT_CONFIG_WITH_HEADER.replace("octocat", "hubot"))
        monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
        assert detect_copilot_login_id() == "hubot"

    def test_copilot_home_ignores_default_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        default_dir = tmp_path / ".copilot"
        default_dir.mkdir()
        (default_dir / "config.json").write_text(_COPILOT_CONFIG_WITH_HEADER)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "empty-copilot-home"))
        assert detect_copilot_login_id() == ""

    def test_inline_and_block_comments_keep_urls_in_strings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "config.json").write_text(
            "/* managed */\n"
            "{\n"
            '  "lastLoggedInUser": {\n'
            '    "host": "https://github.com/*not-a-comment*/", // trailing\n'
            '    "login": "octocat"\n'
            "  }\n"
            "}\n"
        )
        assert detect_copilot_login_id() == "octocat"

    def test_malformed_config_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "config.json").write_text('// header\n{"lastLoggedInUser": {"login": ')
        assert detect_copilot_login_id() == ""

    def test_reads_github_username(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "config.json").write_text(json.dumps({"lastLoggedInUser": {"login": "octocat"}}))
        assert detect_copilot_login_id() == "octocat"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_copilot_login_id() == ""

    def test_not_logged_in_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "config.json").write_text(json.dumps({}))
        assert detect_copilot_login_id() == ""


class TestOpencodeDetector:
    def test_reads_email_from_oauth_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        auth_dir = tmp_path / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(
            json.dumps({"chatgpt": {"type": "oauth", "email": "alice@example.com"}})
        )
        assert detect_opencode_login_id() == "alice@example.com"

    def test_api_key_provider_has_no_email(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        auth_dir = tmp_path / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(json.dumps({"ollama-cloud": {"type": "api", "key": "sk-..."}}))
        assert detect_opencode_login_id() == ""

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_opencode_login_id() == ""


def _opencode_data_dir(home: Path) -> Path:
    data_dir = home / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _build_opencode_db(path: Path, accounts=(), control_accounts=(), active_account_id=None) -> None:
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE `account` (
          `id` text PRIMARY KEY,
          `email` text NOT NULL,
          `url` text NOT NULL,
          `access_token` text NOT NULL,
          `refresh_token` text NOT NULL,
          `token_expiry` integer,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL
        );
        CREATE TABLE `control_account` (
          `email` text NOT NULL,
          `url` text NOT NULL,
          `access_token` text NOT NULL,
          `refresh_token` text NOT NULL,
          `token_expiry` integer,
          `active` integer NOT NULL,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          CONSTRAINT `control_account_pk` PRIMARY KEY(`email`, `url`)
        );
        CREATE TABLE `account_state` (
          `id` integer PRIMARY KEY,
          `active_account_id` text,
          `active_org_id` text,
          FOREIGN KEY (`active_account_id`) REFERENCES `account`(`id`) ON DELETE SET NULL
        );
        """)
    for account_id, email in accounts:
        con.execute(
            "INSERT INTO account VALUES (?, ?, 'https://opencode.ai', 'at', 'rt', NULL, 1, 1)",
            (account_id, email),
        )
    for email, active, updated in control_accounts:
        con.execute(
            "INSERT INTO control_account VALUES (?, 'https://opencode.ai', 'at', 'rt', NULL, ?, 1, ?)",
            (email, active, updated),
        )
    if active_account_id is not None:
        con.execute("INSERT INTO account_state VALUES (1, ?, NULL)", (active_account_id,))
    con.commit()
    con.close()


def _write_opencode_oauth_auth(data_dir: Path, email: str) -> None:
    (data_dir / "auth.json").write_text(json.dumps({"chatgpt": {"type": "oauth", "email": email}}))


class TestOpencodeAccountDb:
    def test_reads_active_account_email(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        _build_opencode_db(
            data_dir / "opencode.db",
            accounts=[("acc-1", "first@example.com"), ("acc-2", "active@example.com")],
            active_account_id="acc-2",
        )
        assert detect_opencode_login_id() == "active@example.com"

    def test_reads_account_still_in_uncheckpointed_wal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        db_path = data_dir / "opencode.db"
        _build_opencode_db(db_path)
        writer = sqlite3.connect(str(db_path))
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO account VALUES ('acc-1', 'live@example.com', 'u', 'at', 'rt', NULL, 1, 1)")
            writer.execute("INSERT INTO account_state VALUES (1, 'acc-1', NULL)")
            writer.commit()
            assert detect_opencode_login_id() == "live@example.com"
        finally:
            writer.close()

    def test_active_account_wins_over_auth_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        _build_opencode_db(data_dir / "opencode.db", accounts=[("acc-1", "db@example.com")], active_account_id="acc-1")
        _write_opencode_oauth_auth(data_dir, "provider@example.com")
        assert detect_opencode_login_id() == "db@example.com"

    def test_falls_back_to_active_control_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        _build_opencode_db(
            data_dir / "opencode.db",
            accounts=[("acc-1", "unselected@example.com")],
            control_accounts=[
                ("old@example.com", 0, 300),
                ("current@example.com", 1, 200),
                ("stale@example.com", 1, 100),
            ],
        )
        assert detect_opencode_login_id() == "current@example.com"

    def test_dangling_active_account_id_falls_back_to_control_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        _build_opencode_db(
            data_dir / "opencode.db",
            control_accounts=[("control@example.com", 1, 1)],
            active_account_id="deleted-account",
        )
        assert detect_opencode_login_id() == "control@example.com"

    def test_no_active_account_falls_back_to_auth_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        _build_opencode_db(
            data_dir / "opencode.db",
            accounts=[("acc-1", "unselected@example.com")],
            control_accounts=[("inactive@example.com", 0, 1)],
        )
        _write_opencode_oauth_auth(data_dir, "provider@example.com")
        assert detect_opencode_login_id() == "provider@example.com"

    def test_db_without_account_tables_falls_back_to_auth_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        con = sqlite3.connect(str(data_dir / "opencode.db"))
        con.execute("CREATE TABLE session (id text PRIMARY KEY)")
        con.commit()
        con.close()
        _write_opencode_oauth_auth(data_dir, "provider@example.com")
        assert detect_opencode_login_id() == "provider@example.com"

    def test_corrupt_db_falls_back_to_auth_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        (data_dir / "opencode.db").write_bytes(b"this is not a sqlite database" * 64)
        _write_opencode_oauth_auth(data_dir, "provider@example.com")
        assert detect_opencode_login_id() == "provider@example.com"

    def test_locked_db_falls_back_to_auth_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        db_path = data_dir / "opencode.db"
        _build_opencode_db(db_path, accounts=[("acc-1", "db@example.com")], active_account_id="acc-1")
        _write_opencode_oauth_auth(data_dir, "provider@example.com")
        holder = sqlite3.connect(str(db_path), isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            assert detect_opencode_login_id() == "provider@example.com"
            assert time.monotonic() - started < 1.0
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_never_creates_the_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        assert detect_opencode_login_id() == ""
        assert not (data_dir / "opencode.db").exists()

    def test_never_writes_to_the_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        data_dir = _opencode_data_dir(tmp_path)
        db_path = data_dir / "opencode.db"
        _build_opencode_db(db_path, accounts=[("acc-1", "db@example.com")], active_account_id="acc-1")
        before = db_path.read_bytes()
        assert detect_opencode_login_id() == "db@example.com"
        assert db_path.read_bytes() == before
        assert sorted(p.name for p in data_dir.iterdir()) == ["opencode.db"]


class TestOmpDetector:
    def test_reads_email_key_if_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        omp_dir = tmp_path / ".omp" / "agent"
        omp_dir.mkdir(parents=True)
        (omp_dir / "settings.json").write_text(json.dumps({"email": "alice@example.com"}))
        assert detect_omp_login_id() == "alice@example.com"

    def test_not_installed_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_omp_login_id() == ""


class TestKiroAsyncProbe:
    """Kiro's real CLI takes ~1.9s, so it's fire-and-forget: start_* launches a
    detached process that atomically writes dest_path; read_* is a
    non-blocking check of whether that's happened yet."""

    def test_read_before_probe_finishes_returns_none(self, tmp_path):
        dest = tmp_path / "probe_out"
        assert read_kiro_login_probe(dest) is None

    def test_read_after_probe_writes_email(self, tmp_path):
        dest = tmp_path / "probe_out"
        dest.write_text("Logged in with Google\nEmail: alice@example.com\n")
        assert read_kiro_login_probe(dest) == "alice@example.com"

    def test_read_probe_with_no_email_line_returns_empty_not_none(self, tmp_path):
        dest = tmp_path / "probe_out"
        dest.write_text("Not logged in\n")
        assert read_kiro_login_probe(dest) == ""

    def test_start_probe_never_raises_even_if_binary_missing(self, tmp_path, monkeypatch):
        def fake_popen(*args, **kwargs):
            raise OSError("kiro-cli not found")

        monkeypatch.setattr("core.identity.subprocess.Popen", fake_popen)
        start_kiro_login_probe(tmp_path / "probe_out")  # must not raise

    def test_start_probe_end_to_end_with_real_subprocess(self, tmp_path):
        """Uses `sh` and `echo` (always present) instead of the real kiro-cli
        to verify the atomic-write mechanics without depending on Kiro being
        installed on the test machine."""
        dest = tmp_path / "probe_out"
        import subprocess as sp

        proc = sp.Popen(
            ["sh", "-c", f"echo 'Email: probe@example.com' > {dest}.tmp && mv {dest}.tmp {dest}"],
        )
        proc.wait(timeout=5)
        assert read_kiro_login_probe(dest) == "probe@example.com"


class TestAntigravityDetector:
    def test_reads_email_from_cli_log(self, tmp_path, monkeypatch):
        app_data = tmp_path / "antigravity-cli"
        app_data.mkdir(parents=True)
        (app_data / "cli.log").write_text(
            "I0922 13:49:40 server_oauth.go:201] OAuth: authenticated successfully as loguser@example.com\n"
        )
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(app_data))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_antigravity_login_id() == "loguser@example.com"

    def test_reads_email_from_log_dir(self, tmp_path, monkeypatch):
        app_data = tmp_path / "antigravity-cli"
        log_dir = app_data / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "cli-20260922_120000.log").write_text(
            "I0922 13:49:40 server_oauth.go:196] applyAuthResult: email=diruser@example.com, authMethod=consumer\n"
        )
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(app_data))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_antigravity_login_id() == "diruser@example.com"

    def test_reads_active_account_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "alice@example.com", "old": ["bob@example.com"]})
        )
        assert detect_antigravity_login_id() == "alice@example.com"

    def test_falls_back_to_oauth_creds_id_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        token = _jwt_with_claims({"email": "oauth@example.com"})
        (gemini_dir / "oauth_creds.json").write_text(json.dumps({"id_token": token}))
        assert detect_antigravity_login_id() == "oauth@example.com"

    def test_reads_from_antigravity_app_data_dir(self, tmp_path, monkeypatch):
        app_data = tmp_path / "custom" / "antigravity-cli"
        app_data.mkdir(parents=True)
        gemini_dir = app_data.parent
        (gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "custom@example.com"})
        )
        monkeypatch.setenv("ANTIGRAVITY_APP_DATA_DIR", str(app_data))
        monkeypatch.setenv("HOME", str(tmp_path / "other"))
        assert detect_antigravity_login_id() == "custom@example.com"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert detect_antigravity_login_id() == ""

    def test_malformed_json_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTIGRAVITY_APP_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / "google_accounts.json").write_text("not json")
        (gemini_dir / "oauth_creds.json").write_text("not json either")
        assert detect_antigravity_login_id() == ""
