#!/usr/bin/env python3
"""Tests for codex-tracing install/uninstall module (v2 hooks layout)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import core.setup as _setup
import tracing.codex._toml as codex_toml
import tracing.codex.install as codex_install
from core.common import DEFAULT_OTLP_ENDPOINT
from tracing.codex.constants import NOTIFY_BIN_NAME, get_codex_home

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


ATATUS_BACKEND = ("atatus", {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": ""})
ATATUS_BACKEND = (
    "atatus",
    {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": "ak-xxx"},
)


@pytest.fixture(autouse=True)
def _always_reconfigure(monkeypatch):
    """Take the reconfigure branch of configure_harness.

    These tests predate the "use this existing configuration?" gate and assert
    on what the prompts do with a pre-seeded config, so they need the prompts to
    actually run. The gate itself is covered in tests/core/test_configure_harness.py.
    """
    monkeypatch.setattr(_setup, "_reuse_existing", lambda *a, **k: False)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect all paths to a temp directory."""
    install_dir = tmp_path / ".atatus" / "harness"
    install_dir.mkdir(parents=True)
    config_file = install_dir / "config.json"
    codex_dir = tmp_path / ".codex"
    venv_bin_dir = install_dir / "venv" / "bin"
    venv_bin_dir.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    monkeypatch.setattr("core.setup.INSTALL_DIR", install_dir)
    monkeypatch.setattr("core.setup.CONFIG_FILE", config_file)
    monkeypatch.setattr("core.setup.VENV_DIR", install_dir / "venv")
    monkeypatch.setattr("core.setup.BIN_DIR", install_dir / "bin")
    monkeypatch.setattr("core.setup.RUN_DIR", install_dir / "run")
    monkeypatch.setattr("core.setup.LOG_DIR", install_dir / "logs")
    monkeypatch.setattr("core.setup.STATE_DIR", install_dir / "state")

    monkeypatch.setattr("core.constants.CONFIG_FILE", config_file)
    monkeypatch.setattr("core.config.CONFIG_FILE", config_file)

    # No CODEX_HOME override: get_codex_home() then derives `codex_dir` from the
    # patched Path.home() above, so the installer writes inside tmp_path.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert codex_install.get_codex_home() == codex_dir

    return tmp_path


@pytest.fixture(autouse=True)
def _stub_logging_prompts(monkeypatch):
    """Auto-stub the content-logging wizard so tests don't block on stdin."""
    monkeypatch.setattr(
        _setup,
        "prompt_content_logging",
        lambda: {"prompts": True, "tool_details": True, "tool_content": True},
    )
    monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)


@pytest.fixture()
def mock_prompts(monkeypatch):
    """Mock interactive prompts to return atatus defaults."""
    monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "codex")
    monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
    monkeypatch.setattr(
        _setup,
        "prompt_backend",
        lambda existing_harnesses=None, current=None: ATATUS_BACKEND,
    )


def _mock_prompts_atatus(monkeypatch):
    """Mock interactive prompts to return atatus defaults."""
    monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "codex")
    monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
    monkeypatch.setattr(
        _setup,
        "prompt_backend",
        lambda existing_harnesses=None, current=None: ATATUS_BACKEND,
    )


def _expected_notify_cmd(fake_home: Path) -> str:
    """Compute the venv-bin path the installer should write for notify."""
    return str(fake_home / ".atatus" / "harness" / "venv" / "bin" / NOTIFY_BIN_NAME)


def _hook_commands(toml_data: dict, event: str) -> list[str]:
    """Extract the inner command strings from all [[hooks.<event>]] entries."""
    entries = toml_data.get("hooks", {}).get(event, [])
    cmds: list[str] = []
    for entry in entries:
        for h in entry.get("hooks", []):
            cmd = h.get("command")
            if isinstance(cmd, str):
                cmds.append(cmd)
    return cmds


# ---------------------------------------------------------------------------
# TOML helper tests
# ---------------------------------------------------------------------------


class TestTomlHelpers:
    """Tests for the TOML read/write helpers."""

    def test_roundtrip_simple(self, tmp_path):
        data = {"notify": ["/usr/bin/hook"], "model": {"name": "gpt-4"}}
        p = tmp_path / "config.toml"
        codex_toml._toml_write(data, p)
        parsed = codex_toml._toml_line_parse(p.read_text())
        assert parsed["notify"] == ["/usr/bin/hook"]
        assert parsed["model"]["name"] == "gpt-4"

    def test_roundtrip_array_of_tables(self, tmp_path):
        """Array-of-tables ([[a.b]]) round-trips via tomllib."""
        tomllib = pytest.importorskip("tomllib")
        data = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/x", "timeout": 30}]}]}}
        p = tmp_path / "config.toml"
        codex_toml._toml_write(data, p)
        text = p.read_text()
        assert "[[hooks.SessionStart]]" in text
        parsed = tomllib.loads(text)
        assert parsed == data

    def test_roundtrip_windows_path_with_backslashes(self, tmp_path):
        win_path = r"C:\Users\foo\.atatus\harness\venv\Scripts\atatus-hook-codex-notify.exe"
        data = {"notify": [win_path]}
        p = tmp_path / "config.toml"
        codex_toml._toml_write(data, p)

        raw = p.read_text()
        assert f"'{win_path}'" in raw
        assert "\\\\" not in raw

        parsed = codex_toml._toml_line_parse(raw)
        assert parsed["notify"] == [win_path]

    def test_roundtrip_value_with_single_quote_falls_back_to_basic(self, tmp_path):
        data = {"desc": "it's a value"}
        p = tmp_path / "config.toml"
        codex_toml._toml_write(data, p)

        raw = p.read_text()
        assert 'desc = "it\'s a value"' in raw


# ---------------------------------------------------------------------------
# Install tests — v2 hooks layout
# ---------------------------------------------------------------------------


class TestInstall:
    """Tests for install() under the v2 hooks layout."""

    def test_install_fresh_writes_flat_atatus_entry(self, fake_home, monkeypatch):
        _mock_prompts_atatus(monkeypatch)
        codex_install.install()

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["codex"]
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == DEFAULT_OTLP_ENDPOINT
        assert entry["api_key"] == "ak-xxx"
        assert entry["project_name"] == "codex"

    def test_install_writes_notify_only_layout(self, fake_home, mock_prompts):
        """Fresh install writes one `notify = [...]` entry; no lifecycle hooks, no otel."""
        codex_install.install()

        toml_path = fake_home / ".codex" / "config.toml"
        assert toml_path.is_file()
        data = codex_toml._toml_load(toml_path)
        notify_cmd = _expected_notify_cmd(fake_home)

        # Exactly one notify entry pointing at our hook.
        assert isinstance(data["notify"], list)
        assert data["notify"] == [notify_cmd]

        # No otel block (we ship spans directly from notify, not via OTLP exporter).
        assert "otel" not in data

        # No lifecycle hooks -- the rollout-driven notify path is the only signal.
        assert "hooks" not in data or all(
            not data["hooks"].get(e)
            for e in (
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "PermissionRequest",
                "Stop",
            )
        )

    def test_install_writes_env_file(self, fake_home, mock_prompts):
        codex_install.install()

        env_path = fake_home / ".codex" / "atatus-env.sh"
        assert env_path.is_file()
        env_text = env_path.read_text()
        assert "export ATATUS_TRACE_ENABLED=true" in env_text

    def test_install_existing_codex_entry_only_updates_project_name(self, fake_home, monkeypatch):
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "harnesses": {
                        "codex": {
                            "project_name": "old-name",
                            "target": "atatus",
                            "endpoint": DEFAULT_OTLP_ENDPOINT,
                            "api_key": "ak-existing",
                        }
                    }
                },
                indent=2,
            )
        )

        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": "new-name")
        # A blank line at the credential prompts keeps what is already stored,
        # which is what makes a reconfigure able to change only the name.
        monkeypatch.setattr(_setup, "getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")

        codex_install.install()

        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["codex"]
        assert entry["project_name"] == "new-name"
        assert entry["target"] == "atatus"
        assert entry["api_key"] == "ak-existing"

    def test_install_offers_copy_from_existing_atatus_harness(self, fake_home, monkeypatch):
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "harnesses": {
                        "claude-code": {
                            "project_name": "claude-code",
                            "target": "atatus",
                            "endpoint": DEFAULT_OTLP_ENDPOINT,
                            "api_key": "ak-shared",
                        }
                    }
                },
                indent=2,
            )
        )

        captured_kwargs = {}

        def fake_prompt_backend(existing_harnesses=None, current=None):
            captured_kwargs["existing_harnesses"] = existing_harnesses
            return (
                "atatus",
                {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": "ak-shared"},
            )

        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "codex")
        monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
        monkeypatch.setattr(_setup, "prompt_backend", fake_prompt_backend)

        codex_install.install()

        assert "claude-code" in captured_kwargs["existing_harnesses"]

        config = json.loads(config_file.read_text())
        codex_entry = config["harnesses"]["codex"]
        assert codex_entry["target"] == "atatus"
        assert codex_entry["api_key"] == "ak-shared"

    def test_reinstall_is_idempotent(self, fake_home, mock_prompts):
        """Running install twice produces the same TOML — no duplicates."""
        codex_install.install()
        toml_path = fake_home / ".codex" / "config.toml"
        first = toml_path.read_text()

        codex_install.install()
        second = toml_path.read_text()
        assert first == second

        # Notify entry stays single; the notify-only layout writes no hooks.
        data = codex_toml._toml_load(toml_path)
        assert len(data["notify"]) == 1
        assert "hooks" not in data

    def test_install_with_user_id(self, fake_home, monkeypatch):
        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "codex")
        monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "test-user")
        monkeypatch.setattr(
            _setup,
            "prompt_backend",
            lambda existing_harnesses=None, current=None: ATATUS_BACKEND,
        )

        codex_install.install()

        env_text = (fake_home / ".codex" / "atatus-env.sh").read_text()
        assert "export ATATUS_USER_ID=test-user" in env_text

    def test_install_with_skills_calls_symlink(self, fake_home, mock_prompts):
        with patch.object(codex_install, "symlink_skills") as m_symlink:
            codex_install.install(with_skills=True)
            m_symlink.assert_called_once_with("codex")

    def test_install_prints_completion_message(self, fake_home, mock_prompts, capsys):
        """Install prints a brief success confirmation."""
        codex_install.install()
        out = capsys.readouterr().out
        assert "Codex tracing installed" in out

    def test_install_preserves_unrelated_toml_sections(self, fake_home, mock_prompts):
        """A pre-existing [model] block survives install."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text('[model]\nname = "gpt-4"\n')

        codex_install.install()
        data = codex_toml._toml_load(toml_path)
        assert data.get("model", {}).get("name") == "gpt-4"
        assert "notify" in data


# ---------------------------------------------------------------------------
# Uninstall tests
# ---------------------------------------------------------------------------


class TestUninstall:
    """Tests for uninstall()."""

    def test_uninstall_removes_codex_entry(self, fake_home, mock_prompts):
        codex_install.install()
        codex_install.uninstall()

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        assert "codex" not in config.get("harnesses", {})

    def test_uninstall_removes_our_toml_entries_preserves_unrelated(self, fake_home, mock_prompts):
        codex_install.install()

        toml_path = fake_home / ".codex" / "config.toml"
        data = codex_toml._toml_load(toml_path)
        data["model"] = {"name": "gpt-4"}
        codex_toml._toml_write(data, toml_path)

        codex_install.uninstall()

        assert toml_path.is_file()
        remaining = codex_toml._toml_load(toml_path)
        assert remaining.get("model", {}).get("name") == "gpt-4"
        assert "notify" not in remaining
        assert "hooks" not in remaining

        assert not (fake_home / ".codex" / "atatus-env.sh").is_file()

    def test_uninstall_preserves_foreign_notify(self, fake_home, mock_prompts):
        codex_install.install()

        toml_path = fake_home / ".codex" / "config.toml"
        data = codex_toml._toml_load(toml_path)
        data["notify"].append("/usr/local/bin/my-custom-hook")
        codex_toml._toml_write(data, toml_path)

        codex_install.uninstall()

        remaining = codex_toml._toml_load(toml_path)
        assert remaining["notify"] == ["/usr/local/bin/my-custom-hook"]

    def test_uninstall_preserves_foreign_hook_entries(self, fake_home, mock_prompts):
        """A non-atatus hook entry under [[hooks.PreToolUse]] survives uninstall."""
        codex_install.install()

        # Manually add a foreign hook entry (current install layout has no hooks).
        toml_path = fake_home / ".codex" / "config.toml"
        data = codex_toml._toml_load(toml_path)
        data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"hooks": [{"type": "command", "command": "/usr/local/bin/their-hook"}]}
        )
        codex_toml._toml_write(data, toml_path)

        codex_install.uninstall()

        remaining = codex_toml._toml_load(toml_path)
        # Only the foreign hook survives.
        remaining_cmds = _hook_commands(remaining, "PreToolUse")
        assert remaining_cmds == ["/usr/local/bin/their-hook"]

    def test_uninstall_no_op_when_not_installed(self, fake_home):
        # Should not raise; nothing to do.
        codex_install.uninstall()

    def test_uninstall_is_idempotent(self, fake_home, mock_prompts):
        codex_install.install()

        codex_install.uninstall()
        # Second uninstall should not raise.
        codex_install.uninstall()

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        if config_file.is_file():
            config = json.loads(config_file.read_text())
            assert "codex" not in config.get("harnesses", {})


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestDryRun:
    """Tests for dry-run mode."""

    def test_install_dry_run_writes_nothing(self, fake_home, mock_prompts, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")

        codex_install.install()

        codex_dir = fake_home / ".codex"
        assert not (codex_dir / "config.toml").exists()
        assert not (codex_dir / "atatus-env.sh").exists()

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        assert not config_file.exists()

    def test_dry_run_uninstall_preserves_files(self, fake_home, mock_prompts, monkeypatch):
        codex_install.install()

        toml_path = fake_home / ".codex" / "config.toml"
        env_path = fake_home / ".codex" / "atatus-env.sh"
        assert toml_path.is_file()
        assert env_path.is_file()

        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        codex_install.uninstall()

        assert toml_path.is_file()
        assert env_path.is_file()


# ---------------------------------------------------------------------------
# Env file heuristic tests
# ---------------------------------------------------------------------------


class TestEnvFileHeuristic:
    """Tests for _is_our_env_file()."""

    def test_recognizes_our_file(self, tmp_path):
        p = tmp_path / "atatus-env.sh"
        p.write_text("export ATATUS_TRACE_ENABLED=true\nexport ATATUS_CODEX_BUFFER_PORT=4318\n")
        assert codex_install._is_our_env_file(p) is True

    def test_rejects_foreign_file(self, tmp_path):
        p = tmp_path / "atatus-env.sh"
        p.write_text("#!/bin/bash\necho hello\nexport SOMETHING=else\n")
        assert codex_install._is_our_env_file(p) is False

    def test_rejects_large_file(self, tmp_path):
        p = tmp_path / "atatus-env.sh"
        lines = [f"export ATATUS_VAR_{i}=val" for i in range(20)]
        p.write_text("\n".join(lines) + "\n")
        assert codex_install._is_our_env_file(p) is False

    def test_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent"
        assert codex_install._is_our_env_file(p) is False


# ---------------------------------------------------------------------------
# _codex_toml_apply / _codex_toml_remove unit tests
# ---------------------------------------------------------------------------


class TestTomlApplyRemove:
    """Unit tests for the notify-only TOML mutators."""

    def _apply(self, p: Path) -> None:
        codex_install._codex_toml_apply(p, "/venv/bin/notify")

    def test_apply_to_empty_file(self, tmp_path):
        p = tmp_path / "config.toml"
        self._apply(p)
        data = codex_toml._toml_load(p)
        assert data["notify"] == ["/venv/bin/notify"]
        assert "hooks" not in data

    def test_apply_idempotent(self, tmp_path):
        p = tmp_path / "config.toml"
        self._apply(p)
        self._apply(p)
        data = codex_toml._toml_load(p)
        assert data["notify"] == ["/venv/bin/notify"]

    def test_apply_preserves_existing_notify(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('notify = ["/usr/bin/other-hook"]\n')
        self._apply(p)
        data = codex_toml._toml_load(p)
        assert "/usr/bin/other-hook" in data["notify"]
        assert "/venv/bin/notify" in data["notify"]

    def test_apply_preserves_unrelated_sections(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[model]\nname = "gpt-4"\n')
        self._apply(p)
        data = codex_toml._toml_load(p)
        assert data["model"]["name"] == "gpt-4"
        assert "notify" in data

    def test_apply_leaves_existing_hook_entries_alone(self, tmp_path):
        """apply does not touch pre-existing [[hooks.<Event>]] entries."""
        p = tmp_path / "config.toml"
        p.write_text(
            "[[hooks.PreToolUse]]\n" "hooks = [{ type = 'command', command = '/venv/bin/atatus-hook-codex-tool' }]\n"
        )
        self._apply(p)
        data = codex_toml._toml_load(p)
        assert data["notify"] == ["/venv/bin/notify"]
        cmds = _hook_commands(data, "PreToolUse")
        assert "/venv/bin/atatus-hook-codex-tool" in cmds

    def test_apply_dry_run_no_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        p = tmp_path / "config.toml"
        self._apply(p)
        assert not p.exists()

    def test_remove_only_our_notify(self, tmp_path):
        p = tmp_path / "config.toml"
        self._apply(p)
        data = codex_toml._toml_load(p)
        data["notify"].append("/usr/bin/other")
        codex_toml._toml_write(data, p)

        codex_install._codex_toml_remove(p, "/venv/bin/notify")
        remaining = codex_toml._toml_load(p)
        assert remaining["notify"] == ["/usr/bin/other"]

    def test_remove_strips_legacy_hook_entries(self, tmp_path):
        """remove strips both our notify entry and any leftover atatus-managed hooks."""
        p = tmp_path / "config.toml"
        p.write_text(
            'notify = ["/venv/bin/notify"]\n'
            "[[hooks.PreToolUse]]\n"
            "hooks = [{ type = 'command', command = '/venv/bin/atatus-hook-codex-tool' }]\n"
            "[[hooks.SessionStart]]\n"
            "hooks = [{ type = 'command', command = '/venv/bin/atatus-hook-codex-session' }]\n"
        )
        codex_install._codex_toml_remove(p, "/venv/bin/notify")
        remaining = codex_toml._toml_load(p)
        assert "notify" not in remaining
        assert "hooks" not in remaining

    def test_remove_nonexistent_file_is_noop(self, tmp_path):
        p = tmp_path / "nonexistent.toml"
        codex_install._codex_toml_remove(p, "/venv/bin/notify")
        assert not p.exists()

    def test_remove_dry_run_no_write(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        self._apply(p)
        original = p.read_text()
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        codex_install._codex_toml_remove(p, "/venv/bin/notify")
        assert p.read_text() == original


# ---------------------------------------------------------------------------
# TOML edge case tests
# ---------------------------------------------------------------------------


class TestTomlEdgeCases:
    """Edge cases for TOML parser/writer."""

    def test_boolean_roundtrip(self, tmp_path):
        p = tmp_path / "test.toml"
        codex_toml._toml_write({"flag": True, "other": False}, p)
        data = codex_toml._toml_line_parse(p.read_text())
        assert data["flag"] is True
        assert data["other"] is False

    def test_integer_roundtrip(self, tmp_path):
        p = tmp_path / "test.toml"
        codex_toml._toml_write({"port": 4318}, p)
        data = codex_toml._toml_line_parse(p.read_text())
        assert data["port"] == 4318

    def test_empty_array(self, tmp_path):
        p = tmp_path / "test.toml"
        codex_toml._toml_write({"notify": []}, p)
        text = p.read_text()
        assert "notify = []" in text
        data = codex_toml._toml_line_parse(text)
        assert data["notify"] == []

    def test_comments_ignored_in_parse(self):
        text = '# comment\nkey = "val"\n'
        data = codex_toml._toml_line_parse(text)
        assert data["key"] == "val"


# ---------------------------------------------------------------------------
# Write env file tests
# ---------------------------------------------------------------------------


class TestWriteEnvFile:
    """Tests for _write_env_file."""

    def test_env_file_permissions(self, tmp_path):
        p = tmp_path / "env.sh"
        codex_install._write_env_file(p)
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_env_file_without_user_id(self, tmp_path):
        p = tmp_path / "env.sh"
        codex_install._write_env_file(p)
        text = p.read_text()
        assert "ATATUS_USER_ID" not in text
        assert "ATATUS_TRACE_ENABLED=true" in text

    def test_env_file_with_user_id(self, tmp_path):
        p = tmp_path / "env.sh"
        codex_install._write_env_file(p, user_id="alice")
        text = p.read_text()
        assert "export ATATUS_USER_ID=alice" in text

    def test_env_file_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "subdir" / "env.sh"
        codex_install._write_env_file(p)
        assert p.is_file()

    def test_env_file_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        p = tmp_path / "env.sh"
        codex_install._write_env_file(p)
        assert not p.exists()


# ---------------------------------------------------------------------------
# core/setup/codex.py delegation tests
# ---------------------------------------------------------------------------


class TestCoreSetupDelegation:
    """Test that core/setup/codex.py delegates to tracing.codex/install.py."""

    def test_install_delegates(self, fake_home, mock_prompts):
        import core.setup.codex as setup_codex

        mock_mod = MagicMock()
        with patch.object(setup_codex, "_install_mod", mock_mod):
            setup_codex.install(with_skills=True)
            mock_mod.install.assert_called_once_with(with_skills=True)

    def test_uninstall_delegates(self, fake_home):
        import core.setup.codex as setup_codex

        mock_mod = MagicMock()
        with patch.object(setup_codex, "_install_mod", mock_mod):
            setup_codex.uninstall()
            mock_mod.uninstall.assert_called_once()


# ---------------------------------------------------------------------------
# CLI dispatch tests
# ---------------------------------------------------------------------------


class TestCLIDispatch:
    """Tests for cli_main() dispatch logic."""

    def test_cli_install(self, fake_home, mock_prompts):
        with patch.object(codex_install, "install") as m:
            codex_install.cli_main(["install.py", "install"])
            m.assert_called_once_with(with_skills=False)

    def test_cli_install_with_skills(self, fake_home, mock_prompts):
        with patch.object(codex_install, "install") as m:
            codex_install.cli_main(["install.py", "install", "--with-skills"])
            m.assert_called_once_with(with_skills=True)

    def test_cli_uninstall(self, fake_home):
        with patch.object(codex_install, "uninstall") as m:
            codex_install.cli_main(["install.py", "uninstall"])
            m.assert_called_once()

    def test_cli_invalid_action_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            codex_install.cli_main(["install.py", "bogus"])
        assert exc_info.value.code == 1

    def test_cli_no_args_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            codex_install.cli_main(["install.py"])
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TOML fallback quoting tests
# ---------------------------------------------------------------------------


class TestTomlFallbackQuoting:
    """Tests for quote-aware TOML fallback parser/writer."""

    def test_unkey_roundtrips_through_key(self):
        inputs = ["plain", "with.dot", "with@at", "with/slash", 'with"quote', "with\\backslash", "@scope/server"]
        for s in inputs:
            assert codex_toml._toml_unkey(codex_toml._toml_key(s)) == s, f"roundtrip failed for {s!r}"

    def test_split_key_path_respects_quotes(self):
        cases = [
            ("a.b.c", ["a", "b", "c"]),
            ('mcp_servers."@scope/server"', ["mcp_servers", "@scope/server"]),
            ('plugins."browser-use@openai-bundled"', ["plugins", "browser-use@openai-bundled"]),
            ('mcp_servers."a.b.c"', ["mcp_servers", "a.b.c"]),
            ('  outer . "inner.path"  ', ["outer", "inner.path"]),
        ]
        for path, expected in cases:
            assert codex_toml._toml_split_key_path(path) == expected, f"split failed for {path!r}"

    def test_fallback_roundtrips_quoted_section_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tracing.codex._toml._tomllib", None)
        toml_text = textwrap.dedent("""\
            [mcp_servers."@scope/server"]
            command = "npx"
            args = ["-y", "@scope/server"]
        """)
        p = tmp_path / "config.toml"
        p.write_text(toml_text)

        data = codex_toml._toml_load(p)
        assert data == {"mcp_servers": {"@scope/server": {"command": "npx", "args": ["-y", "@scope/server"]}}}

        p2 = tmp_path / "config2.toml"
        codex_toml._toml_write(data, p2)
        data2 = codex_toml._toml_load(p2)
        assert data2 == data

    def test_fallback_repairs_malformed_unquoted_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tracing.codex._toml._tomllib", None)
        toml_text = textwrap.dedent("""\
            [plugins.@scope/server]
            enabled = true
        """)
        p = tmp_path / "config.toml"
        p.write_text(toml_text)

        data = codex_toml._toml_load(p)
        assert data == {"plugins": {"@scope/server": {"enabled": True}}}

        p2 = tmp_path / "config2.toml"
        codex_toml._toml_write(data, p2)
        rewritten = p2.read_text()
        assert '[plugins."@scope/server"]' in rewritten

        tomllib = pytest.importorskip("tomllib")
        strict_data = tomllib.loads(rewritten)
        assert strict_data == data

    def test_split_key_path_leading_and_trailing_dots(self):
        assert codex_toml._toml_split_key_path("a.") == ["a", ""]
        assert codex_toml._toml_split_key_path(".b") == ["", "b"]
        assert codex_toml._toml_split_key_path(".") == ["", ""]

    def test_split_key_path_empty_string(self):
        assert codex_toml._toml_split_key_path("") == [""]

    def test_split_key_path_single_bare_key(self):
        assert codex_toml._toml_split_key_path("server") == ["server"]

    def test_split_key_path_single_quoted_key(self):
        assert codex_toml._toml_split_key_path('"@scope/server"') == ["@scope/server"]

    def test_unkey_roundtrip_backslash_and_quote(self):
        s = 'back\\and"quote'
        assert codex_toml._toml_unkey(codex_toml._toml_key(s)) == s

    def test_unkey_bare_key_passthrough(self):
        assert codex_toml._toml_unkey("simple-key_0") == "simple-key_0"

    def test_fallback_deeply_nested_quoted_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tracing.codex._toml._tomllib", None)
        toml_text = textwrap.dedent("""\
            [a."b.c"."d/e"]
            x = 1
        """)
        p = tmp_path / "deep.toml"
        p.write_text(toml_text)

        data = codex_toml._toml_load(p)
        assert data == {"a": {"b.c": {"d/e": {"x": 1}}}}

        p2 = tmp_path / "deep2.toml"
        codex_toml._toml_write(data, p2)
        data2 = codex_toml._toml_load(p2)
        assert data2 == data

    def test_fallback_multiple_sections_with_quoted_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tracing.codex._toml._tomllib", None)
        toml_text = textwrap.dedent("""\
            [servers."@org/alpha"]
            port = 8080

            [servers."@org/beta"]
            port = 9090
        """)
        p = tmp_path / "multi.toml"
        p.write_text(toml_text)

        data = codex_toml._toml_load(p)
        assert data == {"servers": {"@org/alpha": {"port": 8080}, "@org/beta": {"port": 9090}}}

        p2 = tmp_path / "multi2.toml"
        codex_toml._toml_write(data, p2)
        data2 = codex_toml._toml_load(p2)
        assert data2 == data

    def test_toml_key_idempotent_for_bare_keys(self):
        for bare in ["simple", "with-dash", "with_under", "CamelCase", "num123"]:
            assert codex_toml._toml_key(bare) == bare

    def test_toml_key_quotes_special_chars(self):
        assert codex_toml._toml_key("a.b") == '"a.b"'
        assert codex_toml._toml_key("@scope") == '"@scope"'
        assert codex_toml._toml_key("a/b") == '"a/b"'

    def test_written_toml_valid_for_strict_parser(self, tmp_path):
        tomllib = pytest.importorskip("tomllib")

        data = {
            "mcp_servers": {
                "@anthropic/server": {"command": "run", "args": ["--flag"]},
                "normal-server": {"command": "exec"},
            },
            "projects": {"/Users/someone/proj": {"enabled": True}},
        }
        p = tmp_path / "out.toml"
        codex_toml._toml_write(data, p)
        text = p.read_text()
        parsed = tomllib.loads(text)
        assert parsed == data


# ---------------------------------------------------------------------------
# CODEX_HOME
# ---------------------------------------------------------------------------


class TestGetCodexHome:
    """`get_codex_home()` mirrors Codex's own CODEX_HOME resolution."""

    def test_defaults_to_dot_codex_under_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert get_codex_home() == tmp_path / ".codex"

    def test_empty_value_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", "")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert get_codex_home() == tmp_path / ".codex"

    @pytest.mark.parametrize("raw", ["~/custom-codex", "$HOME/custom-codex"], ids=["tilde", "env-var"])
    def test_expands_like_a_shell(self, tmp_path, monkeypatch, raw):
        target = tmp_path / "custom-codex"
        target.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CODEX_HOME", raw)
        assert get_codex_home() == target.resolve()

    def test_missing_directory_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope"))
        with pytest.raises(ValueError, match="invalid path"):
            get_codex_home()

    def test_file_instead_of_directory_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "afile"
        f.write_text("")
        monkeypatch.setenv("CODEX_HOME", str(f))
        with pytest.raises(ValueError, match="non-directory"):
            get_codex_home()


class TestInstallHonorsCodexHome:
    """An invalid override must never fall through to the default profile."""

    def test_install_writes_into_codex_home(self, fake_home, mock_prompts, tmp_path, monkeypatch):
        alt = tmp_path / "alt-codex"
        alt.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(alt))

        codex_install.install()

        assert (alt / "config.toml").is_file()
        assert (alt / "atatus-env.sh").is_file()
        # The default profile must be left completely alone.
        assert not (fake_home / ".codex" / "config.toml").exists()

    def test_uninstall_reverts_inside_codex_home(self, fake_home, mock_prompts, tmp_path, monkeypatch):
        alt = tmp_path / "alt-codex"
        alt.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(alt))

        codex_install.install()
        codex_install.uninstall()

        data = codex_toml._toml_load(alt / "config.toml")
        assert "notify" not in data
        assert not (alt / "atatus-env.sh").exists()

    def test_install_aborts_on_invalid_codex_home(self, fake_home, mock_prompts, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "does-not-exist"))

        with pytest.raises(ValueError):
            codex_install.install()

        # Nothing was written to the default profile as a consolation prize.
        assert not (fake_home / ".codex" / "config.toml").exists()


class TestStrictTomlGuard:
    """A config.toml we cannot fully parse must be left byte-for-byte alone."""

    MALFORMED = "notify = [\nthis is not toml\n"

    def test_strict_loader_raises_where_lenient_one_guesses(self, tmp_path):
        pytest.importorskip("tomllib")
        p = tmp_path / "config.toml"
        p.write_text(self.MALFORMED)

        # The lenient parser still returns something — that is exactly the risk.
        assert isinstance(codex_toml._toml_load(p), dict)
        with pytest.raises(ValueError, match="not valid TOML"):
            codex_toml._toml_load_strict(p)

    def test_strict_loader_accepts_missing_file(self, tmp_path):
        assert codex_toml._toml_load_strict(tmp_path / "absent.toml") == {}

    def test_install_does_not_rewrite_malformed_config(self, fake_home, mock_prompts):
        pytest.importorskip("tomllib")
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        toml_path = codex_dir / "config.toml"
        toml_path.write_text(self.MALFORMED)

        with pytest.raises(ValueError, match="not valid TOML"):
            codex_install.install()

        assert toml_path.read_text() == self.MALFORMED
