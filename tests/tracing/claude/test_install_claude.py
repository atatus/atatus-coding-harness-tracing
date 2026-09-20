"""Tests for claude-code-tracing/install.py — install/uninstall module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.setup as _setup
from core.common import DEFAULT_OTLP_ENDPOINT


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path so all file writes land in a temp dir.

    Also patches the module-level constants in install.py and core.setup that
    derive from Path.home() so they point at the temp tree.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    # Patch INSTALL_DIR / VENV_DIR / CONFIG_FILE in core.setup
    import core.setup as setup_mod

    install_dir = tmp_path / ".atatus" / "harness"
    venv_dir = install_dir / "venv"
    config_file = install_dir / "config.json"

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(setup_mod, "VENV_DIR", venv_dir)
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")

    # Patch CONFIG_FILE in core.config so load_config/save_config use tmp
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))

    # Patch SETTINGS_FILE in the install module's constants
    settings_file = tmp_path / ".claude" / "settings.json"

    # We need to patch both the constants module and the install module's reference
    import tracing.claude_code.constants as claude_constants
    import tracing.claude_code.install as claude_install

    monkeypatch.setattr(claude_constants, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(claude_install, "SETTINGS_FILE", settings_file)

    # Create the harness plugin dir so harness_dir() resolves
    plugin_dir = install_dir / "tracing" / "claude_code"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    return tmp_path


def _fake_stdout():
    """Non-tty stdout to suppress ANSI codes."""
    return type(
        "FakeOut",
        (),
        {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
    )()


ATATUS_BACKEND = ("atatus", {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": ""})
ATATUS_BACKEND = (
    "atatus",
    {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": "test-key"},
)


@pytest.fixture(autouse=True)
def _always_reconfigure(monkeypatch):
    """Take the reconfigure branch of configure_harness.

    These tests predate the "use this existing configuration?" gate and assert
    on what the prompts do with a pre-seeded config, so they need the prompts to
    actually run. The gate itself is covered in tests/core/test_configure_harness.py.
    """
    monkeypatch.setattr(_setup, "_reuse_existing", lambda *a, **k: False)


def _mock_prompts(monkeypatch, backend=None):
    """Patch the prompts on core.setup, which is where configure_harness calls them."""
    if backend is None:
        backend = ATATUS_BACKEND

    monkeypatch.setattr(
        _setup,
        "prompt_backend",
        lambda existing_harnesses=None, current=None: backend,
    )
    monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "claude-code")
    monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
    monkeypatch.setattr(
        _setup, "prompt_content_logging", lambda: {"prompts": True, "tool_details": True, "tool_content": True}
    )
    monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)
    monkeypatch.setattr("sys.stdout", _fake_stdout())


class TestFreshInstall:
    """Fresh install with no existing config."""

    @pytest.mark.parametrize(
        "backend,expected_target",
        [(ATATUS_BACKEND, "atatus"), (ATATUS_BACKEND, "atatus")],
        ids=["atatus", "atatus"],
    )
    def test_fresh_install_creates_config_and_hooks(self, fake_home, monkeypatch, backend, expected_target):
        """With no existing config, install() prompts and writes config.json + settings.json."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=backend)

        claude_install.install(with_skills=False)

        # Check config.json was written
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        assert config_file.exists()
        config = json.loads(config_file.read_text())
        assert config["harnesses"]["claude-code"]["target"] == expected_target
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"

        # Check settings.json has plugin + all hook events
        settings_file = fake_home / ".claude" / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())

        assert len(settings.get("plugins", [])) == 1
        assert settings["plugins"][0]["type"] == "local"

        hooks = settings.get("hooks", {})
        assert len(hooks) == 16

        env = settings.get("env", {})
        assert env.get("ATATUS_TRACE_ENABLED") == "true"
        assert env.get("ATATUS_PROJECT_NAME") == "claude-code"

    def test_install_fresh_writes_flat_harness_entry(self, fake_home, monkeypatch):
        """Fresh install writes all backend fields directly under harnesses.claude-code."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=ATATUS_BACKEND)

        claude_install.install(with_skills=False)

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # All fields at the same level — no nested backend block
        assert entry["project_name"] == "claude-code"
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == DEFAULT_OTLP_ENDPOINT
        assert entry["api_key"] == "test-key"

        # No legacy top-level backend block
        assert "backend" not in config
        # No nested backend under the harness entry
        assert "backend" not in entry


class TestIdempotent:
    """Re-install is idempotent — no duplicate hooks."""

    def test_double_install_no_duplicates(self, fake_home, monkeypatch):
        """Running install() twice does not duplicate hooks or plugins."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.install(with_skills=False)

        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())

        # Still exactly 1 plugin
        assert len(settings["plugins"]) == 1

        # Still exactly 1 hook entry per event
        for event, entries in settings["hooks"].items():
            assert len(entries) == 1, f"Event {event} has {len(entries)} entries"


class TestExistingEntry:
    """Re-install with an existing harness entry only updates project_name."""

    def test_install_existing_claude_entry_only_updates_project_name(self, fake_home, monkeypatch):
        """When harnesses.claude-code already exists, re-install updates only project_name."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=ATATUS_BACKEND)

        # Pre-populate config with an existing claude-code entry
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        original_entry = {
            "project_name": "old-name",
            "target": "atatus",
            "endpoint": DEFAULT_OTLP_ENDPOINT,
            "api_key": "original-key",
        }
        config_file.write_text(json.dumps({"harnesses": {"claude-code": original_entry}}, indent=2))

        # Mock prompt_project_name to return a new name
        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": "new-project-name")
        # These two files stub prompt_backend wholesale in _mock_prompts, so
        # re-stub it with what a blank licence key and endpoint resolve to:
        # the stored credentials, unchanged.
        monkeypatch.setattr(
            _setup,
            "prompt_backend",
            lambda existing_harnesses=None, current=None: (current["target"], dict(current)),
        )

        claude_install.install(with_skills=False)

        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # project_name updated
        assert entry["project_name"] == "new-project-name"
        # credentials preserved
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == DEFAULT_OTLP_ENDPOINT
        assert entry["api_key"] == "original-key"


class TestCopyFrom:
    """Copy-from: install offers to reuse credentials from another harness."""

    def test_install_second_harness_offers_copy_from(self, fake_home, monkeypatch):
        """Pre-populate codex with atatus creds; verify claude-code gets them via copy-from."""
        import tracing.claude_code.install as claude_install

        # Pre-populate config with a codex entry using atatus
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        codex_entry = {
            "project_name": "codex",
            "target": "atatus",
            "endpoint": DEFAULT_OTLP_ENDPOINT,
            "api_key": "codex-key",
        }
        config_file.write_text(json.dumps({"harnesses": {"codex": codex_entry}}, indent=2))

        # Mock prompt_backend to return atatus target with codex's credentials (simulating copy-from)
        copied_creds = {"endpoint": DEFAULT_OTLP_ENDPOINT, "api_key": "codex-key"}
        monkeypatch.setattr(
            _setup,
            "prompt_backend",
            lambda existing_harnesses=None, current=None: ("atatus", copied_creds),
        )
        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "claude-code")
        monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
        monkeypatch.setattr(
            _setup,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        claude_install.install(with_skills=False)

        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # claude-code got codex's credentials
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == DEFAULT_OTLP_ENDPOINT
        assert entry["api_key"] == "codex-key"
        assert entry["project_name"] == "claude-code"

        # codex entry is preserved
        assert config["harnesses"]["codex"]["api_key"] == "codex-key"


class TestUninstall:
    """Uninstall removes hooks and harness entry."""

    def test_uninstall_removes_hooks_and_config(self, fake_home, monkeypatch):
        """Uninstall removes hooks, plugin, and harness entry from config.json."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.uninstall()

        # settings.json should have no hooks and no plugins
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        assert "hooks" not in settings
        assert "plugins" not in settings

        # config.json should have no claude-code entry
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        harnesses = config.get("harnesses", {})
        assert "claude-code" not in harnesses

    def test_uninstall_removes_harness_entry_preserves_others(self, fake_home, monkeypatch):
        """Uninstall removes claude-code but preserves other harness entries."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        # Install claude-code
        claude_install.install(with_skills=False)

        # Add another harness entry to config
        config_file = fake_home / ".atatus" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        config["harnesses"]["copilot"] = {
            "project_name": "copilot",
            "target": "atatus",
            "endpoint": DEFAULT_OTLP_ENDPOINT,
            "api_key": "copilot-key",
        }
        config_file.write_text(json.dumps(config, indent=2))

        claude_install.uninstall()

        config = json.loads(config_file.read_text())
        assert "claude-code" not in config["harnesses"]
        assert config["harnesses"]["copilot"]["api_key"] == "copilot-key"

    def test_uninstall_idempotent(self, fake_home, monkeypatch):
        """Calling uninstall twice does not error."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.uninstall()
        # Second uninstall should be a no-op, not raise
        claude_install.uninstall()

    def test_uninstall_clears_atatus_env_keys(self, fake_home, monkeypatch):
        """Uninstall pops Atatus env vars from settings.json.

        Regression guard: the previous bash installer cleaned
        ATATUS_PROJECT_NAME / ATATUS_TRACE_ENABLED / etc. out of
        settings.json on uninstall. The Python port only removed hooks
        and plugins, leaving stale env entries.
        """
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        # Inject extra Atatus env keys + a non-Atatus key that must be preserved.
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        settings["env"].update(
            {
                "ATATUS_USER_ID": "user-42",
                "ATATUS_API_KEY": "ak-secret",
                "ATATUS_OTLP_ENDPOINT": DEFAULT_OTLP_ENDPOINT,
                "UNRELATED_VAR": "keep-me",
            }
        )
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")

        claude_install.uninstall()

        settings = json.loads(settings_file.read_text())
        env = settings.get("env", {})

        # All Atatus keys are gone
        for key in (
            "ATATUS_PROJECT_NAME",
            "ATATUS_TRACE_ENABLED",
            "ATATUS_USER_ID",
            "ATATUS_API_KEY",
            "ATATUS_OTLP_ENDPOINT",
        ):
            assert key not in env, f"{key} should have been removed from env"

        # Non-Atatus env survives
        assert env.get("UNRELATED_VAR") == "keep-me"

    def test_uninstall_drops_env_block_when_emptied(self, fake_home, monkeypatch):
        """If removing Atatus keys leaves env empty, the env block is dropped."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)
        claude_install.install(with_skills=False)
        claude_install.uninstall()

        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        # install only set ATATUS_* keys, so removing them leaves env empty
        assert "env" not in settings

    def test_uninstall_preserves_third_party_hooks(self, fake_home, monkeypatch):
        """Uninstall keeps hooks that don't belong to us."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        # Inject a third-party hook into SessionStart
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        third_party = {"hooks": [{"type": "command", "command": "/usr/local/bin/my-hook"}]}
        settings["hooks"]["SessionStart"].append(third_party)
        # Also add a completely separate event
        settings["hooks"]["CustomEvent"] = [{"hooks": [{"type": "command", "command": "/usr/local/bin/other"}]}]
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")

        claude_install.uninstall()

        settings = json.loads(settings_file.read_text())
        hooks = settings.get("hooks", {})

        # Third-party hook in SessionStart survives
        assert "SessionStart" in hooks
        assert len(hooks["SessionStart"]) == 1
        assert hooks["SessionStart"][0]["hooks"][0]["command"] == "/usr/local/bin/my-hook"

        # CustomEvent survives
        assert "CustomEvent" in hooks
        assert hooks["CustomEvent"][0]["hooks"][0]["command"] == "/usr/local/bin/other"


class TestDryRun:
    """Dry-run mode should not write files."""

    def test_install_dry_run_writes_nothing(self, fake_home, monkeypatch):
        """With ATATUS_DRY_RUN=true, install() logs but does not write files."""
        import tracing.claude_code.install as claude_install

        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        settings_file = fake_home / ".claude" / "settings.json"
        assert not settings_file.exists()

        config_file = fake_home / ".atatus" / "harness" / "config.json"
        assert not config_file.exists()


class TestHookTimeout:
    """Claude Code kills a hook that outlives its ``timeout`` and moves on; the
    default is 60s. Every entry we register carries an explicit cap so a hook
    that ever wedges costs the user seconds, not a minute per tool call."""

    def test_fresh_install_stamps_the_timeout_on_every_hook(self, fake_home, monkeypatch):
        import tracing.claude_code.install as claude_install
        from tracing.claude_code.constants import HOOK_EVENTS, HOOK_TIMEOUT_SECONDS

        _mock_prompts(monkeypatch)
        claude_install.install(with_skills=False)

        settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
        for event in HOOK_EVENTS:
            for entry in settings["hooks"][event]:
                for hook in entry["hooks"]:
                    assert hook["timeout"] == HOOK_TIMEOUT_SECONDS

    def test_update_stamps_the_timeout_onto_entries_that_predate_it(self, fake_home, monkeypatch):
        """Re-running install must tighten an existing registration, not skip it
        as "already present" and leave it on the 60s default."""
        import tracing.claude_code.install as claude_install
        from core.setup import venv_bin
        from tracing.claude_code.constants import HOOK_EVENTS, HOOK_TIMEOUT_SECONDS

        settings_file = fake_home / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        legacy = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": str(venv_bin(ep))}]}]
                for event, ep in HOOK_EVENTS.items()
            }
        }
        settings_file.write_text(json.dumps(legacy))

        _mock_prompts(monkeypatch)
        claude_install.install(with_skills=False)

        settings = json.loads(settings_file.read_text())
        for event in HOOK_EVENTS:
            assert len(settings["hooks"][event]) == 1
            assert settings["hooks"][event][0]["hooks"][0]["timeout"] == HOOK_TIMEOUT_SECONDS

    def test_timeout_is_a_bound_on_faults_not_on_normal_hooks(self):
        """Our slowest measured hook is ~0.3s; the cap must sit far above that so
        a loaded machine never has its telemetry cut mid-turn."""
        from tracing.claude_code.constants import HOOK_TIMEOUT_SECONDS

        assert 5 <= HOOK_TIMEOUT_SECONDS < 60
