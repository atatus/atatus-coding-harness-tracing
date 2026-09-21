"""Tests for tracing.copilot/install.py: install and uninstall of Copilot hooks."""

from __future__ import annotations

import json

import pytest

import core.setup as _setup
import tracing.copilot.install as _install
from core.common import DEFAULT_OTLP_ENDPOINT
from tracing.copilot.constants import HOOK_CONFIG_VERSION, HOOKS_FILE_NAME, LEGACY_HOOKS_DIR, LEGACY_HOOKS_FILE_NAME

install = _install.install
uninstall = _install.uninstall


# ---------------------------------------------------------------------------
# Test backend tuples
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_stdout():
    """Non-tty stdout to suppress ANSI codes."""
    return type(
        "FakeOut",
        (),
        {"isatty": lambda self: False, "write": lambda self, s: None, "flush": lambda self: None},
    )()


def _mock_prompts(monkeypatch, backend=None):
    """Patch prompt functions on the install module (where they're bound after import)."""
    if backend is None:
        backend = ATATUS_BACKEND

    monkeypatch.setattr(
        _setup,
        "prompt_backend",
        lambda existing_harnesses=None, current=None: backend,
    )
    monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "copilot")
    monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
    monkeypatch.setattr(
        _setup,
        "prompt_content_logging",
        lambda: {"prompts": True, "tool_details": True, "tool_content": True},
    )
    monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)
    monkeypatch.setattr("sys.stdout", _fake_stdout())


@pytest.fixture
def cwd_tmp(tmp_path, monkeypatch):
    """Set cwd to tmp_path and patch core.setup paths for isolation.

    Hooks are user-level now, so COPILOT_HOME is redirected too: without it the
    installer would write into the developer's real ~/.copilot.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / ".copilot"))

    import core.setup as setup_mod

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", tmp_path / ".atatus" / "harness")
    monkeypatch.setattr(setup_mod, "VENV_DIR", tmp_path / ".atatus" / "harness" / "venv")
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", tmp_path / ".atatus" / "harness" / "config.json")
    monkeypatch.setattr(setup_mod, "BIN_DIR", tmp_path / ".atatus" / "harness" / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", tmp_path / ".atatus" / "harness" / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", tmp_path / ".atatus" / "harness" / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", tmp_path / ".atatus" / "harness" / "state")

    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", tmp_path / ".atatus" / "harness")
    monkeypatch.setattr(c, "CONFIG_FILE", tmp_path / ".atatus" / "harness" / "config.json")

    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / ".atatus" / "harness" / "config.json"))

    return tmp_path


@pytest.fixture
def hooks_dir(cwd_tmp):
    """Return the user-level hooks directory under the temp COPILOT_HOME.

    Hooks used to be written to ``<cwd>/.github/hooks``, which traced only the
    one project the installer happened to be run from.
    """
    from tracing.copilot.constants import hooks_dir as _hooks_dir

    resolved = _hooks_dir()
    assert resolved == cwd_tmp / ".copilot" / "hooks"
    return resolved


# ---------------------------------------------------------------------------
# Install tests
# ---------------------------------------------------------------------------


class TestInstallFreshWritesFlatHarnessEntry:
    """Fresh install writes flat harness entry to config.json."""

    @pytest.mark.parametrize(
        "backend,expected_target",
        [(ATATUS_BACKEND, "atatus"), (ATATUS_BACKEND, "atatus")],
        ids=["atatus", "atatus"],
    )
    def test_fresh_install_creates_config_and_hooks(self, cwd_tmp, monkeypatch, backend, expected_target):
        _mock_prompts(monkeypatch, backend=backend)
        install()

        config_path = cwd_tmp / ".atatus" / "harness" / "config.json"
        assert config_path.is_file()
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "copilot"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]

        # No collector for copilot
        assert "collector" not in entry

    def test_hooks_file_created_at_user_level(self, hooks_dir, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert (hooks_dir / HOOKS_FILE_NAME).is_file()
        # Not project-local: an install must trace every project, not just this one.
        assert not (cwd_tmp / ".github" / "hooks").exists()

    def test_hooks_file_structure(self, hooks_dir, monkeypatch):
        """The hooks file must follow the Copilot schema:
        {"version": 1, "hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}
        """
        _mock_prompts(monkeypatch)
        install()
        data = json.loads((hooks_dir / HOOKS_FILE_NAME).read_text())
        assert set(data["hooks"].keys()) == {
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
        }
        for event, entries in data["hooks"].items():
            assert len(entries) == 1
            assert entries[0]["type"] == "command"
            assert "atatus-hook-copilot-" in entries[0]["command"]

    def test_hooks_file_declares_schema_version(self, hooks_dir, monkeypatch):
        """Copilot rejects a hooks file that omits the schema version."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads((hooks_dir / HOOKS_FILE_NAME).read_text())
        assert data["version"] == HOOK_CONFIG_VERSION

    def test_owns_a_dedicated_file(self, hooks_dir, monkeypatch):
        """Copilot reads every *.json here, so we write only our own file."""
        _mock_prompts(monkeypatch)
        install()
        json_files = list(hooks_dir.glob("*.json"))
        assert len(json_files) == 1
        assert json_files[0].name == HOOKS_FILE_NAME

    def test_leaves_a_foreign_hooks_file_alone(self, hooks_dir, monkeypatch):
        """A hooks file the user wrote is neither read nor rewritten."""
        hooks_dir.mkdir(parents=True, exist_ok=True)
        other = hooks_dir / "my-own-hooks.json"
        other.write_text(json.dumps({"hooks": {"SessionStart": [{"type": "command", "command": "/bin/mine"}]}}))
        original = other.read_text()

        _mock_prompts(monkeypatch)
        install()

        assert other.read_text() == original


class TestInstallSecondHarnessOffersCopyFrom:
    """When another harness exists with the same target, copy-from is offered."""

    def test_copy_from_populates_credentials(self, cwd_tmp, monkeypatch):
        """Pre-seed a claude-code entry; copilot install should receive it in prompt_backend."""
        config_dir = cwd_tmp / ".atatus" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        # Pre-seed with claude-code atatus entry
        seed_config = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "atatus",
                    "endpoint": DEFAULT_OTLP_ENDPOINT,
                    "api_key": "ak-existing",
                }
            }
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        captured = {}

        def fake_prompt_backend(existing_harnesses=None, current=None):
            captured["existing_harnesses"] = existing_harnesses
            return ATATUS_BACKEND

        monkeypatch.setattr(_setup, "prompt_backend", fake_prompt_backend)
        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": default or "copilot")
        monkeypatch.setattr(_setup, "prompt_user_id", lambda default="": "")
        monkeypatch.setattr(
            _setup,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        # prompt_backend should have received the existing harnesses dict
        assert captured["existing_harnesses"] is not None
        assert "claude-code" in captured["existing_harnesses"]
        assert captured["existing_harnesses"]["claude-code"]["target"] == "atatus"

        # Verify the copilot entry was actually written with correct credentials
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == ATATUS_BACKEND[1]["endpoint"]
        assert entry["api_key"] == ATATUS_BACKEND[1]["api_key"]
        assert entry["project_name"] == "copilot"


class TestInstallExistingCopilotEntryOnlyUpdatesProjectName:
    """Re-install with existing copilot config only updates project_name."""

    def test_existing_entry_preserves_target(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".atatus" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "copilot": {
                    "project_name": "copilot",
                    "target": "atatus",
                    "endpoint": DEFAULT_OTLP_ENDPOINT,
                    "api_key": "ak-existing",
                }
            }
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        # prompt_project_name returns a new name
        monkeypatch.setattr(_setup, "prompt_project_name", lambda default="": "my-copilot")
        # A blank line at the credential prompts keeps what is already stored,
        # which is what makes a reconfigure able to change only the name.
        monkeypatch.setattr(_setup, "getpass", lambda prompt="": "")
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(
            _setup,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_setup, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["project_name"] == "my-copilot"
        # Other fields preserved
        assert entry["target"] == "atatus"
        assert entry["endpoint"] == DEFAULT_OTLP_ENDPOINT
        assert entry["api_key"] == "ak-existing"


class TestIdempotent:
    """Re-install is idempotent — no duplicate entries."""

    def test_no_duplicate_entries(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads((hooks_dir / HOOKS_FILE_NAME).read_text())
        for event, entries in data["hooks"].items():
            assert len(entries) == 1, f"Duplicate entries for event {event}"

    def test_moved_venv_does_not_stack_duplicates(self, hooks_dir, monkeypatch):
        """Our previous entry is matched by prefix, not by absolute path."""
        _mock_prompts(monkeypatch)
        install()

        hf = hooks_dir / HOOKS_FILE_NAME
        data = json.loads(hf.read_text())
        for entries in data["hooks"].values():
            entries[0]["command"] = "/old/venv/bin/" + entries[0]["command"].rsplit("/", 1)[-1]
        hf.write_text(json.dumps(data, indent=2) + "\n")

        install()

        data = json.loads(hf.read_text())
        for event, entries in data["hooks"].items():
            assert len(entries) == 1, f"Duplicate entries for event {event}"
            assert not entries[0]["command"].startswith("/old/venv/")


# ---------------------------------------------------------------------------
# Uninstall tests
# ---------------------------------------------------------------------------


class TestUninstallRemovesHarnessEntry:
    """Uninstall removes harness entry from config.json."""

    def test_config_entry_removed(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        config_path = cwd_tmp / ".atatus" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            harnesses = config.get("harnesses", {})
            assert "copilot" not in harnesses

    def test_hooks_file_removed(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert (hooks_dir / HOOKS_FILE_NAME).is_file()
        uninstall()
        assert not (hooks_dir / HOOKS_FILE_NAME).exists()

    def test_uninstall_is_idempotent(self, cwd_tmp, monkeypatch):
        """Running uninstall twice succeeds without error."""
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        # Second uninstall should be a no-op, no exception
        uninstall()
        config_path = cwd_tmp / ".atatus" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            harnesses = config.get("harnesses", {})
            assert "copilot" not in harnesses


class TestUninstallPreservesUserHooks:
    """Uninstall on a pre-populated hooks file preserves unrelated user hooks."""

    def test_preserves_user_hooks(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()

        # Add user-defined entries: a brand-new event, plus an extra command
        # alongside ours under SessionStart.
        hf = hooks_dir / HOOKS_FILE_NAME
        data = json.loads(hf.read_text())
        data["hooks"]["CustomEvent"] = [{"type": "command", "command": "/usr/local/bin/my-hook"}]
        data["hooks"]["SessionStart"].append({"type": "command", "command": "/usr/local/bin/user-session"})
        hf.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        assert hf.is_file()
        remaining = json.loads(hf.read_text())
        assert remaining["hooks"]["CustomEvent"] == [{"type": "command", "command": "/usr/local/bin/my-hook"}]
        assert remaining["hooks"]["SessionStart"] == [{"type": "command", "command": "/usr/local/bin/user-session"}]


# ---------------------------------------------------------------------------
# Legacy project-local hooks
# ---------------------------------------------------------------------------


class TestLegacyProjectLocalHooks:
    """Hooks left by a pre-user-level install would double-fire inside that project."""

    def _write_legacy(self, cwd_tmp, entries):
        path = cwd_tmp / LEGACY_HOOKS_DIR / LEGACY_HOOKS_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"SessionStart": entries}}, indent=2) + "\n")
        return path

    def test_install_removes_superseded_project_local_hooks(self, cwd_tmp, monkeypatch):
        legacy = self._write_legacy(
            cwd_tmp, [{"type": "command", "command": "/old/bin/atatus-hook-copilot-session-start"}]
        )
        _mock_prompts(monkeypatch)
        install()
        assert not legacy.exists()

    def test_uninstall_removes_superseded_project_local_hooks(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        legacy = self._write_legacy(
            cwd_tmp, [{"type": "command", "command": "/old/bin/atatus-hook-copilot-session-start"}]
        )
        uninstall()
        assert not legacy.exists()

    def test_legacy_file_without_our_entries_is_untouched(self, cwd_tmp, monkeypatch):
        legacy = self._write_legacy(cwd_tmp, [{"type": "command", "command": "/usr/local/bin/user-hook"}])
        original = legacy.read_text()
        _mock_prompts(monkeypatch)
        install()
        assert legacy.read_text() == original


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestInstallDryRunWritesNothing:
    """Dry-run mode writes nothing."""

    def test_dry_run_no_files(self, hooks_dir, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        json_files = list(hooks_dir.glob("*.json")) if hooks_dir.exists() else []
        assert len(json_files) == 0

    def test_dry_run_no_config(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ATATUS_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        config_path = cwd_tmp / ".atatus" / "harness" / "config.json"
        assert not config_path.is_file()
