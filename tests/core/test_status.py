#!/usr/bin/env python3
"""Tests for `install.sh status` (core/setup/status.py).

Hermetic by construction: every registration path is redirected into tmp_path.
Resolving them against the developer's real ~/.claude and ~/.codex would make
results vary by machine — the same defect this command exists to catch.
"""

from __future__ import annotations

import json

import pytest

import core.setup.status as status


@pytest.fixture()
def harness_env(tmp_path, monkeypatch):
    """Point INSTALL_DIR, CONFIG_FILE and every registration path at tmp_path."""
    install_dir = tmp_path / ".atatus" / "harness"
    (install_dir / "venv").mkdir(parents=True)
    config_file = install_dir / "config.json"

    monkeypatch.setattr(status, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(status, "VENV_DIR", install_dir / "venv")
    monkeypatch.setattr(status, "CONFIG_FILE", config_file)

    reg_dir = tmp_path / "reg"
    reg_dir.mkdir()
    monkeypatch.setattr(
        status,
        "_REGISTRATION",
        {"claude-code": ("core.setup.status", ("_FAKE_SETTINGS",))},
    )
    monkeypatch.setattr(status, "_FAKE_SETTINGS", reg_dir / "settings.json", raising=False)

    return {"install_dir": install_dir, "config_file": config_file, "settings": reg_dir / "settings.json"}


def _write_config(path, harnesses, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"harnesses": harnesses, **extra}))


def _register(settings_path, install_dir):
    """Write a settings file that references the install dir, as a real install would."""
    settings_path.write_text(json.dumps({"hooks": [{"command": f"{install_dir}/venv/bin/atatus-hook-stop"}]}))


# ---------------------------------------------------------------------------
# _references_install
# ---------------------------------------------------------------------------


class TestReferencesInstall:
    def test_matches_a_file_mentioning_the_install_dir(self, harness_env):
        _register(harness_env["settings"], harness_env["install_dir"])
        assert status._references_install(harness_env["settings"]) is True

    def test_sibling_directory_does_not_match_on_prefix(self, harness_env):
        """`~/.atatus/harness-old` is not `~/.atatus/harness`.

        Without the trailing separator a stale install reported as wired up, in
        the one command whose job is to tell you the truth about that.
        """
        harness_env["settings"].write_text(
            json.dumps({"hooks": [{"command": f"{harness_env['install_dir']}-old/venv/bin/atatus-hook-stop"}]})
        )
        assert status._references_install(harness_env["settings"]) is False

    def test_json_escaped_windows_separators_match(self, harness_env, monkeypatch):
        """A JSON-serialised Windows path doubles every backslash.

        Matching only the raw form reported every JSON-registered harness as not
        registered on Windows while the install had in fact worked.
        """
        monkeypatch.setattr(status, "INSTALL_DIR", type(harness_env["install_dir"])("D:\\Users\\dg\\.atatus\\harness"))
        harness_env["settings"].write_text(r'{"command": "D:\\Users\\dg\\.atatus\\harness\\venv\\Scripts\\hook.exe"}')
        assert status._references_install(harness_env["settings"]) is True

    def test_unrelated_file_does_not_match(self, harness_env):
        harness_env["settings"].write_text(json.dumps({"hooks": [{"command": "/usr/bin/true"}]}))
        assert status._references_install(harness_env["settings"]) is False

    def test_directory_is_scanned_for_a_matching_child(self, harness_env, tmp_path):
        """Kiro registers into ~/.kiro/agents/<name>.json, so the path is a dir."""
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "other.json").write_text("{}")
        (agents / "traced.json").write_text(json.dumps({"cmd": f"{harness_env['install_dir']}/venv/bin/hook"}))
        assert status._references_install(agents) is True

    def test_unreadable_path_is_not_a_crash(self, tmp_path):
        assert status._references_install(tmp_path / "absent.json") is False


# ---------------------------------------------------------------------------
# _registration_state
# ---------------------------------------------------------------------------


class TestRegistrationState:
    def test_unknown_harness_is_none_not_false(self, harness_env):
        """Never guess. A harness we cannot check is not a broken one."""
        assert status._registration_state("not-a-harness") == (None, None)

    def test_registered(self, harness_env):
        _register(harness_env["settings"], harness_env["install_dir"])
        registered, path = status._registration_state("claude-code")
        assert registered is True
        assert path == str(harness_env["settings"])

    def test_not_registered_when_file_exists_but_does_not_mention_us(self, harness_env):
        harness_env["settings"].write_text("{}")
        registered, path = status._registration_state("claude-code")
        assert registered is False
        assert path == str(harness_env["settings"])

    def test_every_candidate_is_checked_not_just_the_first(self, harness_env, tmp_path, monkeypatch):
        """omp registers through either a settings file or a plugin file.

        Stopping at the first *existing* path reported NOT registered whenever
        its settings file existed but the plugin was what wired it up.
        """
        settings = tmp_path / "omp-settings.json"
        plugin = tmp_path / "omp-plugin.ts"
        settings.write_text("{}")  # exists, but does not reference us
        plugin.write_text(f"import '{harness_env['install_dir']}/venv/x'")

        monkeypatch.setattr(status, "_FAKE_SETTINGS", settings, raising=False)
        monkeypatch.setattr(status, "_FAKE_PLUGIN", plugin, raising=False)
        monkeypatch.setattr(
            status,
            "_REGISTRATION",
            {"omp": ("core.setup.status", ("_FAKE_SETTINGS", "_FAKE_PLUGIN"))},
        )

        assert status._registration_state("omp") == (True, str(plugin))

    def test_a_raising_accessor_reports_unknown_rather_than_crashing(self, harness_env, monkeypatch):
        """An invalid CODEX_HOME raises. Saying "cannot tell" beats taking the
        command down — status exists to describe the install."""

        def _boom():
            raise ValueError("CODEX_HOME points to an invalid path")

        monkeypatch.setattr(status, "_FAKE_ACCESSOR", _boom, raising=False)
        monkeypatch.setattr(status, "_REGISTRATION", {"codex": ("core.setup.status", ("_FAKE_ACCESSOR",))})

        assert status._registration_state("codex") == (None, None)


# ---------------------------------------------------------------------------
# collect_status / exit codes
# ---------------------------------------------------------------------------


class TestCollectStatus:
    def test_no_config_is_empty_but_not_an_error(self, harness_env):
        payload = status.collect_status()
        assert payload["harnesses"] == []
        assert payload["healthy"] is False

    def test_reports_project_and_endpoint(self, harness_env):
        _write_config(
            harness_env["config_file"],
            {"claude-code": {"target": "atatus", "endpoint": "https://e", "api_key": "k", "project_name": "team"}},
        )
        _register(harness_env["settings"], harness_env["install_dir"])

        item = status.collect_status()["harnesses"][0]
        assert item["name"] == "claude-code"
        assert item["project_name"] == "team"
        assert item["endpoint"] == "https://e"
        assert item["registered"] is True

    def test_never_leaks_the_licence_key(self, harness_env):
        _write_config(
            harness_env["config_file"],
            {"claude-code": {"target": "atatus", "endpoint": "https://e", "api_key": "super-secret"}},
        )
        payload = status.collect_status()
        assert payload["harnesses"][0]["api_key_present"] is True
        assert "super-secret" not in json.dumps(payload)

    def test_unregistered_harness_is_listed_and_unhealthy(self, harness_env):
        _write_config(harness_env["config_file"], {"claude-code": {"target": "atatus", "api_key": "k"}})
        harness_env["settings"].write_text("{}")

        payload = status.collect_status()
        assert payload["unregistered"] == ["claude-code"]
        assert payload["healthy"] is False

    def test_uncheckable_harness_is_not_counted_as_broken(self, harness_env):
        _write_config(harness_env["config_file"], {"mystery": {"target": "atatus", "api_key": "k"}})
        payload = status.collect_status()
        assert payload["harnesses"][0]["registered"] is None
        assert payload["unregistered"] == []
        assert payload["healthy"] is True


class TestExitCodes:
    def test_nothing_configured_exits_1(self, harness_env, capsys):
        assert status.main([]) == 1
        assert "No harnesses configured." in capsys.readouterr().out

    def test_wired_up_exits_0(self, harness_env):
        _write_config(harness_env["config_file"], {"claude-code": {"target": "atatus", "api_key": "k"}})
        _register(harness_env["settings"], harness_env["install_dir"])
        assert status.main([]) == 0

    def test_hooks_missing_exits_2(self, harness_env, capsys):
        """0 used to cover this: the payload said registered:false while the
        process reported success, so anything gating on the exit code alone
        concluded the install was fine."""
        _write_config(harness_env["config_file"], {"claude-code": {"target": "atatus", "api_key": "k"}})
        harness_env["settings"].write_text("{}")

        assert status.main([]) == 2
        assert "Hooks are missing for: claude-code" in capsys.readouterr().out

    def test_json_output_is_parseable(self, harness_env, capsys):
        _write_config(harness_env["config_file"], {"claude-code": {"target": "atatus", "api_key": "k"}})
        _register(harness_env["settings"], harness_env["install_dir"])

        status.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["healthy"] is True
        assert payload["harnesses"][0]["name"] == "claude-code"

    def test_human_output_hides_the_logging_version_marker(self, harness_env, capsys):
        _write_config(
            harness_env["config_file"],
            {"claude-code": {"target": "atatus", "api_key": "k"}},
            logging={"_v": 2, "tool_content": True},
        )
        _register(harness_env["settings"], harness_env["install_dir"])

        status.main([])
        out = capsys.readouterr().out
        assert "tool_content=on" in out
        assert "_v=" not in out


class TestRegistrationTableCoverage:
    @staticmethod
    def _shipped_config_keys() -> set:
        """HARNESS_NAME from every tracing/*/constants.py — the keys that end up
        in config.json, and therefore the keys `status` has to recognise."""
        import pathlib
        import re

        root = pathlib.Path(__file__).parents[2] / "tracing"
        keys = set()
        for constants in sorted(root.glob("*/constants.py")):
            found = re.search(r'^HARNESS_NAME\s*=\s*"([^"]+)"', constants.read_text(), re.MULTILINE)
            if found:
                keys.add(found.group(1))
        return keys

    def test_every_shipped_harness_is_mapped(self):
        """A new harness must be one line in _REGISTRATION, not a silent "unknown".

        Discovered from the packages rather than hardcoded: the hardcoded version
        of this test passed while Devin and Antigravity both reported
        "registration unknown" from a real install.
        """
        shipped = self._shipped_config_keys()
        assert len(shipped) >= 10, f"only discovered {sorted(shipped)}"
        missing = shipped - set(status._REGISTRATION)
        assert not missing, f"harnesses missing from _REGISTRATION: {sorted(missing)}"

    def test_each_mapping_resolves_to_at_least_one_path(self):
        for harness in status._REGISTRATION:
            assert status._registration_paths(harness), f"{harness} resolved no registration path"
