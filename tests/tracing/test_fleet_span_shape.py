#!/usr/bin/env python3
"""Cross-harness invariants for coding-agent span shape.

These are source-level guards. Each one encodes a defect that was live in several
harnesses at once, so a per-harness test would not have caught the next occurrence.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACING = REPO_ROOT / "tracing"

HARNESSES = sorted(p.name for p in TRACING.iterdir() if p.is_dir() and (p / "hooks").is_dir())


def _handler_sources():
    for name in HARNESSES:
        for path in sorted((TRACING / name / "hooks").glob("*.py")):
            yield name, path, path.read_text(encoding="utf-8")


def test_harnesses_are_discovered():
    assert len(HARNESSES) >= 8, HARNESSES


class TestModelCallNamesAreDistinct:
    """The dashboard span bucketer (`useSpanBuckets`, minBucketSize 3) collapses three or
    more *adjacent same-name siblings* and re-parents their children to depth 0.

    A harness that names every model call `LLM: {model}` produces identical siblings, so
    a turn's nesting silently flattens in the waterfall — the exact thing the model-call
    layer exists to show. Every model-call span name must carry a per-turn ordinal.
    """

    # `f"LLM: {model}"` and friends: a name built only from the model is not unique.
    BARE_MODEL_NAME = re.compile(r'f"LLM: \{[a-z_]+\}"')

    @pytest.mark.parametrize("harness", HARNESSES)
    def test_no_model_call_span_is_named_by_model_alone(self, harness):
        offenders = []
        for name, path, source in _handler_sources():
            if name != harness:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if self.BARE_MODEL_NAME.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "model-call span names must include a per-turn ordinal, or the UI span "
            "bucketer collapses sibling calls and flattens the trace:\n" + "\n".join(offenders)
        )


class TestTokensAreNotDuplicatedOntoTheRoot:
    """Summary queries sum `llm.token_count.*` across every span in a range.

    A root that repeats the sum of its children therefore reports exactly twice the real
    usage — and twice the cost. Tokens belong to the span that spent them.
    """

    # e.g. root_attrs.update(_token_attrs(sum(...)))
    ROOT_TOKENS = re.compile(r"root_attrs(?:\[|\.update\()[^\n]*token", re.IGNORECASE)

    @pytest.mark.parametrize("harness", HARNESSES)
    def test_no_root_attrs_dict_carries_token_counts(self, harness):
        offenders = []
        for name, path, source in _handler_sources():
            if name != harness:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if self.ROOT_TOKENS.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "token counts must not be attached to a trace root — range sums would "
            "double-count the turn:\n" + "\n".join(offenders)
        )


class TestSkillAttributesFollowToolDetails:
    """Skill attributes are tool details, left out (not redacted) when tool details are off: a
    placeholder would read as a skill name. The gate lives in the core helpers, so a hook that
    writes a skill key itself would send it whatever the setting says."""

    SKILL_KEY = re.compile(r"""["'](?:tool\.skill_name|skill\.(?:trigger|name|active))["']""")
    HELPER_CALLS = {
        "skill_name_attributes": lambda c: c.skill_name_attributes("release-notes", trigger=c.SKILL_TRIGGER_MODEL),
        "skill_read_attributes": lambda c: c.skill_read_attributes(".agents/skills/release-notes/SKILL.md"),
        "skill_command_attributes": lambda c: c.skill_command_attributes("cat .agents/skills/release-notes/SKILL.md"),
        "invoked_skill_attributes": lambda c: c.invoked_skill_attributes(["release-notes"]),
        "active_skill_attributes": lambda c: c.active_skill_attributes("release-notes"),
    }

    @pytest.mark.parametrize("harness", HARNESSES)
    def test_no_hook_writes_a_skill_attribute_itself(self, harness):
        offenders = []
        for name, path, source in _handler_sources():
            if name != harness:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if self.SKILL_KEY.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "skill attributes must come from the core skill helpers, which leave them out "
            "without tool details:\n" + "\n".join(offenders)
        )

    def test_every_core_helper_that_emits_a_skill_key_is_checked_below(self):
        source = (REPO_ROOT / "core" / "common.py").read_text(encoding="utf-8")
        emitting = {
            match.group(1)
            for match in re.finditer(r"^def (\w+)\(.*?(?=^def |\Z)", source, re.MULTILINE | re.DOTALL)
            if self.SKILL_KEY.search(match.group(0))
        }
        assert emitting <= set(self.HELPER_CALLS), sorted(emitting - set(self.HELPER_CALLS))

    @pytest.mark.parametrize("helper", sorted(HELPER_CALLS))
    def test_every_skill_helper_gives_nothing_without_tool_details(self, monkeypatch, helper):
        import core.common as common

        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "true")
        assert self.HELPER_CALLS[helper](common), "the call below must name a skill when tool details are on"
        monkeypatch.setenv("ATATUS_LOG_TOOL_DETAILS", "false")
        assert self.HELPER_CALLS[helper](common) == {}


class TestToolArgumentsFollowToolContent:
    """Tool arguments carry file bodies, patches and generated code, so `input.value` on a
    tool or permission span must follow tool content (off by default), never tool details.
    Small fields that say what ran (command, path, URL, query) stay under tool details."""

    ARGS_UNDER_DETAILS = re.compile(
        r'"input\.value":\s*redact_content\(env\.log_tool_details'
        r"|redact_content\(env\.log_tool_details,\s*(?:json\.dumps\(|args_|tool_input\b|input_)"
    )

    @pytest.mark.parametrize("harness", HARNESSES)
    def test_no_tool_arguments_are_gated_by_tool_details(self, harness):
        offenders = []
        for name, path, source in _handler_sources():
            if name != harness:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if self.ARGS_UNDER_DETAILS.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "tool arguments must be redacted with env.log_tool_content; promote the command, "
            "path or URL with tool_detail_attributes instead:\n" + "\n".join(offenders)
        )

    # A slice of the serialized arguments is still argument content: MCP payloads, skill args, file bodies.
    ARGUMENT_PREFIX = re.compile(
        r"(?:\b(?:args_\w*|tool_input\w*|tool_args\w*|input_\w*|arguments)\)?|json\.dumps\([^)\n]*\))\s*\[\s*:"
        r"|redact_content\(env\.log_tool_details,[^\n]*\b(?:args_\w+|tool_input|tool_args)\b"
    )

    @pytest.mark.parametrize("harness", HARNESSES)
    def test_no_prefix_of_the_arguments_is_sent_as_a_tool_detail(self, harness):
        offenders = []
        for name, path, source in _handler_sources():
            if name != harness:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if self.ARGUMENT_PREFIX.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "a slice of the raw tool arguments is argument content, not a tool detail; build "
            "tool.description from a known short field or leave it out:\n" + "\n".join(offenders)
        )
