"""Tests for the rewritten install.sh shell router.

Validates the thin shell router structure, dispatch logic, and smoke-test
behaviors specified in the task: help, no-args, and bogus-command.
"""

from __future__ import annotations

import os
import re
import shutil
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

        Raised 500 -> 520: the venv/ensurepip probe must run before anything is
        downloaded or written, so it is shell too.

        Raised 520 -> 560: the bounded git fetch and the source stamp that lets a
        no-change rerun skip pip both run before the venv is usable.
        """
        text = _read_install_sh()
        lines = text.strip().splitlines()
        assert len(lines) <= 560, f"install.sh has {len(lines)} lines — should be under 560"


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
            assert not re.search(pattern, self.text, re.MULTILINE), (
                f"Old function {old_func}() should be removed from the router"
            )


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
        assert 'run_harness_py "$key" "$vp" uninstall' in pre_wipe, (
            "Full uninstall does not dispatch per-harness uninstall before wipe"
        )

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
        assert '[[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]:-}" ]] || return 1' in self.sh
        assert '[[ "$self" == "$dir" ]]' in self.sh

    def test_piped_bash_has_no_bash_source(self):
        """`curl | bash -s -- update` leaves BASH_SOURCE empty; under `set -u` a
        bare `${BASH_SOURCE[0]}` test aborts with "unbound variable" (macOS)."""
        bare = [ln for ln in self.sh.splitlines() if re.search(r'-f "\$\{BASH_SOURCE\[0\]\}"', ln)]
        assert bare == []

    def test_the_fresh_copy_is_not_written_into_the_install_dir(self):
        """It would sit inside the tree the update is rewriting, and INSTALL_DIR
        is a checkout, so it would also show up as untracked there."""
        assert '"${TMPDIR:-/tmp}/atatus-install-update.sh"' in self.sh
        assert ".install-update.sh" not in self.sh

    def test_a_failed_fetch_falls_back_instead_of_aborting(self):
        assert "continuing with the local copy" in self.sh

    def test_the_fetch_is_time_bounded(self):
        """A hung network must not stall the update indefinitely."""
        assert "--connect-timeout 15" in self.sh
        assert 'download_file "$INSTALL_SH_URL" "$fresh" 8 0' in self.sh

    def test_tarball_download_retries_slow_tls(self):
        """A 5s connect timeout with no retry died on `curl: (28) SSL connection
        timeout` behind slow/filtered networks; the installer refresh stays
        single-shot so it cannot stall the update start."""
        assert '--retry "$retries" --retry-delay 2' in self.sh
        assert 'retries="${4:-2}"' in self.sh


class TestVenvProbeRunsBeforeAnythingIsWritten:
    """Debian/Ubuntu ship python3 without python3-venv. The old installer downloaded
    and extracted the tarball, then failed inside `python -m venv`, which leaves a
    venv with bin/python but no pip -- and the retry after `apt install` then
    reported "pip not found in venv" instead of installing.
    """

    FAKE_NO_ENSUREPIP = r"""#!/bin/bash
# python3 whose venv/ensurepip modules are missing, as shipped without python3-venv.
case "$*" in
  *"import venv, ensurepip"*) exit 1 ;;
  *"-m venv"*) d="${@: -1}"; mkdir -p "$d/bin"; cp /usr/bin/python3 "$d/bin/python"; echo "ensurepip is not available" >&2; exit 1 ;;
esac
exec /usr/bin/python3 "$@"
"""

    def _bash(self, script: str, home: str, extra_path: str = "") -> subprocess.CompletedProcess:
        env = {"HOME": home, "PATH": (extra_path + ":" if extra_path else "") + "/usr/bin:/bin", "NO_COLOR": "1"}
        # The file ends in `main "$@"`; load everything above it so the functions
        # can be called directly.
        functions = _read_install_sh().rsplit('main "$@"', 1)[0]
        return subprocess.run(
            ["bash", "-c", f"{functions}\n{script}"], capture_output=True, text=True, env=env, timeout=60
        )

    def _fake_python(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "python3").write_text(self.FAKE_NO_ENSUREPIP, encoding="utf-8")
        (bindir / "python3").chmod(0o755)
        return str(bindir)

    def test_probe_rejects_a_python_without_ensurepip_and_names_the_package(self, tmp_path):
        bindir = self._fake_python(tmp_path)
        r = self._bash(f'check_python_can_venv "{bindir}/python3"', str(tmp_path), bindir)
        assert r.returncode == 1
        assert "cannot create a virtual environment" in r.stderr
        assert "python3.12-venv" in r.stderr or "venv/ensurepip" in r.stderr

    def test_probe_accepts_a_python_that_can_venv(self, tmp_path):
        r = self._bash("check_python_can_venv /usr/bin/python3", str(tmp_path))
        assert r.returncode == 0, r.stderr

    def test_install_writes_nothing_when_the_probe_fails(self):
        """The probe sits between find_python and install_repo, so a machine that
        cannot build a venv gets the message and an untouched home directory."""
        sh = _read_install_sh()
        install = sh[sh.index("install_harness() {") : sh.index("usage() {")]
        assert install.index("check_python_can_venv") < install.index("install_repo")
        assert 'check_python_can_venv "$python_cmd" || exit 1' in install

    def test_a_failed_venv_creation_leaves_no_half_built_venv(self, tmp_path):
        bindir = self._fake_python(tmp_path)
        venv = tmp_path / "venv"
        r = self._bash(f'VENV_DIR="{venv}"; setup_venv "{bindir}/python3"', str(tmp_path), bindir)
        assert r.returncode == 1
        assert not venv.exists(), "debris from a failed venv creation must be removed"

    def test_a_venv_with_python_but_no_pip_is_rebuilt_not_reused(self, tmp_path):
        """The state a user is left in by the old installer after `apt install python3-venv`."""
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        shutil.copy("/usr/bin/python3", venv / "bin" / "python")
        script = f'VENV_DIR="{venv}"; pip_install_harness() {{ return 0; }}; setup_venv /usr/bin/python3'
        r = self._bash(script, str(tmp_path))
        assert r.returncode == 0, r.stderr
        assert "has no pip; rebuilding" in r.stdout
        assert (venv / "bin" / "pip").exists()


class TestBatMirrorsTheVenvHandling:
    """install.sh and install.bat must not drift: whatever the shell installer
    checks or repairs around Python and the venv, the batch installer does too."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.sh = _read_install_sh()
        self.bat = _read_install_bat()

    def test_both_probe_venv_capability_before_fetching_anything(self):
        assert "import venv, ensurepip" in self.sh
        assert "import venv, ensurepip" in self.bat
        bat_install = self.bat[
            self.bat.index("REM --- Install a harness ---") : self.bat.index("REM --- cmd_status ---")
        ]
        assert bat_install.index("call :check_python_can_venv") < bat_install.index("call :bootstrap_repo")
        assert (
            "if %ERRORLEVEL% neq 0 exit /b 1"
            in bat_install.split("call :check_python_can_venv", 1)[1].split("call :bootstrap_repo", 1)[0]
        )

    def test_both_rebuild_a_venv_that_has_python_but_no_pip(self):
        assert "has no pip; rebuilding it" in self.sh
        assert "has no pip; rebuilding it" in self.bat
        assert 'rmdir /s /q "%VENV_DIR%"' in self.bat

    def test_both_remove_the_debris_of_a_failed_venv_creation(self):
        sh_fail = self.sh.split('"$python_cmd" -m venv "$VENV_DIR"', 1)[1].split("return 1", 1)[0]
        assert 'rm -rf "$VENV_DIR"' in sh_fail
        bat_fail = self.bat.split('%FOUND_PYTHON% -m venv "%VENV_DIR%"', 1)[1].split("exit /b 1", 1)[0]
        assert 'rmdir /s /q "%VENV_DIR%"' in bat_fail

    def test_both_name_the_fix_when_python_is_missing(self):
        assert "No Python 3.9+ found. Install it with:" in self.sh
        assert self.bat.count("No Python 3.9+ found. Install it from") == 2, "install and update paths"
        assert "Python 3.9+ is required" not in self.bat


class TestReinstallIsFastAndNeverHangs:
    """A rerun of the documented install line on a machine that already has the
    harness used to make four sequential git round trips (a silent minute on a bad
    link) and then a full pip reinstall of an unchanged tree. Both installers now
    make one bounded fetch, never prompt for credentials, fall back to the tarball
    over the existing tree, and reuse a venv built from the same source."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.sh = _read_install_sh()
        self.bat = _read_install_bat()

    def test_git_never_prompts_and_is_time_bounded(self):
        assert "GIT_TERMINAL_PROMPT=0" in self.sh and "GIT_TERMINAL_PROMPT=0" in self.bat
        assert "core.askPass=true" in self.sh and "core.askPass=true" in self.bat
        assert "http.lowSpeedTime=10" in self.sh and "http.lowSpeedTime=10" in self.bat
        assert "timeout_cmd 20 git" in self.sh
        assert self.bat.count("core.askPass=true") == 2, "install sync and update pull"

    def test_one_fetch_then_the_tarball(self):
        sync = self.sh[self.sh.index("git_sync_harness_repo() {") : self.sh.index("# True only when this script")]
        assert sync.count("fetch --depth 1") == 1 and "pull --ff-only" not in sync
        bat_sync = self.bat[self.bat.index("\n:bootstrap_repo") : self.bat.index("\n:download_tarball")]
        assert bat_sync.count("fetch --depth 1") == 1 and "pull --ff-only" not in bat_sync
        assert "call :download_tarball" in bat_sync.split("git fetch failed", 1)[1]

    def test_a_failed_fetch_does_not_wipe_the_install(self):
        """The venv lives inside INSTALL_DIR; the old .bat rmdir'd it on every flaky network."""
        bat_sync = self.bat[self.bat.index("\n:bootstrap_repo") : self.bat.index("\n:download_tarball")]
        assert 'rmdir /s /q "%INSTALL_DIR%"' not in bat_sync
        assert "re-cloning" not in self.bat

    def test_unchanged_tree_skips_pip_in_both(self):
        for src in (self.sh, self.bat):
            assert ".atatus-source" in src
            assert "already current" in src
        assert "record_venv_source" in self.sh.split('pip_install_harness "$pip" -U', 1)[1][:80]
        assert "call :record_venv_source" in self.bat

    def test_tarball_download_is_time_bounded_in_both(self):
        assert "--max-time" in self.sh
        assert "-TimeoutSec 120" in self.bat and "--max-time 120" in self.bat

    def test_venv_reuse_is_behavioural_not_just_import_core(self, tmp_path):
        """A venv from an older tree must be reinstalled, not reused because `import core` works."""
        functions = _read_install_sh().rsplit('main "$@"', 1)[0]
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        shutil.copy("/usr/bin/python3", venv / "bin" / "python")
        (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
        (venv / "bin" / "pip").chmod(0o755)
        install = tmp_path / "harness"
        (install / "core").mkdir(parents=True)
        (install / "core" / "__init__.py").write_text("")
        (install / "tracing").mkdir()
        (install / "pyproject.toml").write_text("[project]\nname='x'\n")
        (venv / ".atatus-source").write_text("stale-stamp")
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
        script = (
            f'{functions}\nINSTALL_DIR="{install}"; VENV_DIR="{venv}"; WHEEL_DIR=""; '
            f"pip_install_harness() {{ echo PIP-RAN; }}; setup_venv /usr/bin/python3"
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        assert "PIP-RAN" in r.stdout, r.stderr
        assert (venv / ".atatus-source").read_text().strip() != "stale-stamp"
        r2 = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        assert "PIP-RAN" not in r2.stdout and "already current" in r2.stdout
