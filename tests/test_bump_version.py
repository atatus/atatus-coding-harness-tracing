"""bump_version.sh keeps the release version identical in every file that carries it."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bump_version.sh"


def _run(*args, root=None):
    cmd = ["bash", str(SCRIPT), *args]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True)


def _targets():
    return [line.split()[0] for line in _run("--check").stdout.splitlines()[:-1]]


@pytest.fixture
def repo(tmp_path):
    for relative in _targets():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / relative, tmp_path / relative)
    return tmp_path


def _snapshot(root):
    return {relative: (root / relative).read_text(encoding="utf-8") for relative in _targets()}


def test_the_repo_carries_one_version():
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_targets()) == 9


def test_bump_writes_the_version_into_every_target(repo):
    result = _run("9.8.7", root=repo)
    assert result.returncode == 0, result.stderr
    assert _run("--check", root=repo).returncode == 0
    for relative, content in _snapshot(repo).items():
        assert "9.8.7" in content, relative


def test_bump_changes_nothing_but_the_version(repo):
    before = _snapshot(repo)
    current = _run("--check", root=repo).stdout.split()[-1]
    assert _run("9.8.7", root=repo).returncode == 0
    for relative, content in _snapshot(repo).items():
        assert content.replace("9.8.7", current) == before[relative], relative


def test_check_names_the_file_that_disagrees(repo):
    assert _run("9.8.7", root=repo).returncode == 0
    plugin = repo / "tracing/claude_code/.claude-plugin/plugin.json"
    plugin.write_text(plugin.read_text(encoding="utf-8").replace("9.8.7", "9.8.6"), encoding="utf-8")
    result = _run("--check", root=repo)
    assert result.returncode == 1
    assert "tracing/claude_code/.claude-plugin/plugin.json" in result.stdout


@pytest.mark.parametrize("bad", ["1.0", "v1.0.1", "1.0.x", ""])
def test_a_malformed_version_is_rejected_and_nothing_is_written(repo, bad):
    before = _snapshot(repo)
    result = _run(bad, root=repo)
    assert result.returncode == 2
    assert _snapshot(repo) == before


def test_a_target_that_lost_its_version_fails_before_anything_is_written(repo):
    pyproject = repo / "tracing/cursor/pyproject.toml"
    pyproject.write_text(
        "\n".join(
            line for line in pyproject.read_text(encoding="utf-8").splitlines() if not line.startswith("version")
        ),
        encoding="utf-8",
    )
    before = _snapshot(repo)
    result = _run("9.8.7", root=repo)
    assert result.returncode == 1
    assert "tracing/cursor/pyproject.toml" in result.stderr
    assert _snapshot(repo) == before
