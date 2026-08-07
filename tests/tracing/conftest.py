"""Shared fixtures for the per-harness hook tests.

These tests assert the *shape* of emitted spans -- which attributes appear and
what they contain -- so they need tool output present to assert against.

``ATATUS_LOG_TOOL_CONTENT`` defaults to **False**, which would
otherwise replace every ``output.value`` with ``<redacted (N chars)>``. Opting
in here keeps these tests about span construction, and leaves the default itself
under test where it belongs: ``tests/core/test_common.py`` covers the flag's
precedence, and ``TestPrivacyDefaults`` covers the redaction it produces.

Deliberately scoped to ``tests/tracing/`` rather than the root conftest -- a
suite-wide opt-in would mean nothing ever exercised the real default.
"""

import pytest


@pytest.fixture(autouse=True)
def capture_tool_content(monkeypatch):
    """Enable tool-output capture for harness hook tests (see module docstring)."""
    monkeypatch.setenv("ATATUS_LOG_TOOL_CONTENT", "true")
    from core.common import env

    env.invalidate_caches()
    yield
    env.invalidate_caches()
