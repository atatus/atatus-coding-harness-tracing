#!/usr/bin/env python3
"""Tests for core/setup/ — shared utilities and per-harness setup wizards."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run every test under tmp_path so cwd-relative writes (e.g. .github/hooks
    from copilot install) don't leak into the project directory."""
    monkeypatch.chdir(tmp_path)


def _patched_path_class(tmp_path):
    """Create a Path subclass that redirects home() and relative .claude/ to tmp_path."""
    _real_path = Path

    class _FakePath(_real_path):
        @classmethod
        def home(cls):
            return _real_path(tmp_path)

        def __new__(cls, *args, **kwargs):
            # Redirect ".claude/..." to tmp_path/.claude/...
            if args and str(args[0]).startswith(".claude"):
                return _real_path(tmp_path / args[0])
            return _real_path.__new__(cls, *args, **kwargs)

    return _FakePath


# ---------------------------------------------------------------------------
# Shared utility tests (core.setup.__init__)
# ---------------------------------------------------------------------------


class TestPrintColor:
    """Tests for print_color()."""

    def test_no_color_when_not_tty(self, capsys):
        """print_color with non-tty stdout should not emit ANSI codes."""
        from core.setup import print_color

        with patch.object(sys.stdout, "isatty", return_value=False):
            print_color("hello", "green")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    def test_no_color_with_empty_color(self, capsys):
        """print_color with no color arg should not emit ANSI codes."""
        from core.setup import print_color

        print_color("hello")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    def test_no_color_with_invalid_color(self, capsys):
        """print_color with unrecognized color should not emit ANSI codes."""
        from core.setup import print_color

        print_color("hello", "magenta")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    @pytest.mark.skipif(os.name == "nt", reason="ANSI color tests only on Unix")
    def test_color_when_tty(self, capsys):
        """print_color with tty stdout should emit ANSI codes."""
        from core.setup import print_color

        with patch.object(sys.stdout, "isatty", return_value=True):
            print_color("hello", "green")
        out = capsys.readouterr().out
        assert "\033[0;32m" in out
        assert "\033[0m" in out
        assert "hello" in out


class TestPromptBackend:
    """Tests for prompt_backend().

    There is a single backend now, so the wizard asks only for the licence key
    and an optional endpoint override — no backend-selection menu.
    """

    def test_default_endpoint(self):
        """Blank endpoint input falls back to the default collector."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=[""]):
            with patch("core.setup.getpass", return_value="lic-key"):
                target, creds = prompt_backend()
        assert target == "atatus"
        assert creds["endpoint"] == "https://otel-rx.atatus.com"
        assert creds["api_key"] == "lic-key"

    def test_custom_endpoint(self):
        """An explicit endpoint is used verbatim."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["https://otel.mycorp.internal"]):
            with patch("core.setup.getpass", return_value="lic-key"):
                target, creds = prompt_backend()
        assert target == "atatus"
        assert creds["endpoint"] == "https://otel.mycorp.internal"
        assert creds["api_key"] == "lic-key"

    def test_missing_api_key_exits(self):
        """An empty licence key is fatal — there is nothing to authenticate with."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=[""]):
            with patch("core.setup.getpass", return_value=""):
                with pytest.raises(SystemExit):
                    prompt_backend()

    def test_key_is_read_through_getpass(self):
        """The licence key must never echo to the terminal."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=[""]) as mock_input:
            with patch("core.setup.getpass", return_value="secret") as mock_getpass:
                target, creds = prompt_backend()
        assert mock_getpass.call_count == 1
        assert creds["api_key"] == "secret"
        # the endpoint prompt is the only plain input
        assert mock_input.call_count == 1


class TestPromptProjectName:
    """The user-supplied name is the grouping key, so a fresh install
    must not silently default. A re-install may confirm the stored name."""

    def test_fresh_install_rejects_blank(self):
        from core.setup import prompt_project_name

        with patch("builtins.input", side_effect=["", "", ""]):
            with pytest.raises(SystemExit):
                prompt_project_name()

    def test_fresh_install_reprompts_then_accepts(self):
        from core.setup import prompt_project_name

        with patch("builtins.input", side_effect=["", "  ", "team-payments"]):
            assert prompt_project_name() == "team-payments"

    def test_reinstall_blank_confirms_stored_name(self):
        from core.setup import prompt_project_name

        with patch("builtins.input", side_effect=[""]):
            assert prompt_project_name("existing-project") == "existing-project"

    def test_reinstall_can_be_overridden(self):
        from core.setup import prompt_project_name

        with patch("builtins.input", side_effect=["renamed"]):
            assert prompt_project_name("existing-project") == "renamed"


class TestPromptContentLogging:
    """Prompts and tool *details* captured, tool *output* not.

    Regression suite for the 2026-08-07 bug — the wizard prompted `[Y/n]` for
    tool content and wrote every answer explicitly, so pressing Enter stored
    `"tool_content": true`, which outranks the code default in
    `_resolve_log_flag`. The privacy posture was defeated on every installed
    machine while `core/common.py` looked correct. These tests assert the two
    properties that together prevent a recurrence: the blank-line answer must
    equal the default, and a defaulted answer must not be persisted.
    """

    @staticmethod
    def _run(answers):
        from core.setup import prompt_content_logging

        with patch("builtins.input", side_effect=answers):
            with patch.object(sys.stdout, "isatty", return_value=False):
                return prompt_content_logging()

    @staticmethod
    def _flags(block):
        """The block minus its version stamp — i.e. what was actually persisted."""
        return {k: v for k, v in block.items() if k != "_v"}

    def test_all_defaults_accepted_persists_no_flags(self):
        """🔴 The exact bug. Three blank lines must store no flag at all."""
        assert self._flags(self._run(["", "", ""])) == {}

    def test_every_result_carries_the_version_stamp(self):
        from core.common import LOG_CONFIG_VERSION

        for answers in (["", "", ""], ["n", "no", "y"]):
            assert self._run(answers)["_v"] == LOG_CONFIG_VERSION

    def test_defaults_are_the_adr011_posture(self):
        from core.common import LOG_FLAG_DEFAULTS

        assert LOG_FLAG_DEFAULTS == {
            "prompts": True,
            "tool_details": True,
            "tool_content": False,
        }

    def test_tool_content_requires_explicit_yes(self):
        assert self._flags(self._run(["", "", "y"])) == {"tool_content": True}
        assert self._flags(self._run(["", "", "yes"])) == {"tool_content": True}

    def test_tool_content_junk_answer_stays_off(self):
        """A False default must not be flipped by anything but yes/y — the
        old `not in ("n", "no")` parse turned every typo into an opt-in."""
        for junk in ("sure", "1", "true", "yep", "n"):
            assert self._flags(self._run(["", "", junk])) == {}, f"{junk!r} must not opt in"

    def test_surrounding_whitespace_is_ignored(self):
        assert self._flags(self._run(["", "", " y "])) == {"tool_content": True}
        assert self._flags(self._run([" n ", "", ""])) == {"prompts": False}

    def test_opting_out_of_a_true_default_is_persisted(self):
        assert self._flags(self._run(["n", "", ""])) == {"prompts": False}
        assert self._flags(self._run(["", "no", ""])) == {"tool_details": False}

    def test_only_deviations_are_returned(self):
        assert self._flags(self._run(["n", "no", "y"])) == {
            "prompts": False,
            "tool_details": False,
            "tool_content": True,
        }

    def test_answers_are_case_insensitive(self):
        assert self._flags(self._run(["N", "", "Y"])) == {
            "prompts": False,
            "tool_content": True,
        }

    def test_reinstall_accepting_defaults_clears_a_stored_override(self, tmp_path, monkeypatch):
        """End-to-end repair path for machines already carrying the bad config.

        write_logging_config replaces the block, so returning only the stamp is
        what wipes a previously-stored `tool_content: true`.
        """
        import json

        from core.common import LOG_CONFIG_VERSION
        from core.setup import write_logging_config

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"user_id": "dg", "logging": {"prompts": True, "tool_content": True}}))
        monkeypatch.setattr("core.setup.dry_run", lambda: False)

        write_logging_config(self._run(["", "", ""]), str(config_path))

        written = json.loads(config_path.read_text())
        assert written["logging"] == {"_v": LOG_CONFIG_VERSION}
        assert written["user_id"] == "dg", "unrelated keys must survive"


class TestNeedsContentLoggingPrompt:
    """The migration gate. Installers skip the wizard once a block exists, so
    this predicate is the only thing that can repair a v1 machine."""

    def test_missing_config_prompts(self):
        from core.setup import needs_content_logging_prompt

        assert needs_content_logging_prompt(None) is True
        assert needs_content_logging_prompt({}) is True

    def test_v1_block_is_reprompted(self):
        """🔴 The repair path. A pre-version block carrying the bad override
        must re-prompt, or the machine keeps `tool_content: true` forever."""
        from core.setup import needs_content_logging_prompt

        v1 = {"logging": {"prompts": True, "tool_details": True, "tool_content": True}}
        assert needs_content_logging_prompt(v1) is True

    def test_current_version_is_not_reprompted(self):
        from core.common import LOG_CONFIG_VERSION
        from core.setup import needs_content_logging_prompt

        current = {"logging": {"_v": LOG_CONFIG_VERSION, "tool_content": True}}
        assert needs_content_logging_prompt(current) is False

    def test_non_dict_logging_value_prompts(self):
        """Fail-safe: a corrupt block re-prompts rather than crashing."""
        from core.setup import needs_content_logging_prompt

        for junk in ("yes", 1, [], True):
            assert needs_content_logging_prompt({"logging": junk}) is True


class TestPromptUserId:
    """Tests for prompt_user_id()."""

    def test_returns_user_id(self):
        from core.setup import prompt_user_id

        with patch("builtins.input", return_value="alice"):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = prompt_user_id()
        assert result == "alice"

    def test_returns_empty_when_skipped(self):
        from core.setup import prompt_user_id

        with patch("builtins.input", return_value=""):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = prompt_user_id()
        assert result == ""


class TestWriteConfig:
    """Tests for write_config()."""

    def test_creates_new_config_atatus(self, tmp_path, monkeypatch):
        """write_config creates fresh config.json for Atatus."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "atatus",
            {"endpoint": "https://otel-rx.atatus.com", "api_key": "k"},
            "codex",
            "codex",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        entry = config["harnesses"]["codex"]
        assert entry["target"] == "atatus"
        assert entry["api_key"] == "k"
        assert entry["project_name"] == "codex"
        assert "backend" not in config

    def test_merge_harness_preserves_existing(self, tmp_path, monkeypatch):
        """write_config with existing config adds harness, preserves others."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        # Pre-existing config in new flat format
        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "atatus",
                    "endpoint": "http://custom:9999",
                    "api_key": "secret",
                }
            }
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        from core.setup import write_config

        write_config(
            "atatus",
            {"endpoint": "https://otel-rx.atatus.com", "api_key": ""},
            "cursor",
            "cursor",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        # New harness should be added
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["cursor"]["target"] == "atatus"
        # Old harness should be preserved
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert config["harnesses"]["claude-code"]["endpoint"] == "http://custom:9999"

    def test_write_config_with_user_id(self, tmp_path, monkeypatch):
        """write_config sets user_id when provided."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "atatus",
            {"endpoint": "https://otel-rx.atatus.com", "api_key": ""},
            "claude-code",
            "claude-code",
            user_id="alice",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        assert config["user_id"] == "alice"


# ---------------------------------------------------------------------------
# Claude setup tests (core.setup.claude)
# ---------------------------------------------------------------------------


class TestClaudeSetup:
    """Tests for core.setup.claude."""

    def test_settings_json_atatus(self, tmp_path):
        """Claude setup creates settings.json with Atatus env block."""
        settings_path = tmp_path / ".claude" / "settings.local.json"

        from core.setup.claude import _ensure_settings_file, _load_settings, _save_settings

        _ensure_settings_file(settings_path)
        settings = _load_settings(settings_path)
        env_block = settings.setdefault("env", {})
        env_block["ATATUS_API_KEY"] = "test-key"
        env_block["ATATUS_OTLP_ENDPOINT"] = "https://otel-rx.atatus.com"
        env_block["ATATUS_TRACE_ENABLED"] = "true"
        _save_settings(settings_path, settings)

        result = json.loads(settings_path.read_text())
        assert result["env"]["ATATUS_API_KEY"] == "test-key"
        assert result["env"]["ATATUS_OTLP_ENDPOINT"] == "https://otel-rx.atatus.com"
        assert result["env"]["ATATUS_TRACE_ENABLED"] == "true"

    def test_existing_settings_merged(self, tmp_path):
        """Existing settings.json keys are preserved when adding env block."""
        settings_path = tmp_path / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"theme": "dark", "env": {"EXISTING_VAR": "keep_me"}}))

        from core.setup.claude import _load_settings, _save_settings

        settings = _load_settings(settings_path)
        env_block = settings.setdefault("env", {})
        env_block["ATATUS_OTLP_ENDPOINT"] = "https://otel-rx.atatus.com"
        _save_settings(settings_path, settings)

        result = json.loads(settings_path.read_text())
        assert result["theme"] == "dark"
        assert result["env"]["EXISTING_VAR"] == "keep_me"
        assert result["env"]["ATATUS_OTLP_ENDPOINT"] == "https://otel-rx.atatus.com"

    def test_check_existing_config_no_overwrite(self, tmp_path):
        """Declining overwrite returns False."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"ATATUS_OTLP_ENDPOINT": "https://otel-rx.atatus.com"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="n"):
            result = _check_existing_configuration(settings_path)
        assert result is False

    def test_check_existing_config_overwrite(self, tmp_path):
        """Accepting overwrite returns True."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"ATATUS_OTLP_ENDPOINT": "https://otel-rx.atatus.com"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="y"):
            result = _check_existing_configuration(settings_path)
        assert result is True

    def test_check_existing_config_atatus_no_overwrite(self, tmp_path):
        """Declining overwrite for Atatus config returns False."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"ATATUS_API_KEY": "some-key"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="N"):
            result = _check_existing_configuration(settings_path)
        assert result is False

    def test_check_no_existing_config(self, tmp_path):
        """No existing config returns True (proceed)."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")

        from core.setup.claude import _check_existing_configuration

        result = _check_existing_configuration(settings_path)
        assert result is True

    def test_load_settings_missing_file(self, tmp_path):
        """_load_settings returns {} for missing file."""
        from core.setup.claude import _load_settings

        result = _load_settings(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_settings_invalid_json(self, tmp_path):
        """_load_settings returns {} for invalid JSON."""
        path = tmp_path / "bad.json"
        path.write_text("not json{{{")
        from core.setup.claude import _load_settings

        result = _load_settings(path)
        assert result == {}

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.claude import main

        with patch("core.setup.claude._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.claude import main

        with patch("core.setup.claude._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def _setup_install_env(self, tmp_path, monkeypatch):
        """Set up the environment so _run() → install() can resolve all paths."""
        import core.config
        import core.setup as setup_mod

        install_dir = tmp_path / ".atatus" / "harness"
        config_path = install_dir / "config.json"

        monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
        monkeypatch.setattr(setup_mod, "VENV_DIR", install_dir / "venv")
        monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_path)
        monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
        monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
        monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
        monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")
        monkeypatch.setattr(core.config, "CONFIG_FILE", str(config_path))

        # Create the harness plugin dir so harness_dir() resolves
        plugin_dir = install_dir / "tracing" / "claude_code"
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # Patch SETTINGS_FILE in install module
        settings_file = tmp_path / ".claude" / "settings.json"
        import tracing.claude_code.constants as claude_constants
        import tracing.claude_code.install as claude_install

        monkeypatch.setattr(claude_install, "SETTINGS_FILE", settings_file)
        monkeypatch.setattr(claude_constants, "SETTINGS_FILE", settings_file)

        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
            )(),
        )

        return config_path, settings_file

    def test_run_atatus_flow(self, tmp_path, monkeypatch):
        """Full Claude _run() flow for Atatus backend writes settings.json and config.json."""
        config_path, settings_file = self._setup_install_env(tmp_path, monkeypatch)

        # Inputs: endpoint=default, project_name (REQUIRED on a fresh install --
        # a blank is rejected), user_id="", then three content-logging
        # prompts (defaults: Y, N, N). The licence key comes from getpass.
        inputs = iter(["", "my-project", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "lic-key")

        from core.setup.claude import _run

        _run()

        config = json.loads(config_path.read_text())
        assert config["harnesses"]["claude-code"]["target"] == "atatus"
        assert config["harnesses"]["claude-code"]["project_name"] == "my-project"

        # settings.json should have hooks and env vars
        result = json.loads(settings_file.read_text())
        assert result["env"]["ATATUS_TRACE_ENABLED"] == "true"
        assert result["env"]["ATATUS_PROJECT_NAME"] == "my-project"
        assert len(result.get("hooks", {})) == 16


# ---------------------------------------------------------------------------
# Codex setup tests (core.setup.codex)
# ---------------------------------------------------------------------------


class TestCodexWriteEnvFile:
    """Tests for _write_env_file()."""

    def test_atatus_env_file(self, tmp_path):
        """Env file for Atatus backend has correct exports."""
        env_path = tmp_path / ".codex" / "atatus-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "atatus", {"endpoint": "https://otel-rx.atatus.com", "api_key": ""})

        content = env_path.read_text()
        assert "export ATATUS_TRACE_ENABLED=true" in content
        assert 'export ATATUS_OTLP_ENDPOINT="https://otel-rx.atatus.com"' in content
        assert "ATATUS_API_KEY" not in content  # empty api_key should be skipped
        assert 'export ATATUS_PROJECT_NAME="codex"' in content

    def test_atatus_env_file_with_api_key(self, tmp_path):
        """Env file for Atatus with API key includes it."""
        env_path = tmp_path / ".codex" / "atatus-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "atatus", {"endpoint": "https://otel-rx.atatus.com", "api_key": "my-key"})

        content = env_path.read_text()
        assert 'export ATATUS_API_KEY="my-key"' in content

    def test_env_file_creates_parent_dir(self, tmp_path):
        """_write_env_file creates parent directories."""
        env_path = tmp_path / "deep" / "nested" / "atatus-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "atatus", {"endpoint": "https://otel-rx.atatus.com", "api_key": ""})
        assert env_path.exists()

    def test_env_file_permissions(self, tmp_path):
        """Env file should be chmod 600 on Unix."""
        if os.name == "nt":
            pytest.skip("chmod test only on Unix")
        env_path = tmp_path / ".codex" / "atatus-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "atatus", {"endpoint": "https://otel-rx.atatus.com", "api_key": ""})
        mode = oct(env_path.stat().st_mode & 0o777)
        assert mode == "0o600"


class TestCodexRunFlow:
    """Integration tests for codex _run() flow."""

    def test_run_fresh_atatus(self, tmp_path, monkeypatch):
        """Codex _run() with no existing config prompts and writes all files."""
        config_path = str(tmp_path / "config.json")
        codex_dir = tmp_path / ".codex"

        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        # Patch Path.home() to use tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Inputs: project_name (required on fresh install), endpoint=default,
        # user_id="" (key via getpass)
        inputs = iter(["my-project", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "lic-key")
        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
            )(),
        )

        from core.setup.codex import _run

        _run()

        # config.json written
        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["codex"]["target"] == "atatus"
        assert config["harnesses"]["codex"]["project_name"] == "my-project"

        # atatus-env.sh written
        env_file = codex_dir / "atatus-env.sh"
        assert env_file.exists()
        env_content = env_file.read_text()
        assert "export ATATUS_TRACE_ENABLED=true" in env_content
        assert 'export ATATUS_OTLP_ENDPOINT="https://otel-rx.atatus.com"' in env_content

        # The wizard no longer touches ~/.codex/config.toml — spans are sent
        # straight to Atatus from the hooks, so there is no local collector to
        # point Codex's own OTLP exporter at.
        assert not (codex_dir / "config.toml").exists()

    def test_run_existing_config_skips_prompts(self, tmp_path, monkeypatch):
        """Codex _run() with existing config skips backend prompts."""
        config_path = str(tmp_path / "config.json")
        codex_dir = tmp_path / ".codex"
        existing = {
            "harnesses": {
                "codex": {
                    "project_name": "codex",
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "",
                    "collector": {"host": "127.0.0.1", "port": 4318},
                }
            }
        }
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Inputs: project_name=default, user_id="" (no backend prompts)
        inputs = iter(["", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
            )(),
        )

        from core.setup.codex import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["codex"]["project_name"] == "codex"
        assert config["harnesses"]["codex"]["target"] == "atatus"

        # env file is still written; config.toml is not touched
        assert (codex_dir / "atatus-env.sh").exists()
        assert not (codex_dir / "config.toml").exists()


# ---------------------------------------------------------------------------
# Cursor setup tests (core.setup.cursor)
# ---------------------------------------------------------------------------


class TestCursorSetup:
    """Tests for core.setup.cursor."""

    def test_config_written_with_cursor_harness(self, tmp_path, monkeypatch):
        """write_config creates config with cursor harness entry."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "atatus",
            {"endpoint": "https://otel-rx.atatus.com", "api_key": ""},
            "cursor",
            "cursor",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["cursor"]["target"] == "atatus"

    def test_existing_config_adds_cursor_harness(self, tmp_path, monkeypatch):
        """Existing config gets cursor harness added, other harnesses preserved."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "key",
                }
            }
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        config = core.config.load_config(config_path)
        core.config.set_value(config, "harnesses.cursor.project_name", "cursor")
        core.config.save_config(config, config_path)

        result = json.loads(Path(config_path).read_text())
        assert result["harnesses"]["cursor"]["project_name"] == "cursor"
        assert result["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert result["harnesses"]["claude-code"]["target"] == "atatus"
        assert result["harnesses"]["claude-code"]["api_key"] == "key"

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt."""
        from core.setup.cursor import main

        with patch("core.setup.cursor._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def _patch_cursor_install(self, tmp_path, monkeypatch):
        """Shared patching for cursor _run() tests — patches config and install module paths."""
        import core.config
        import core.setup as setup_mod

        config_path = str(tmp_path / "config.json")
        install_dir = tmp_path / ".atatus" / "harness"
        hooks_file = tmp_path / ".cursor" / "hooks.json"

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)
        monkeypatch.setattr(setup_mod, "CONFIG_FILE", Path(config_path))
        monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
        monkeypatch.setattr(setup_mod, "VENV_DIR", install_dir / "venv")
        monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
        monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
        monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
        monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")

        # Patch HOOKS_FILE + INSTALL_DIR in the cursor install module.
        import tracing.cursor.install as cursor_install

        monkeypatch.setattr(cursor_install, "HOOKS_FILE", hooks_file)
        monkeypatch.setattr(cursor_install, "INSTALL_DIR", install_dir)

        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
            )(),
        )

        return config_path

    def test_run_fresh_atatus(self, tmp_path, monkeypatch):
        """Cursor _run() with no existing config prompts and writes config.json."""
        config_path = self._patch_cursor_install(tmp_path, monkeypatch)

        # Inputs: endpoint=default, project_name (required on fresh install),
        # user_id="", then three content-logging prompts. Key via getpass.
        inputs = iter(["", "my-project", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "lic-key")

        from core.setup.cursor import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["target"] == "atatus"
        assert config["harnesses"]["cursor"]["project_name"] == "my-project"

    def test_run_existing_config_skips_prompts(self, tmp_path, monkeypatch):
        """Cursor _run() with existing cursor entry skips backend prompts."""
        config_path = self._patch_cursor_install(tmp_path, monkeypatch)
        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "k",
                },
                "cursor": {
                    "project_name": "cursor",
                    "target": "atatus",
                    "endpoint": "https://otel-rx.atatus.com",
                    "api_key": "k",
                },
            }
        }
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        # Inputs: project_name=default (no backend prompts since cursor entry exists),
        # then three content-logging prompts (defaults).
        inputs = iter(["", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        from core.setup.cursor import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert "backend" not in config


# ---------------------------------------------------------------------------
# Info/err helper tests
# ---------------------------------------------------------------------------


class TestInfoErr:
    """Tests for info() and err() helpers."""

    def test_info_non_tty(self, capsys):
        """info() on non-tty has no ANSI codes."""
        from core.setup import info

        with patch.object(sys.stdout, "isatty", return_value=False):
            info("test message")
        out = capsys.readouterr().out
        assert "[atatus] test message" in out
        assert "\033[" not in out

    def test_err_non_tty(self, capsys):
        """err() on non-tty has no ANSI codes."""
        from core.setup import err

        with patch.object(sys.stderr, "isatty", return_value=False):
            err("error message")
        captured = capsys.readouterr().err
        assert "[atatus] error message" in captured
        assert "\033[" not in captured


# ---------------------------------------------------------------------------
# Copilot setup tests (core.setup.copilot)
# ---------------------------------------------------------------------------


class TestCopilotSetup:
    """Tests for core.setup.copilot."""

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.copilot import main

        with patch("core.setup.copilot._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.copilot import main

        with patch("core.setup.copilot._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_run_delegates_to_installer(self):
        """_run() delegates to tracing.copilot/install.py install()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod._run()
            mock_mod.install.assert_called_once()

    def test_install_delegates_to_installer(self):
        """install() delegates to tracing.copilot/install.py install()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod.install()
            mock_mod.install.assert_called_once()

    def test_uninstall_delegates_to_installer(self):
        """uninstall() delegates to tracing.copilot/install.py uninstall()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod.uninstall()
            mock_mod.uninstall.assert_called_once()


# ---------------------------------------------------------------------------
# Gemini setup tests (core.setup.gemini)
# ---------------------------------------------------------------------------


class TestGeminiSetup:
    """Tests for core.setup.gemini."""

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_prints_cancelled_on_interrupt(self, capsys):
        """main() prints 'Setup cancelled.' on KeyboardInterrupt."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                main()
        assert "Setup cancelled." in capsys.readouterr().out

    def test_run_delegates_to_installer(self):
        """_run() delegates to tracing.gemini/install.py install()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod._run()
            mock_mod.install.assert_called_once()

    def test_install_delegates_to_installer(self):
        """install() delegates to tracing.gemini/install.py install()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod.install()
            mock_mod.install.assert_called_once()

    def test_uninstall_delegates_to_installer(self):
        """uninstall() delegates to tracing.gemini/install.py uninstall()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod.uninstall()
            mock_mod.uninstall.assert_called_once()


# ---------------------------------------------------------------------------
# Entry point registration tests
# ---------------------------------------------------------------------------


class TestEntryPoints:
    """Tests that entry points are properly defined in pyproject.toml."""

    def test_pyproject_has_setup_entry_points(self):
        """pyproject.toml defines all five setup wizard entry points."""
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        content = pyproject_path.read_text()
        assert 'atatus-setup-claude = "core.setup.claude:main"' in content
        assert 'atatus-setup-codex = "core.setup.codex:main"' in content
        assert 'atatus-setup-copilot = "core.setup.copilot:main"' in content
        assert 'atatus-setup-cursor = "core.setup.cursor:main"' in content
        assert 'atatus-setup-gemini = "core.setup.gemini:main"' in content

    def test_claude_main_is_callable(self):
        """core.setup.claude.main is importable and callable."""
        from core.setup.claude import main

        assert callable(main)

    def test_codex_main_is_callable(self):
        """core.setup.codex.main is importable and callable."""
        from core.setup.codex import main

        assert callable(main)

    def test_copilot_main_is_callable(self):
        """core.setup.copilot.main is importable and callable."""
        from core.setup.copilot import main

        assert callable(main)

    def test_cursor_main_is_callable(self):
        """core.setup.cursor.main is importable and callable."""
        from core.setup.cursor import main

        assert callable(main)

    def test_gemini_main_is_callable(self):
        """core.setup.gemini.main is importable and callable."""
        from core.setup.gemini import main

        assert callable(main)

    def test_gemini_install_uninstall_importable(self):
        """core.setup.gemini exports install and uninstall."""
        from core.setup.gemini import install, uninstall

        assert callable(install)
        assert callable(uninstall)
