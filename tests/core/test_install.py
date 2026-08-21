"""Tests for install.sh / install.bat — the thin shell/batch router."""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_BAT = REPO_ROOT / "install.bat"

ALL_HARNESSES = ["claude", "codex", "copilot", "cursor", "opencode", "omp"]


# ---------------------------------------------------------------------------
# File existence and basic validity
# ---------------------------------------------------------------------------


def test_install_sh_exists():
    """install.sh must exist at repo root."""
    assert INSTALL_SH.is_file()


def test_install_bat_exists():
    """install.bat must exist at repo root."""
    assert INSTALL_BAT.is_file()


def test_install_sh_is_executable():
    """install.sh must be executable."""
    assert os.access(INSTALL_SH, os.X_OK)


def test_install_sh_has_bash_shebang():
    """install.sh must start with a bash shebang."""
    first_line = INSTALL_SH.read_text().splitlines()[0]
    assert first_line.startswith("#!/bin/bash"), f"Expected bash shebang, got: {first_line}"


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_syntax_valid():
    """install.sh must parse without syntax errors."""
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Bash syntax error: {result.stderr}"


# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_help():
    """install.sh --help exits 0 and shows usage."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Usage" in result.stdout or "usage" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_no_args_exits_nonzero():
    """install.sh with no arguments should exit with error."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_unknown_command_exits_nonzero():
    """install.sh with unknown command should exit with error."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "bogus"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Command surface — router must name every harness + update + uninstall
# ---------------------------------------------------------------------------


def test_install_sh_has_all_commands():
    """install.sh must support claude, codex, copilot, cursor, update, uninstall."""
    text = INSTALL_SH.read_text()
    for cmd in ALL_HARNESSES + ["update", "uninstall"]:
        assert cmd in text, f"Missing command: {cmd}"


def test_install_bat_has_all_commands():
    """install.bat must support claude, codex, copilot, cursor, update, uninstall."""
    text = INSTALL_BAT.read_text()
    for cmd in ALL_HARNESSES + ["update", "uninstall"]:
        assert cmd.lower() in text.lower(), f"Missing command: {cmd}"


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_help_output_names_every_harness():
    """--help output must mention every supported harness."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    output = result.stdout
    for harness in ALL_HARNESSES:
        assert harness in output, f"--help output missing harness: {harness}"


# ---------------------------------------------------------------------------
# Router dispatches to per-harness install.py
# ---------------------------------------------------------------------------


def test_install_sh_references_install_py():
    """The router MUST reference install.py — it dispatches to per-harness scripts."""
    text = INSTALL_SH.read_text()
    assert "install.py" in text


def test_install_bat_references_install_py():
    """The router MUST reference install.py — it dispatches to per-harness scripts."""
    text = INSTALL_BAT.read_text()
    assert "install.py" in text


def test_install_sh_harness_dir_mapping():
    """Router must map each harness name to its <harness>-tracing directory."""
    text = INSTALL_SH.read_text()
    assert "tracing/claude_code" in text
    assert "tracing/codex" in text
    assert "tracing/copilot" in text
    assert "tracing/cursor" in text
    assert "tracing/opencode" in text
    assert "tracing/omp" in text


def test_install_bat_harness_dir_mapping():
    """install.bat must map each harness name to its tracing\\<harness> directory."""
    text = INSTALL_BAT.read_text()
    assert "tracing\\claude_code" in text
    assert "tracing\\codex" in text
    assert "tracing\\copilot" in text
    assert "tracing\\cursor" in text
    assert "tracing\\opencode" in text
    assert "tracing\\omp" in text


# ---------------------------------------------------------------------------
# Git sync / branch support
# ---------------------------------------------------------------------------


def test_install_sh_existing_repo_syncs_requested_branch():
    """Existing harness git dir must fetch/checkout INSTALL_BRANCH."""
    text = INSTALL_SH.read_text()
    assert "Syncing with origin/" in text
    assert "checkout -B" in text and "FETCH_HEAD" in text


# ---------------------------------------------------------------------------
# Venv / pip
# ---------------------------------------------------------------------------


def test_install_sh_uses_pip_install_package():
    """install.sh must install the package via pip."""
    text = INSTALL_SH.read_text()
    assert 'pip" install' in text or "pip install" in text


def test_install_sh_no_jq_dependency():
    """install.sh must not require jq."""
    text = INSTALL_SH.read_text()
    assert "jq is required" not in text


# ---------------------------------------------------------------------------
# update re-execs from a fresh installer, on both platforms
# ---------------------------------------------------------------------------


def test_install_bat_update_refetches_itself():
    """cmd.exe re-reads a batch file by offset after every line, so an update
    that rewrites install.bat underneath itself is worse on Windows than on
    POSIX. It must hand over to a freshly downloaded copy."""
    text = INSTALL_BAT.read_text()
    assert "raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/!INSTALL_BRANCH!/install.bat" in text
    assert 'call "!FRESH!" update --branch !INSTALL_BRANCH!' in text


def test_install_bat_reexec_is_guarded_against_looping():
    text = INSTALL_BAT.read_text()
    assert "if not defined ATATUS_UPDATE_REEXEC if not defined WHEEL_DIR" in text
    assert 'set "ATATUS_UPDATE_REEXEC=1"' in text


def test_install_bat_falls_back_when_the_fetch_fails():
    assert "continuing with the local copy" in INSTALL_BAT.read_text()


def test_install_bat_only_refetches_the_installed_copy():
    """The documented Windows update runs the installer from TEMP, which the
    update never rewrites. Only the copy in INSTALL_DIR needs the hand-over."""
    text = INSTALL_BAT.read_text()
    assert 'set "SELF_DIR=%~dp0"' in text
    assert 'if /i "!SELF_DIR!"=="%INSTALL_DIR%\\"' in text


def test_neither_installer_writes_into_the_install_dir():
    """The fresh copy must not land in the tree the update is rewriting, and
    INSTALL_DIR is a checkout, so a stray file would show up untracked there."""
    assert '"${TMPDIR:-/tmp}/atatus-install-update.sh"' in INSTALL_SH.read_text()
    assert '%TEMP%\\atatus-install-update.bat' in INSTALL_BAT.read_text()
