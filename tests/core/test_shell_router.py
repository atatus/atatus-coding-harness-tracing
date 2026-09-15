"""Tests for the rewritten install.sh shell router.

Validates the thin shell router structure, dispatch logic, and smoke-test
behaviors specified in the task: help, no-args, and bogus-command.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

INSTALL_SH = os.path.join(os.path.dirname(__file__), "..", "..", "install.sh")


def _read_install_sh() -> str:
    with open(INSTALL_SH) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Syntax & structure tests
# ---------------------------------------------------------------------------


class TestShellSyntax:
    """Verify the script is syntactically valid bash."""

    def test_bash_syntax_check(self):
        """bash -n parses the file without errors."""
        result = subprocess.run(
            ["bash", "-n", INSTALL_SH],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Syntax error:\n{result.stderr}"

    def test_starts_with_shebang(self):
        text = _read_install_sh()
        assert text.startswith("#!/bin/bash"), "Missing bash shebang"

    def test_set_euo_pipefail(self):
        text = _read_install_sh()
        assert "set -euo pipefail" in text, "Missing strict mode"

    def test_line_count_under_cap(self):
        """Router should stay small, well under the old 1919 lines.

        Cap raised 400 -> 460 for non-interactive install, `status` and
        `--wheel-dir`: flag parsing and the pip invocation both run *before* the
        venv exists, so neither can move into core/setup/.

        Raised again 460 -> 500: `update` now re-execs from a freshly fetched
        installer, which has to happen before any of the update runs and so
        cannot live in core/setup/ either.
        """
        text = _read_install_sh()
        lines = text.strip().splitlines()
        assert len(lines) <= 500, f"install.sh has {len(lines)} lines — should be under 500"


# ---------------------------------------------------------------------------
# Function definition tests
# ---------------------------------------------------------------------------


class TestFunctionsDefined:
    """Verify that all required shell functions exist."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    @pytest.mark.parametrize(
        "func",
        [
            "info",
            "warn",
            "err",
            "header",
            "command_exists",
            "find_python",
            "venv_python",
            "venv_pip",
            "git_sync_harness_repo",
            "install_repo_tarball",
            "install_repo",
            "run_harness_py",
            "pip_install_harness",
            "setup_venv",
            "harness_dir",
            "usage",
            "main",
        ],
    )
    def test_function_defined(self, func):
        # Match "funcname() {" or "funcname ()" patterns
        pattern = rf"^{func}\s*\(\)"
        assert re.search(pattern, self.text, re.MULTILINE), f"Function {func}() not defined in install.sh"

    def test_no_old_setup_functions(self):
        """Old monolith functions should be removed."""
        for old_func in [
            "setup_claude",
            "setup_cursor",
            "setup_codex",
            "setup_copilot",
            "setup_shared_runtime",
            "do_uninstall",
            "update_install",
            "write_config",
            "collect_backend_credentials",
            "install_skills",
            # Dead on arrival: defined, never called. Python's getpass replaced the
            # masked-input one. Listed here so they cannot creep back.
            "tty_input",
            "tty_read_masked_line",
        ]:
            pattern = rf"^{old_func}\s*\(\)"
            assert not re.search(
                pattern, self.text, re.MULTILINE
            ), f"Old function {old_func}() should be removed from the router"


# ---------------------------------------------------------------------------
# Harness name mapping tests
# ---------------------------------------------------------------------------


class TestHarnessMapping:
    """Verify the harness_dir case statement maps correctly."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_claude_maps_to_tracing_claude_code(self):
        assert 'claude|claude-code)  echo "tracing/claude_code"' in self.text

    def test_claude_code_config_key_is_aliased(self):
        """`update` and full `uninstall` look harnesses up by *config key*.

        Claude Code is the only harness whose config key ("claude-code") differs
        from its CLI name ("claude"). Without the alias both loops skipped it, so
        a full uninstall wiped the venv and left every hook in
        ~/.claude/settings.json pointing at the deleted path.
        """
        import re as _re

        body = _re.search(r"harness_dir\(\) \{(.*?)\n\}", self.text, _re.S)
        assert body, "harness_dir() not found"
        assert "claude-code" in body.group(1)

    def test_codex_maps_to_tracing_codex(self):
        assert 'codex)   echo "tracing/codex"' in self.text

    def test_copilot_maps_to_tracing_copilot(self):
        assert 'copilot) echo "tracing/copilot"' in self.text

    def test_cursor_maps_to_tracing_cursor(self):
        assert 'cursor)  echo "tracing/cursor"' in self.text

    def test_opencode_maps_to_tracing_opencode(self):
        assert "opencode)" in self.text and '"tracing/opencode"' in self.text

    def test_omp_maps_to_tracing_omp(self):
        assert "omp)" in self.text and '"tracing/omp"' in self.text


# ---------------------------------------------------------------------------
# Usage output tests
# ---------------------------------------------------------------------------


class TestUsageOutput:
    """Verify the usage() function includes all required content."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_title(self):
        assert "Atatus Coding Harness Tracing Installer" in self.text

    @pytest.mark.parametrize(
        "cmd",
        ["claude", "codex", "copilot", "cursor", "opencode", "omp", "update", "uninstall"],
    )
    def test_command_listed(self, cmd):
        assert cmd in self.text

    def test_with_skills_flag(self):
        assert "--with-skills" in self.text

    def test_branch_flag(self):
        assert "--branch NAME" in self.text


# ---------------------------------------------------------------------------
# Smoke tests (subprocess execution)
# ---------------------------------------------------------------------------


class TestSmokeTests:
    """Run the actual script with safe arguments."""

    def _run(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "NO_COLOR": "1"}
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", INSTALL_SH, *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

    def test_help_exits_zero(self):
        result = self._run("--help")
        assert result.returncode == 0
        assert "Atatus Coding Harness Tracing Installer" in result.stdout

    def test_help_flag_h(self):
        result = self._run("-h")
        assert result.returncode == 0
        assert "Usage:" in result.stdout

    def test_help_word(self):
        result = self._run("help")
        assert result.returncode == 0

    def test_no_args_exits_nonzero(self):
        result = self._run()
        assert result.returncode != 0
        assert "Usage:" in result.stdout

    def test_bogus_command_exits_nonzero(self):
        result = self._run("bogus")
        assert result.returncode != 0
        assert "Unknown command" in result.stderr

    def test_uninstall_bogus_harness_exits_nonzero(self):
        """uninstall <invalid> should fail."""
        result = self._run("uninstall", "invalid-harness")
        assert result.returncode != 0

    def test_update_without_install_fails(self):
        """update should fail if no venv exists at ~/.atatus/harness/venv."""
        # Use a fake HOME so we don't touch real install
        result = self._run("update", env_extra={"HOME": "/tmp/atatus-test-nonexistent"})
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Dispatch logic tests
# ---------------------------------------------------------------------------


class TestDispatchLogic:
    """Verify that the main() case statement dispatches correctly."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    @pytest.mark.parametrize(
        "harness", ["claude", "codex", "copilot", "cursor", "gemini", "kiro", "opencode", "omp", "devin"]
    )
    def test_dispatches_harness_commands(self, harness):
        """Every harness must appear in the install dispatch alternation.

        Asserted per-harness rather than as one literal alternation string: the
        literal broke on every harness added, which pushes people towards editing
        the assertion instead of checking the router.
        """
        import re as _re

        match = _re.search(r"^\s{8}(claude\|[a-z|]+)\)$", self.text, _re.M)
        assert match, "install dispatch alternation not found"
        assert harness in match.group(1).split("|")

    def test_install_harness_called(self):
        """install_harness function should be called for harness commands."""
        assert 'install_harness "$cmd"' in self.text

    def test_install_harness_defined(self):
        """install_harness must be defined if it's called."""
        # This is a critical check: the function is called but must exist
        calls = re.findall(r"install_harness\b", self.text)
        definitions = re.findall(r"^install_harness\s*\(\)", self.text, re.MULTILINE)
        if calls:
            assert len(definitions) > 0, (
                "install_harness is called but never defined — "
                "this will cause claude/codex/copilot/cursor commands to fail"
            )

    def test_uninstall_dispatches_to_python(self):
        """Uninstall with harness should dispatch to <dir>/install.py uninstall."""
        # The actual line is: "$vp" "${INSTALL_DIR}/${dir}/install.py" uninstall
        assert "install.py" in self.text and "uninstall" in self.text

    def test_full_uninstall_dispatches_to_wipe(self):
        """Uninstall without harness should call core.setup.wipe."""
        assert "core.setup.wipe" in self.text

    def test_full_uninstall_runs_per_harness_uninstall_before_wipe(self):
        """Full uninstall must iterate installed harnesses and call each
        harness's install.py uninstall before the shared-runtime wipe.

        Regression guard: wipe.py intentionally does NOT touch
        ~/.claude/settings.json, ~/.cursor/hooks.json, ~/.codex/config.toml,
        or .github/hooks/*. Callers must run each harness uninstall first to
        clean those external registrations. install.bat does this; install.sh
        previously omitted it, leaving orphaned hook entries after full
        uninstall.
        """
        # Extract the full-uninstall branch (the `else` clause after
        # `if [[ -n "$subcmd" ]]`). It must list harnesses and dispatch
        # each harness's uninstall BEFORE running wipe.
        text = self.text
        wipe_idx = text.find('"$vp" -m core.setup.wipe')
        assert wipe_idx >= 0, "wipe call not found"

        # The list_installed_harnesses invocation must appear before the
        # wipe call, and an uninstall dispatch must appear between them.
        # run_harness_py is the dispatcher now — it picks the source-tree
        # install.py or the installed module, depending on install mode.
        pre_wipe = text[:wipe_idx]
        assert "list_installed_harnesses" in pre_wipe, "Full uninstall does not iterate installed harnesses before wipe"
        assert (
            'run_harness_py "$key" "$vp" uninstall' in pre_wipe
        ), "Full uninstall does not dispatch per-harness uninstall before wipe"

    def test_update_calls_pip_install(self):
        assert "pip" in self.text and "install" in self.text

    def test_update_lists_installed_harnesses(self):
        assert "list_installed_harnesses" in self.text


# ---------------------------------------------------------------------------
# Flag parsing tests
# ---------------------------------------------------------------------------


class TestFlagParsing:
    """Verify flag parsing in main()."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_with_skills_flag_parsed(self):
        assert "--with-skills)" in self.text
        assert "with_skills=true" in self.text

    def test_branch_flag_parsed(self):
        assert "--branch)" in self.text
        assert "INSTALL_BRANCH=" in self.text

    def test_env_var_default_branch(self):
        assert "ATATUS_INSTALL_BRANCH" in self.text


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify the script declares expected constants."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_repo_url(self):
        assert "https://github.com/atatus/atatus-coding-harness-tracing.git" in self.text

    def test_install_dir(self):
        assert "${HOME}/.atatus/harness" in self.text

    def test_venv_dir(self):
        assert "${INSTALL_DIR}/venv" in self.text

    def test_tarball_url(self):
        assert "archive/refs/heads/" in self.text


# ---------------------------------------------------------------------------
# Non-interactive / status / wheel-dir flags
# ---------------------------------------------------------------------------


BAT = os.path.join(os.path.dirname(__file__), "..", "..", "install.bat")


def _read_install_bat() -> str:
    with open(BAT) as f:
        return f.read()


class TestNonInteractiveFlags:
    """Both routers must expose the same flags — Windows was the half that
    silently lagged before (see the claude-code alias)."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.sh = _read_install_sh()
        self.bat = _read_install_bat()

    def test_sh_exports_noninteractive(self):
        assert "--non-interactive|-y) export ATATUS_NONINTERACTIVE=1" in self.sh

    def test_bat_exports_noninteractive(self):
        assert '"--non-interactive" ( set "ATATUS_NONINTERACTIVE=1"' in self.bat
        assert '"-y" ( set "ATATUS_NONINTERACTIVE=1"' in self.bat

    def test_sh_has_status_command(self):
        assert "core.setup.status $status_args" in self.sh

    def test_bat_has_status_command(self):
        assert "core.setup.status %STATUS_ARGS%" in self.bat

    def test_status_is_listed_in_both_usages(self):
        assert "status      Report configured harnesses" in self.sh
        assert "status              Report configured harnesses" in self.bat

    def test_update_forces_noninteractive(self):
        """update re-registers every harness, which prompts for the project name.

        With no terminal that died with an unhandled EOFError partway through,
        leaving some harnesses re-registered and others not. Both installers used
        to set the flag only when no terminal was detected; they now set it for
        every update, since an update re-registers what is already configured and
        has nothing to ask about either way.
        """
        assert "export ATATUS_NONINTERACTIVE=1" in self.sh
        assert 'set "ATATUS_NONINTERACTIVE=1"' in self.bat


class TestWheelDir:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.sh = _read_install_sh()
        self.bat = _read_install_bat()

    def test_env_var_seeds_the_flag(self):
        assert 'WHEEL_DIR="${ATATUS_WHEEL_DIR:-}"' in self.sh
        assert 'set "WHEEL_DIR=%ATATUS_WHEEL_DIR%"' in self.bat

    def test_pip_never_reaches_the_index_offline(self):
        """--no-index so a missing wheel fails loudly instead of quietly
        reaching PyPI, which would defeat installing offline."""
        assert '--no-index --find-links "$WHEEL_DIR" atatus-coding-harness-tracing' in self.sh
        assert '--no-index --find-links "%WHEEL_DIR%" atatus-coding-harness-tracing' in self.bat

    def test_wheel_presence_is_validated_up_front(self):
        assert "atatus_coding_harness_tracing-*.whl" in self.sh
        assert "atatus_coding_harness_tracing-*.whl" in self.bat

    def test_installsh_is_placed_for_later_commands(self):
        """status/update/uninstall are documented as running from INSTALL_DIR;
        repo mode gets install.sh via the extract, wheel mode must copy it."""
        assert '"${INSTALL_DIR}/install.sh"' in self.sh
        assert '"%INSTALL_DIR%\\install.bat"' in self.bat

    def test_update_refuses_to_convert_offline_to_network(self):
        assert "offline install with no source tree to update" in self.sh
        assert "offline install with no source tree to update" in self.bat


class TestBatHarnessMapping:
    """install.bat has always accepted both spellings; install.sh now matches."""

    def test_bat_accepts_claude_code_config_key(self):
        assert '"claude-code" set "HARNESS_DIR=tracing\\claude_code"' in _read_install_bat()


class TestConfigKeysResolve:
    """Every harness's *config key* must resolve in harness_dir().

    `update` and full `uninstall` discover harnesses via
    list_installed_harnesses(), which yields config keys (HARNESS_NAME), not CLI
    names. A key the router cannot map is skipped with a warning — which for a
    full uninstall wiped the venv and left that harness's hooks pointing at the
    deleted path, after printing "Uninstall complete."

    Discovered from the constants rather than hardcoded, so a future harness
    whose config key differs from its CLI name fails here instead of in the field.
    """

    @staticmethod
    def _config_keys() -> list[str]:
        root = os.path.join(os.path.dirname(__file__), "..", "..", "tracing")
        keys = []
        for entry in sorted(os.listdir(root)):
            constants = os.path.join(root, entry, "constants.py")
            if not os.path.isfile(constants):
                continue
            with open(constants) as f:
                found = re.search(r'^HARNESS_NAME\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
            if found:
                keys.append(found.group(1))
        return keys

    def test_constants_were_actually_found(self):
        """An empty list would make the test below vacuously green."""
        keys = self._config_keys()
        assert len(keys) >= 8, f"only found {keys}"
        assert "claude-code" in keys, "the case this test exists for"

    @pytest.mark.parametrize("key", _config_keys.__func__())
    def test_config_key_maps_to_a_directory(self, key):
        result = subprocess.run(
            ["bash", "-c", f'source <(sed -n "/^harness_dir() {{/,/^}}/p" "{INSTALL_SH}"); harness_dir "{key}"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"harness_dir does not accept config key {key!r}"
        assert result.stdout.strip().startswith("tracing/")


# ---------------------------------------------------------------------------
# `update` re-execs from a freshly fetched installer
# ---------------------------------------------------------------------------


class TestUpdateRefetchesItself:
    """An update rewrites install.sh while bash is still reading it by offset.

    Continuing in the replaced file is what corrupts a half-finished update, so
    `update` downloads a fresh copy and hands over to it.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.sh = _read_install_sh()

    def test_installer_url_is_branch_derived(self):
        assert (
            'INSTALL_SH_URL="https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/${INSTALL_BRANCH}/install.sh"'
            in self.sh
        )

    def test_branch_urls_are_set_in_one_place(self):
        """--branch must not update the tarball URL and miss the installer URL."""
        assert self.sh.count("set_branch_urls") == 3  # definition + two callers
        assert self.sh.count("archive/refs/heads/${INSTALL_BRANCH}.tar.gz") == 1

    def test_update_execs_the_fresh_copy(self):
        assert 'exec bash "$fresh" update ${args[@]+"${args[@]}"}' in self.sh

    def test_reexec_is_guarded_against_looping(self):
        assert '-z "${ATATUS_UPDATE_REEXEC:-}"' in self.sh
        assert "export ATATUS_UPDATE_REEXEC=1" in self.sh

    def test_only_the_installed_copy_refetches(self):
        """The overwrite hazard exists only for the copy inside INSTALL_DIR.

        The documented paths -- `curl | bash -s -- update` and, on Windows, the
        installer downloaded to TEMP -- are already current and are never a
        target of the pull or the extract, so they must not fetch anything.
        """
        guard = [ln for ln in self.sh.splitlines() if "ATATUS_UPDATE_REEXEC:-" in ln][0]
        assert '-z "$WHEEL_DIR"' in guard
        assert "running_from_install_dir" in guard
        assert '[[ -f "${BASH_SOURCE[0]}" ]] || return 1' in self.sh
        assert '[[ "$self" == "$dir" ]]' in self.sh

    def test_the_fresh_copy_is_not_written_into_the_install_dir(self):
        """It would sit inside the tree the update is rewriting, and INSTALL_DIR
        is a checkout, so it would also show up as untracked there."""
        assert '"${TMPDIR:-/tmp}/atatus-install-update.sh"' in self.sh
        assert ".install-update.sh" not in self.sh

    def test_a_failed_fetch_falls_back_instead_of_aborting(self):
        assert "continuing with the local copy" in self.sh

    def test_the_fetch_is_time_bounded(self):
        """A hung network must not stall the update indefinitely."""
        assert "--connect-timeout 5" in self.sh
        assert 'download_file "$INSTALL_SH_URL" "$fresh" 8' in self.sh
