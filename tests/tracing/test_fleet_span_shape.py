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
