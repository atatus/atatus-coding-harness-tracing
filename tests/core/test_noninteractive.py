#!/usr/bin/env python3
"""Tests for the non-interactive install path in core/setup.

Covers value resolution (dotenv file vs environment), the four shared prompts
under ``ATATUS_NONINTERACTIVE``, and the two guarantees that make the mode safe
to hand to an agent: no credential routing from an unvetted directory, and no
content capture nobody consented to.
"""

from __future__ import annotations

import pytest

import core.setup as setup


def _write_env_file(tmp_path, body: str):
    path = tmp_path / "creds.env"
    path.write_text(body)
    return path


def _no_prompts(monkeypatch):
    """Make any accidental prompt a loud failure rather than a hang."""

    def _boom(*a, **k):
        raise AssertionError("prompted in non-interactive mode")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(setup, "getpass", _boom)


# ---------------------------------------------------------------------------
# non_interactive()
# ---------------------------------------------------------------------------


class TestNonInteractiveFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", value)
        assert setup.non_interactive() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
    def test_everything_else_stays_interactive(self, monkeypatch, value):
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", value)
        assert setup.non_interactive() is False

    def test_unset_is_interactive(self):
        """Opt-in only: an exported ATATUS_API_KEY must not silently skip the wizard."""
        assert setup.non_interactive() is False


# ---------------------------------------------------------------------------
# Value resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_environment_is_used_when_no_file(self, monkeypatch):
        monkeypatch.setenv("ATATUS_API_KEY", "from-env")
        assert setup._env("ATATUS_API_KEY") == "from-env"
        assert setup._source_of("ATATUS_API_KEY") == "$ATATUS_API_KEY"

    def test_dotenv_file_beats_the_environment(self, tmp_path, monkeypatch):
        """An installed harness exports ATATUS_* into every session it spawns.

        Those inherited values must not beat a key the caller just wrote to the
        file they explicitly named.
        """
        path = _write_env_file(tmp_path, "ATATUS_API_KEY=from-file\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        monkeypatch.setenv("ATATUS_API_KEY", "stale-inherited")

        assert setup._env("ATATUS_API_KEY") == "from-file"
        assert setup._source_of("ATATUS_API_KEY") == str(path)

    def test_environment_fills_keys_the_file_omits(self, tmp_path, monkeypatch):
        path = _write_env_file(tmp_path, "ATATUS_API_KEY=from-file\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        monkeypatch.setenv("ATATUS_USER_ID", "dg")

        assert setup._env("ATATUS_API_KEY") == "from-file"
        assert setup._env("ATATUS_USER_ID") == "dg"

    def test_unreadable_env_file_is_fatal(self, tmp_path, monkeypatch):
        """A typo must not quietly install with whatever happened to be exported."""
        monkeypatch.setenv("ATATUS_ENV_FILE", str(tmp_path / "nope.env"))
        monkeypatch.setenv("ATATUS_API_KEY", "ambient")

        with pytest.raises(SystemExit):
            setup._env("ATATUS_API_KEY")

    def test_cwd_dotenv_is_never_read(self, tmp_path, monkeypatch):
        """A cloned repo must not get to choose where spans are shipped.

        File values outrank the environment, so an implicit ./.env search would
        let untrusted repo content supply ATATUS_OTLP_ENDPOINT while the user's
        real licence key came from their environment — shipping every later
        session, plus a bearer key, to an endpoint the repo picked.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("ATATUS_OTLP_ENDPOINT=https://attacker.example\n")
        (tmp_path / ".env.local").write_text("ATATUS_OTLP_ENDPOINT=https://attacker.example\n")

        assert setup._env("ATATUS_OTLP_ENDPOINT") == ""

    def test_only_known_keys_are_read(self, tmp_path, monkeypatch):
        """Pointing at an app's .env must not import unrelated settings."""
        path = _write_env_file(tmp_path, "ATATUS_API_KEY=k\nDATABASE_URL=postgres://secret\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))

        assert setup._dotenv_values() == {"ATATUS_API_KEY": "k"}

    def test_require_env_exits_with_a_named_variable(self, capsys):
        with pytest.raises(SystemExit):
            setup._require_env("ATATUS_API_KEY", "An Atatus licence key")
        assert "ATATUS_API_KEY" in capsys.readouterr().err


class TestEnvFlag:
    @pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("on", True), ("no", False), ("", False)])
    def test_default_off_needs_explicit_yes(self, monkeypatch, raw, expected):
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", raw)
        assert setup.env_flag("ATATUS_LOG_PROMPTS", default=False) is expected

    @pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("off", False), ("yes", True)])
    def test_default_on_needs_explicit_no(self, monkeypatch, raw, expected):
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", raw)
        assert setup.env_flag("ATATUS_LOG_PROMPTS", default=True) is expected


# ---------------------------------------------------------------------------
# Dotenv parsing
# ---------------------------------------------------------------------------


class TestDotenvParsing:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("ATATUS_API_KEY=plain", "plain"),
            ("export ATATUS_API_KEY=exported", "exported"),
            ("ATATUS_API_KEY='single'", "single"),
            ('ATATUS_API_KEY="double"', "double"),
            ("ATATUS_API_KEY=  spaced  ", "spaced"),
            ("ATATUS_API_KEY = spaced-equals", "spaced-equals"),
            ("ATATUS_API_KEY=has=equals", "has=equals"),
            ("ATATUS_API_KEY=value # trailing comment", "value"),
            ("ATATUS_API_KEY=a#b", "a#b"),  # '#' only comments when whitespace precedes it
            ('ATATUS_API_KEY="keep # hash"', "keep # hash"),
            ("ATATUS_API_KEY=$LITERAL", "$LITERAL"),  # never shell-expanded
            ("ATATUS_API_KEY=", ""),
        ],
    )
    def test_value_forms(self, tmp_path, monkeypatch, line, expected):
        path = _write_env_file(tmp_path, line + "\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        assert setup._dotenv_values().get("ATATUS_API_KEY", "") == expected

    def test_comments_and_blank_lines_are_skipped(self, tmp_path, monkeypatch):
        path = _write_env_file(tmp_path, "# a comment\n\n   \nATATUS_API_KEY=k\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        assert setup._dotenv_values() == {"ATATUS_API_KEY": "k"}

    def test_crlf_is_handled(self, tmp_path, monkeypatch):
        path = _write_env_file(tmp_path, "ATATUS_API_KEY=k\r\nATATUS_USER_ID=dg\r\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        assert setup._dotenv_values() == {"ATATUS_API_KEY": "k", "ATATUS_USER_ID": "dg"}

    def test_escaped_quote_does_not_close_the_string(self, tmp_path, monkeypatch):
        path = _write_env_file(tmp_path, 'ATATUS_API_KEY="a\\"b"\n')
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))
        assert setup._dotenv_values()["ATATUS_API_KEY"] == 'a"b'

    @pytest.mark.parametrize("line", ['ATATUS_API_KEY="abc', "ATATUS_API_KEY='abc", "ATATUS_API_KEY=\"abc'"])
    def test_unbalanced_quote_is_fatal(self, tmp_path, monkeypatch, capsys, line):
        """A credential with a stray quote welded on reports as "found" and then
        fails authentication with nothing pointing at the typo. Stop instead."""
        path = _write_env_file(tmp_path, line + "\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))

        with pytest.raises(SystemExit):
            setup._dotenv_values()
        err = capsys.readouterr().err
        assert "ATATUS_API_KEY" in err and "quote" in err


# ---------------------------------------------------------------------------
# The four shared prompts
# ---------------------------------------------------------------------------


class TestPromptBackend:
    def test_resolves_key_and_default_endpoint(self, monkeypatch):
        from core.common import DEFAULT_OTLP_ENDPOINT

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_API_KEY", "lic-123")

        target, creds = setup.prompt_backend()
        assert target == "atatus"
        assert creds == {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": "lic-123"}

    def test_endpoint_override(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_API_KEY", "lic-123")
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://otel.example")

        _, creds = setup.prompt_backend()
        assert creds["endpoint"] == "https://otel.example"

    def test_missing_key_exits_instead_of_prompting(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        with pytest.raises(SystemExit):
            setup.prompt_backend()

    def test_key_is_never_echoed(self, monkeypatch, capsys):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_API_KEY", "super-secret-key")

        setup.prompt_backend()
        assert "super-secret-key" not in capsys.readouterr().out

    def test_copy_from_menu_is_skipped(self, monkeypatch):
        """Existing harnesses must not trigger an interactive menu."""
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_API_KEY", "lic-123")

        existing = {"codex": {"target": "atatus", "endpoint": "https://e", "api_key": "old"}}
        _, creds = setup.prompt_backend(existing_harnesses=existing)
        assert creds["api_key"] == "lic-123"


class TestPromptProjectName:
    def test_reads_from_the_dotenv_file(self, tmp_path, monkeypatch):
        _no_prompts(monkeypatch)
        path = _write_env_file(tmp_path, "ATATUS_PROJECT_NAME=team-alpha\n")
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(path))

        assert setup.prompt_project_name("") == "team-alpha"

    def test_ambient_project_name_is_ignored(self, monkeypatch):
        """ATATUS_PROJECT_NAME in the environment belongs to whichever harness is
        already installed — claude_code/install.py bakes it into settings.json.

        Inheriting it would name this harness's project after a different one and
        silently collide their spans.
        """
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_PROJECT_NAME", "someone-elses-project")

        assert setup.prompt_project_name("stored-name") == "stored-name"

    def test_falls_back_to_the_stored_default(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        assert setup.prompt_project_name("stored-name") == "stored-name"

    def test_no_name_anywhere_is_fatal(self, monkeypatch, capsys):
        """The project name is the grouping key — never invent one."""
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")

        with pytest.raises(SystemExit):
            setup.prompt_project_name("")
        assert "ATATUS_PROJECT_NAME" in capsys.readouterr().err


class TestPromptContentLogging:
    def test_unattended_captures_nothing_by_default(self, monkeypatch):
        """The interactive wizard defaults prompts/tool_details ON. Unattended it
        must not: a [Y/n] default is a human declining to change an answer they
        were shown, which is consent. The same default with nobody watching is
        not — and `update` forces this mode whenever there is no terminal.
        """
        from core.common import LOG_CONFIG_VERSION

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")

        block = setup.prompt_content_logging()
        assert block == {"_v": LOG_CONFIG_VERSION, "prompts": False, "tool_details": False}

    def test_says_so_when_nothing_is_captured(self, monkeypatch, capsys):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")

        setup.prompt_content_logging()
        out = capsys.readouterr().out
        assert "no content is captured" in out
        assert "ATATUS_LOG_PROMPTS" in out

    def test_explicit_opt_in_is_honoured(self, monkeypatch):
        from core.common import LOG_CONFIG_VERSION

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_LOG_PROMPTS", "true")
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "true")

        # Both now match LOG_FLAG_DEFAULTS, so neither is a deviation worth storing.
        assert setup.prompt_content_logging() == {"_v": LOG_CONFIG_VERSION}

    def test_tool_content_opt_in_is_recorded_as_a_deviation(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")

        assert setup.prompt_content_logging()["tool_content"] is True


class TestPromptUserId:
    def test_resolves_from_env(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_USER_ID", "dg")
        assert setup.prompt_user_id() == "dg"

    def test_absent_is_empty_not_an_error(self, monkeypatch):
        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        assert setup.prompt_user_id() == ""


# ---------------------------------------------------------------------------
# Kiro's two extra prompts
# ---------------------------------------------------------------------------


class TestKiroPrompts:
    def test_agent_name_defaults_without_prompting(self, monkeypatch):
        from tracing.kiro import install as kiro_install
        from tracing.kiro.constants import DEFAULT_AGENT_NAME

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        assert kiro_install._prompt_agent_name() == DEFAULT_AGENT_NAME

    def test_agent_name_from_env(self, monkeypatch):
        from tracing.kiro import install as kiro_install

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_KIRO_AGENT", "my-agent")
        assert kiro_install._prompt_agent_name() == "my-agent"

    def test_default_agent_repointing_stays_opt_in(self, monkeypatch):
        """Repointing someone's default Kiro agent is not something to infer."""
        from tracing.kiro import install as kiro_install

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        called = []
        monkeypatch.setattr(kiro_install.shutil, "which", lambda n: called.append(n) or None)

        kiro_install._maybe_set_default("my-agent")
        assert called == []

    def test_default_agent_repointing_honours_opt_in(self, monkeypatch, capsys):
        from tracing.kiro import install as kiro_install

        _no_prompts(monkeypatch)
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")
        monkeypatch.setenv("ATATUS_KIRO_SET_DEFAULT", "true")
        monkeypatch.setenv("ATATUS_DRY_RUN", "1")

        # dry_run short-circuits just past the opt-in check, so its line proves
        # we got through rather than returning early.
        kiro_install._maybe_set_default("my-agent")
        assert "agent set-default my-agent" in capsys.readouterr().out
