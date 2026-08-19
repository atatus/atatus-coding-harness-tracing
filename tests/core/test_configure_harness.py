"""Tests for the shared install conversation (``core.setup.configure_harness``).

This is the one code path all ten harness installers run for their
configuration, so what is asserted here is what every harness does. The
per-harness test files stub `_reuse_existing` and the individual prompts and
concentrate on the harness-native file each one writes.
"""

from __future__ import annotations

import json

import pytest

import core.setup as _setup
from core.common import LOG_CONFIG_VERSION

STORED = {
    "project_name": "ashif-codex",
    "target": "atatus",
    "endpoint": "https://otel-rx.atatus.com",
    "api_key": "ak-live-000000a91f",
}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point every config reader/writer at one temp config.json."""
    path = tmp_path / ".atatus" / "harness" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.config.CONFIG_FILE", path)
    monkeypatch.setattr("core.constants.CONFIG_FILE", path)
    monkeypatch.setattr(_setup, "CONFIG_FILE", path)
    monkeypatch.setattr(_setup, "INSTALL_DIR", tmp_path / ".atatus" / "harness")
    for name, sub in (("BIN_DIR", "bin"), ("RUN_DIR", "run"), ("LOG_DIR", "logs"), ("STATE_DIR", "state")):
        monkeypatch.setattr(_setup, name, tmp_path / ".atatus" / "harness" / sub)

    # Keep the logging wizard out of the way — it is gated on the config as a
    # whole and has its own tests. stdout is deliberately left alone: pytest's
    # captured stream is already non-tty, so info() stays free of ANSI codes and
    # capsys can still see it.
    monkeypatch.setattr(_setup, "prompt_content_logging", lambda: {"_v": LOG_CONFIG_VERSION})
    return path


def _seed(path, entry=None, user_id="ashif"):
    config = {"harnesses": {"codex": dict(entry or STORED)}, "logging": {"_v": LOG_CONFIG_VERSION}}
    if user_id:
        config["user_id"] = user_id
    path.write_text(json.dumps(config, indent=2))
    return config


def _answers(monkeypatch, inputs, key=""):
    """Feed `inputs` to input() in order, and `key` to the licence-key getpass."""
    it = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(_setup, "getpass", lambda prompt="": key)
    return it


# ---------------------------------------------------------------------------
# The "use this existing configuration?" gate
# ---------------------------------------------------------------------------


class TestReuseGate:
    """The gate shown when this harness already has a stored entry."""

    def test_enter_keeps_everything_and_asks_nothing_else(self, config_file, monkeypatch):
        """Accepting the default answers the whole wizard: no further prompts."""
        _seed(config_file)
        before = config_file.read_text()

        # A single blank line for the gate. Any further input() is a bug, and
        # StopIteration makes it a loud one rather than a silent default.
        _answers(monkeypatch, [""])

        setup = _setup.configure_harness("codex")

        assert setup is not None
        assert setup.reused is True
        assert setup.project_name == "ashif-codex"
        assert setup.user_id == "ashif"
        assert setup.credentials["api_key"] == STORED["api_key"]
        # Nothing was rewritten.
        assert config_file.read_text() == before

    def test_declining_runs_the_wizard_with_stored_defaults(self, config_file, monkeypatch):
        """Answering 'n' re-prompts, and a blank line at each prompt keeps the stored value."""
        _seed(config_file)

        # gate=n, project name blank, endpoint blank, user id blank.
        _answers(monkeypatch, ["n", "", "", ""], key="")

        setup = _setup.configure_harness("codex")

        assert setup.reused is False
        assert setup.project_name == "ashif-codex"
        assert setup.user_id == "ashif"
        assert setup.credentials == {
            "endpoint": STORED["endpoint"],
            "api_key": STORED["api_key"],
        }

    def test_declining_persists_changed_credentials(self, config_file, monkeypatch):
        """The whole point of the 'no' branch: a new key and endpoint actually land."""
        _seed(config_file)

        _answers(
            monkeypatch,
            ["n", "renamed-project", "https://otel.example.test", "someone-else"],
            key="ak-rotated-1234",
        )

        _setup.configure_harness("codex")

        entry = json.loads(config_file.read_text())["harnesses"]["codex"]
        assert entry["project_name"] == "renamed-project"
        assert entry["api_key"] == "ak-rotated-1234"
        assert entry["endpoint"] == "https://otel.example.test"
        assert entry["target"] == "atatus"
        assert json.loads(config_file.read_text())["user_id"] == "someone-else"

    def test_user_id_can_be_cleared(self, config_file, monkeypatch):
        """`-` clears a stored user id, since blank now means 'keep'."""
        _seed(config_file)
        _answers(monkeypatch, ["n", "", "", "-"])

        setup = _setup.configure_harness("codex")

        assert setup.user_id == ""
        assert "user_id" not in json.loads(config_file.read_text())

    def test_summary_never_prints_the_whole_key(self, config_file, monkeypatch, capsys):
        """The summary exists to be recognised, not to disclose the key."""
        _seed(config_file)
        _answers(monkeypatch, [""])

        _setup.configure_harness("codex")

        out = capsys.readouterr().out
        assert STORED["api_key"] not in out
        assert "****...a91f" in out

    def test_entry_without_a_target_is_not_configured(self, config_file, monkeypatch):
        """A project_name-only entry is what merge_harness_entry leaves behind.

        Treating it as configured would skip the credential prompts and install
        a harness that can never send, so it must take the fresh path instead.
        """
        config_file.write_text(json.dumps({"harnesses": {"codex": {"project_name": "half"}}}))

        # No gate: project name, endpoint, user id, then three logging answers.
        _answers(monkeypatch, ["fresh-name", "", "", "", "", ""], key="ak-new")

        setup = _setup.configure_harness("codex")

        assert setup.reused is False
        assert setup.credentials["api_key"] == "ak-new"


# ---------------------------------------------------------------------------
# Fresh installs
# ---------------------------------------------------------------------------


class TestFreshInstall:
    """No stored entry for this harness."""

    def test_project_name_defaults_to_the_harness_name(self, config_file, monkeypatch):
        """Enter at the project-name prompt accepts the shown default."""
        prompts = []

        def _input(prompt=""):
            prompts.append(prompt)
            return ""

        monkeypatch.setattr("builtins.input", _input)
        monkeypatch.setattr(_setup, "getpass", lambda prompt="": "ak-new")

        setup = _setup.configure_harness("codex")

        assert setup.project_name == "codex"
        assert any(p.startswith("Project name [codex]") for p in prompts)

    def test_default_comes_from_the_harness_metadata_table(self, config_file, monkeypatch):
        """claude-code's default_project_name is what core.constants says it is."""
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(_setup, "getpass", lambda prompt="": "ak-new")

        setup = _setup.configure_harness("claude-code")

        assert setup.project_name == "claude-code"

    def test_unknown_harness_falls_back_to_its_own_name(self, config_file, monkeypatch):
        """kiro and devin are absent from the HARNESSES table; the fallback covers them."""
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(_setup, "getpass", lambda prompt="": "ak-new")

        assert _setup.configure_harness("kiro").project_name == "kiro"

    def test_no_gate_is_shown(self, config_file, monkeypatch):
        """With nothing stored there is nothing to reuse, so the first prompt is the name."""
        _answers(monkeypatch, ["chosen-name", "", "", "", "", ""], key="ak-new")

        setup = _setup.configure_harness("codex")

        assert setup.project_name == "chosen-name"
        assert setup.reused is False


# ---------------------------------------------------------------------------
# Non-interactive
# ---------------------------------------------------------------------------


class TestNonInteractive:
    """ATATUS_NONINTERACTIVE=1 — including `install.sh update` on a non-TTY."""

    @pytest.fixture(autouse=True)
    def _no_prompts(self, monkeypatch):
        monkeypatch.setenv("ATATUS_NONINTERACTIVE", "1")

        def _boom(prompt=""):
            raise AssertionError(f"prompted with nobody to answer: {prompt!r}")

        monkeypatch.setattr("builtins.input", _boom)
        monkeypatch.setattr(_setup, "getpass", _boom)

    def test_reinstall_reuses_the_stored_entry(self, config_file):
        """The codex regression: this used to exit 1 despite a stored project name."""
        _seed(config_file)

        setup = _setup.configure_harness("codex")

        assert setup.project_name == "ashif-codex"
        assert setup.credentials["api_key"] == STORED["api_key"]
        assert setup.user_id == "ashif"

    def test_dotenv_overrides_a_stored_value(self, config_file, tmp_path, monkeypatch):
        """Reuse must not mean 'stuck': naming a value in the dotenv file rotates it."""
        _seed(config_file)
        env_file = tmp_path / "onboarding.env"
        env_file.write_text("ATATUS_API_KEY=ak-rotated\nATATUS_PROJECT_NAME=from-dotenv\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(env_file))

        setup = _setup.configure_harness("codex")

        assert setup.credentials["api_key"] == "ak-rotated"
        assert setup.project_name == "from-dotenv"

    def test_ambient_env_does_not_repoint_a_stored_entry(self, config_file, monkeypatch):
        """The traced-terminal trap: inherited vars must not beat stored config.

        Every installed harness exports ATATUS_API_KEY and ATATUS_OTLP_ENDPOINT
        into the sessions it spawns, so `install.sh update` frequently runs with
        them already set to some *other* harness's values. Honouring them would
        silently ship this harness's spans to whatever endpoint that session
        carried.
        """
        _seed(config_file)
        monkeypatch.setenv("ATATUS_API_KEY", "ak-inherited")
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "http://localhost:8081")
        monkeypatch.setenv("ATATUS_USER_ID", "someone-elses-session")

        setup = _setup.configure_harness("codex")

        assert setup.credentials["api_key"] == STORED["api_key"]
        assert setup.credentials["endpoint"] == STORED["endpoint"]
        assert setup.user_id == "ashif"

    def test_ambient_env_still_supplies_a_fresh_install(self, config_file, monkeypatch, tmp_path):
        """With nothing stored there is nothing to protect, so the environment counts."""
        env_file = tmp_path / "onboarding.env"
        env_file.write_text("ATATUS_PROJECT_NAME=ci-project\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(env_file))
        monkeypatch.setenv("ATATUS_API_KEY", "ak-from-env")
        monkeypatch.setenv("ATATUS_OTLP_ENDPOINT", "https://otel.eu.atatus.com")

        setup = _setup.configure_harness("codex")

        assert setup.credentials["api_key"] == "ak-from-env"
        assert setup.credentials["endpoint"] == "https://otel.eu.atatus.com"

    def test_fresh_install_without_a_project_name_still_exits(self, config_file, monkeypatch):
        """The CI guard: no harness-name default when nobody is watching.

        The name is the OTLP service.name, so defaulting it here would file every
        unattended install on every machine into one shared project.
        """
        monkeypatch.setenv("ATATUS_API_KEY", "ak-new")

        with pytest.raises(SystemExit) as exc:
            _setup.configure_harness("codex")
        assert exc.value.code == 1

    def test_fresh_install_without_a_key_still_exits(self, config_file, tmp_path, monkeypatch):
        env_file = tmp_path / "onboarding.env"
        env_file.write_text("ATATUS_PROJECT_NAME=ci-project\n")
        monkeypatch.setenv("ATATUS_ENV_FILE", str(env_file))

        with pytest.raises(SystemExit) as exc:
            _setup.configure_harness("codex")
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# The presence check
# ---------------------------------------------------------------------------


class TestPresenceCheck:
    """`ensure_harness_installed` runs only when it has something to look at."""

    def test_declining_aborts(self, config_file, monkeypatch):
        monkeypatch.setattr(_setup, "ensure_harness_installed", lambda *a, **k: False)

        assert _setup.configure_harness("codex", display_name="Codex", bin_name="codex") is None

    def test_skipped_when_no_signals_are_given(self, config_file, monkeypatch):
        """Copilot passes none, and must not be warned about on every install."""
        called = []
        monkeypatch.setattr(_setup, "ensure_harness_installed", lambda *a, **k: called.append(a) or True)
        _answers(monkeypatch, ["n", "", "", ""])
        _seed(config_file)

        _setup.configure_harness("codex", display_name="Codex")

        assert called == []
