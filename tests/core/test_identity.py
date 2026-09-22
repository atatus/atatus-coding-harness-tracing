#!/usr/bin/env python3
"""Tests for core.identity — per-harness login-identity detection.

Every detector must return "" (never raise) on a missing file, malformed
JSON, missing binary, or non-zero/timing-out subprocess — that contract is
what lets core.common.get_user_login_id skip its own try/except being the
only thing standing between a bad detector and a crashed hook.
"""

import base64
import json
import subprocess
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


class TestCopilotDetector:
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
