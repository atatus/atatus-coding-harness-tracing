#!/usr/bin/env python3
"""Tests for hook registration updates (task-13).

Validates that:
- plugin.json is valid JSON with correct CLI entry points
- No bash/jq/curl references remain in harness docs
- All CLI commands referenced in docs exist in pyproject.toml
- Documentation consistency across all three harnesses
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

# --- Fixtures ---

HARNESS_DIRS = ["tracing/claude_code", "tracing/codex", "tracing/cursor"]

EXPECTED_ENTRY_POINTS = {
    "atatus-config": "core.config:main",
    "atatus-hook-session-start": "tracing.claude_code.hooks.handlers:session_start",
    "atatus-hook-pre-tool-use": "tracing.claude_code.hooks.handlers:pre_tool_use",
    "atatus-hook-post-tool-use": "tracing.claude_code.hooks.handlers:post_tool_use",
    "atatus-hook-user-prompt-submit": "tracing.claude_code.hooks.handlers:user_prompt_submit",
    "atatus-hook-stop": "tracing.claude_code.hooks.handlers:stop",
    "atatus-hook-subagent-stop": "tracing.claude_code.hooks.handlers:subagent_stop",
    "atatus-hook-stop-failure": "tracing.claude_code.hooks.handlers:stop_failure",
    "atatus-hook-notification": "tracing.claude_code.hooks.handlers:notification",
    "atatus-hook-permission-request": "tracing.claude_code.hooks.handlers:permission_request",
    "atatus-hook-session-end": "tracing.claude_code.hooks.handlers:session_end",
    "atatus-hook-codex-notify": "tracing.codex.hooks.handlers:notify",
    "atatus-hook-cursor": "tracing.cursor.hooks.handlers:main",
}


def _parse_pyproject_scripts():
    """Parse [project.scripts] from pyproject.toml."""
    content = (REPO_ROOT / "pyproject.toml").read_text()
    scripts = {}
    in_scripts = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "[project.scripts]":
            in_scripts = True
            continue
        if in_scripts:
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            if stripped.startswith("#") or not stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"')
            if key and value:
                scripts[key] = value
    return scripts


def _collect_md_files():
    """Collect all .md files in harness directories."""
    files = []
    for d in HARNESS_DIRS:
        harness_dir = REPO_ROOT / d
        if harness_dir.exists():
            files.extend(harness_dir.rglob("*.md"))
    return files


def _collect_json_files():
    """Collect all .json files in harness directories."""
    files = []
    for d in HARNESS_DIRS:
        harness_dir = REPO_ROOT / d
        if harness_dir.exists():
            files.extend(harness_dir.rglob("*.json"))
    return files


# --- plugin.json tests ---


class TestPluginJson:
    """Tests for tracing.claude_code/.claude-plugin/plugin.json."""

    @pytest.fixture
    def plugin_data(self):
        path = REPO_ROOT / "tracing" / "claude_code" / ".claude-plugin" / "plugin.json"
        with open(path) as f:
            return json.load(f)

    def test_valid_json(self, plugin_data):
        """plugin.json must be valid JSON (implicitly tested by fixture loading)."""
        assert isinstance(plugin_data, dict)

    def test_has_required_metadata(self, plugin_data):
        """plugin.json must have name, description, version."""
        assert "name" in plugin_data
        assert "description" in plugin_data
        assert "version" in plugin_data


class TestHooksJson:
    """Tests for tracing.claude_code/hooks/hooks.json."""

    @pytest.fixture
    def hooks_data(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "hooks" / "hooks.json"
        with open(path) as f:
            return json.load(f)

    def test_valid_json(self, hooks_data):
        """hooks.json must be valid JSON."""
        assert isinstance(hooks_data, dict)
        assert "hooks" in hooks_data

    def test_matches_hook_events_exactly(self, hooks_data):
        """hooks.json must register exactly what HOOK_EVENTS does.

        There are two registration surfaces for Claude Code and they had
        drifted: `install.py` writes ~/.claude/settings.json from
        `constants.HOOK_EVENTS` (16 events), while this plugin manifest listed
        only 10. Six events were implemented, wired into pyproject as entry
        points, and reachable through the installer — but invisible to anyone
        who loaded the directory as a Claude Code *plugin*.

        Asserting equality against HOOK_EVENTS rather than a hardcoded list is
        the point: the previous version of this test pinned the 10 and so
        actively held the drift in place.
        """
        from tracing.claude_code.constants import HOOK_EVENTS

        assert set(hooks_data["hooks"]) == set(HOOK_EVENTS)

    def test_each_event_dispatches_its_own_entry_point(self, hooks_data):
        """A copy-paste that points two events at one binary would otherwise
        pass every other check in this class."""
        from tracing.claude_code.constants import HOOK_EVENTS

        for event, entry_point in HOOK_EVENTS.items():
            commands = [h["command"] for group in hooks_data["hooks"][event] for h in group["hooks"]]
            assert commands == [
                f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/run-hook" {entry_point}'
            ], f"{event} does not dispatch {entry_point}"

    def test_hook_commands_use_run_hook(self, hooks_data):
        """Each hook command must use the run-hook dispatcher."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "run-hook" in cmd, f"{event}: command should use run-hook dispatcher: {cmd}"
                    assert "CLAUDE_PLUGIN_ROOT" in cmd, f"{event}: command should reference CLAUDE_PLUGIN_ROOT: {cmd}"

    def test_hook_commands_reference_entry_points(self, hooks_data):
        """Each hook command must pass an atatus-hook-* entry point name."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "atatus-hook-" in cmd, f"{event}: command should reference atatus-hook- entry point: {cmd}"

    def test_hook_entry_points_exist_in_pyproject(self, hooks_data):
        """Every entry point referenced in hooks must exist in pyproject.toml."""
        scripts = _parse_pyproject_scripts()
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    # Extract the entry point name (last argument)
                    entry_point = cmd.strip().split()[-1].strip('"')
                    assert entry_point in scripts, f"{event}: entry point '{entry_point}' not found in pyproject.toml"

    def test_event_to_entry_point_mapping(self, hooks_data):
        """Verify specific event-to-entry-point mappings."""
        mapping = {}
        for event, hook_list in hooks_data["hooks"].items():
            cmd = hook_list[0]["hooks"][0]["command"]
            entry_point = cmd.strip().split()[-1].strip('"')
            mapping[event] = entry_point

        assert mapping["SessionStart"] == "atatus-hook-session-start"
        assert mapping["UserPromptSubmit"] == "atatus-hook-user-prompt-submit"
        assert mapping["PreToolUse"] == "atatus-hook-pre-tool-use"
        assert mapping["PostToolUse"] == "atatus-hook-post-tool-use"
        assert mapping["Stop"] == "atatus-hook-stop"
        assert mapping["SubagentStop"] == "atatus-hook-subagent-stop"
        assert mapping["StopFailure"] == "atatus-hook-stop-failure"
        assert mapping["Notification"] == "atatus-hook-notification"
        assert mapping["PermissionRequest"] == "atatus-hook-permission-request"
        assert mapping["SessionEnd"] == "atatus-hook-session-end"

    def test_hook_type_is_command(self, hooks_data):
        """All hooks must have type 'command'."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    assert hook["type"] == "command", f"{event}: hook type should be 'command', got '{hook['type']}'"

    def test_no_hardcoded_paths(self, hooks_data):
        """Hook commands must not use hardcoded venv or home directory paths."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "~/.atatus" not in cmd, f"{event}: hardcoded ~/.atatus path: {cmd}"
                    assert "/home/" not in cmd, f"{event}: hardcoded /home/ path: {cmd}"


class TestRunHookScript:
    """Tests for tracing.claude_code/scripts/run-hook."""

    def test_run_hook_exists(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        assert path.is_file(), "scripts/run-hook must exist"

    def test_run_hook_is_executable(self):
        import os

        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        assert os.access(path, os.X_OK), "scripts/run-hook must be executable"

    def test_run_hook_has_sh_shebang(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        first_line = path.read_text().splitlines()[0]
        assert first_line.startswith("#!/bin/sh"), f"Expected sh shebang, got: {first_line}"

    def test_run_hook_references_plugin_vars(self):
        """run-hook must use CLAUDE_PLUGIN_ROOT and CLAUDE_PLUGIN_DATA."""
        text = (REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook").read_text()
        assert "CLAUDE_PLUGIN_ROOT" in text
        assert "CLAUDE_PLUGIN_DATA" in text

    @pytest.mark.parametrize(
        "env,label",
        [
            ({"CLAUDE_PLUGIN_ROOT": "/tmp/plugin"}, "root set, data unset"),
            ({}, "neither set"),
            ({"CLAUDE_PLUGIN_DATA": "/tmp/data"}, "data set, root unset"),
        ],
    )
    def test_run_hook_exits_zero_when_not_run_by_claude_code(self, env, label):
        """Other agents read the same .claude/settings.json and honour its
        enabledPlugins. Cursor sets CLAUDE_PLUGIN_ROOT but has no per-plugin
        data dir, so CLAUDE_PLUGIN_DATA is empty there. A non-zero exit is read
        by those hosts as the hook refusing the request, which stops the user
        sending a prompt at all — so this must leave quietly instead."""
        import os
        import subprocess

        script = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        clean = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")}
        result = subprocess.run(
            ["sh", str(script), "atatus-hook-stop"],
            env={**clean, **env},
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{label}: exited {result.returncode}: {result.stderr.decode()[:200]}"

    def test_run_hook_exits_zero_without_an_entry_point_argument(self):
        import os
        import subprocess

        script = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        clean = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")}
        result = subprocess.run(["sh", str(script)], env=clean, capture_output=True, timeout=30)
        assert result.returncode == 0


# --- No bash/jq/curl references in docs ---


class TestNoBashReferences:
    """Verify no stale bash/jq references in harness JSON and Markdown files."""

    def test_no_bash_in_json_files(self):
        """No 'bash ' in .json files under harness directories."""
        for f in _collect_json_files():
            content = f.read_text()
            assert "bash " not in content, f"{f.relative_to(REPO_ROOT)}: still contains 'bash ' reference"

    def test_no_sh_scripts_in_json_files(self):
        """No '.sh' script references in .json files under harness directories."""
        for f in _collect_json_files():
            content = f.read_text()
            # Check for .sh in command context (not in general text)
            for line in content.splitlines():
                if ".sh" in line and ("command" in line or "hook" in line):
                    assert False, f"{f.relative_to(REPO_ROOT)}: still references .sh script: {line.strip()}"

    def test_no_bash_hook_references_in_md(self):
        """No 'bash .../hooks/' patterns in .md files."""
        pattern = re.compile(r"bash\s+.*?/hooks/")
        for f in _collect_md_files():
            content = f.read_text()
            matches = pattern.findall(content)
            assert not matches, f"{f.relative_to(REPO_ROOT)}: still references bash hooks: {matches}"

    def test_no_jq_in_docs(self):
        """No 'jq ' references in harness .md files."""
        for f in _collect_md_files():
            content = f.read_text()
            assert "jq " not in content, f"{f.relative_to(REPO_ROOT)}: still references jq"

    def test_no_hook_sh_in_md(self):
        """No references to hook .sh scripts (like notify.sh, hook-handler.sh) in .md files."""
        # Match specific hook script filenames
        hook_scripts = [
            "session_start.sh",
            "session_end.sh",
            "stop.sh",
            "subagent_stop.sh",
            "notification.sh",
            "permission_request.sh",
            "pre_tool_use.sh",
            "post_tool_use.sh",
            "user_prompt_submit.sh",
            "notify.sh",
            "hook-handler.sh",
            "common.sh",
        ]
        for f in _collect_md_files():
            content = f.read_text()
            for script in hook_scripts:
                assert script not in content, f"{f.relative_to(REPO_ROOT)}: still references {script}"

    def test_no_source_collector_ctl_sh(self):
        """No 'collector_ctl.sh' references in markdown or shell files."""
        pattern = re.compile(r"collector_ctl\.sh")
        for f in _collect_md_files():
            content = f.read_text()
            matches = pattern.findall(content)
            assert not matches, f"{f.relative_to(REPO_ROOT)}: still references collector_ctl.sh: {matches}"

    def test_user_facing_docs_reference_install_sh(self):
        """User-facing docs should tell users to run install.sh, not install.py directly.

        Per-harness install.py files now exist (the router dispatches to them),
        so we only check that user-facing READMEs point to install.sh as the
        entry point.
        """
        user_facing = [REPO_ROOT / "README.md", *(REPO_ROOT.glob("*-tracing/README.md"))]
        for f in user_facing:
            if not f.exists():
                continue
            content = f.read_text()
            if "install.py" in content:
                # install.py may appear in technical context; just ensure
                # install.sh is ALSO referenced as the user-facing command
                assert "install.sh" in content, (
                    f"{f.relative_to(REPO_ROOT)}: references install.py "
                    "but not install.sh — users should be told to run install.sh"
                )


# --- CLI entry points consistency ---


class TestEntryPointConsistency:
    """Verify all referenced CLI commands exist in pyproject.toml."""

    def test_pyproject_has_all_expected_entry_points(self):
        """pyproject.toml must define all expected entry points."""
        scripts = _parse_pyproject_scripts()
        for name, module in EXPECTED_ENTRY_POINTS.items():
            assert name in scripts, f"Missing entry point: {name}"
            assert scripts[name] == module, f"Entry point {name}: expected {module}, got {scripts[name]}"


# --- Documentation consistency ---


class TestDocumentationConsistency:
    """Verify documentation is internally consistent."""

    def test_cursor_skill_references_cli_entry_points(self):
        """Cursor SKILL.md should use CLI entry points for hooks."""
        skill = (REPO_ROOT / "tracing" / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md").read_text()
        assert "atatus-hook-cursor" in skill
        assert "send_span()" in skill
        assert "hook-handler.sh" not in skill

    def test_codex_skill_references_cli_entry_points(self):
        """Codex SKILL.md should use CLI entry points."""
        skill = (REPO_ROOT / "tracing" / "codex" / "skills" / "manage-codex-tracing" / "SKILL.md").read_text()
        assert "atatus-hook-codex-notify" in skill
        assert "notify.sh" not in skill

    def test_claude_skill_references_cli_entry_points(self):
        """Claude SKILL.md should use CLI entry points."""
        skill = (
            REPO_ROOT / "tracing" / "claude_code" / "skills" / "manage-claude-code-tracing" / "SKILL.md"
        ).read_text()
        assert "send_span()" in skill
        assert "collector_ctl.sh" not in skill


# --- Codex config.toml pattern ---


class TestCodexHookReference:
    """Verify Codex docs reference the correct notify hook command."""

    def test_codex_skill_notify_command(self):
        """Codex SKILL.md notify hook should use the CLI entry point."""
        skill = (REPO_ROOT / "tracing" / "codex" / "skills" / "manage-codex-tracing" / "SKILL.md").read_text()
        assert "atatus-hook-codex-notify" in skill


# --- Cursor hooks.json pattern ---


class TestCursorHookReference:
    """Verify Cursor docs reference the correct handler command."""

    def test_cursor_skill_hook_command(self):
        """Cursor SKILL.md should show atatus-hook-cursor for all 12 events."""
        skill = (REPO_ROOT / "tracing" / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md").read_text()
        # All events should reference the same entry point
        events = [
            "beforeSubmitPrompt",
            "afterAgentResponse",
            "afterAgentThought",
            "beforeShellExecution",
            "afterShellExecution",
            "beforeMCPExecution",
            "afterMCPExecution",
            "beforeReadFile",
            "afterFileEdit",
            "stop",
            "beforeTabFileRead",
            "afterTabFileEdit",
        ]
        for event in events:
            assert event in skill, f"Cursor SKILL.md missing event: {event}"
        # Count occurrences of atatus-hook-cursor — should be at least 12 (one per event)
        count = skill.count("atatus-hook-cursor")
        assert count >= 12, f"Expected at least 12 atatus-hook-cursor references, got {count}"


# --- State file extension ---


class TestStateFileExtension:
    """Verify docs reference .json state files, not .yaml."""

    def test_state_files_use_json_extension(self):
        """All state file references in docs should use .json, not .yaml."""
        for f in _collect_md_files():
            content = f.read_text()
            if "state_*.yaml" in content:
                assert False, f"{f.relative_to(REPO_ROOT)}: still references state_*.yaml (should be state_*.json)"


class TestRunHookStalenessCheck:
    """The plugin venv installs a *copy* of the package, so it must be rebuilt whenever
    the source changes.

    It used to key that decision on the hash of `pyproject.toml` alone. Packaging
    metadata rarely changes, so a code-only update — new modules, a rewritten handler —
    left the venv serving the previously installed wheel indefinitely, and the plugin
    kept emitting the old span shape while a source install emitted the new one.
    """

    RUN_HOOKS = [
        Path(__file__).resolve().parents[2] / "tracing" / harness / "scripts" / "run-hook"
        for harness in ("claude_code", "cursor")
    ]

    @pytest.mark.parametrize("script", RUN_HOOKS, ids=lambda p: p.parents[1].name)
    def test_staleness_is_not_decided_by_pyproject_alone(self, script):
        source = script.read_text(encoding="utf-8")
        assert "source_fingerprint" in source, f"{script} must fingerprint the source tree"
        # The old check read pyproject.toml and nothing else.
        assert 'open(sys.argv[1],' not in source, (
            f"{script} still hashes a single file to decide whether to reinstall"
        )

    @pytest.mark.parametrize("script", RUN_HOOKS, ids=lambda p: p.parents[1].name)
    def test_fingerprint_covers_python_sources(self, script):
        source = script.read_text(encoding="utf-8")
        assert ".py" in source and "os.walk" in source, (
            f"{script} must walk the tree and include .py files"
        )
        assert "__pycache__" in source, f"{script} must exclude __pycache__ from the fingerprint"

    @pytest.mark.parametrize("script", RUN_HOOKS, ids=lambda p: p.parents[1].name)
    def test_marker_is_written_with_the_same_fingerprint(self, script):
        source = script.read_text(encoding="utf-8")
        assert 'source_fingerprint > "$MARKER"' in source, (
            f"{script} must persist the same fingerprint it compares against, or every "
            f"hook invocation reinstalls the venv"
        )
