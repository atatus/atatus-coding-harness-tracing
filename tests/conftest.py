"""Shared pytest fixtures for coding-harness-tracing tests."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from core.common import DEFAULT_OTLP_ENDPOINT

# Ensure repo root is importable
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Isolate every test from the developer's real ~/.atatus/harness/config.json.

    Three independent leaks are closed here:

    1. ``core.config.load_config`` binds its path via ``from core.constants
       import CONFIG_FILE``, so patching ``core.constants.CONFIG_FILE`` (as
       ``tmp_harness_dir`` does) does NOT redirect it — the name ``load_config``
       actually reads is ``core.config.CONFIG_FILE``. Point that at a
       nonexistent path so config defaults to ``{}`` unless a test opts in
       (tests that need real config, like ``test_config.py``, re-patch it).
    2. ``core.common.env`` is a module-level singleton whose ``cached_property``
       config reads survive across tests. Clear them before and after each test
       so the next access re-reads the isolated config rather than a stale value
       cached from a sibling test.

    3. The capture flags resolve **env var first**, then config, then default
       (``_Env._resolve_log_flag``). Isolating the config file therefore does not
       isolate them: any shell exporting ``ATATUS_LOG_*`` decides what the suite
       asserts against. That is not hypothetical -- a harness that injects those
       vars into the processes it spawns (Claude Code's ``settings.json`` ``env``
       block does) turns ~69 tests red on a clean checkout, which reads as a
       regression and is not one. Delete them so every test sees the documented
       defaults unless it sets them itself.
    """
    from core.common import env

    for var in ("ATATUS_LOG_PROMPTS", "ATATUS_LOG_TOOL_DETAILS", "ATATUS_LOG_TOOL_CONTENT"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr("core.config.CONFIG_FILE", tmp_path / "no-such-config.json")
    env.invalidate_caches()
    yield
    env.invalidate_caches()


# Every variable the non-interactive resolver consults. Cleared wholesale rather
# than per-test: an installed harness exports ATATUS_API_KEY and
# ATATUS_PROJECT_NAME into every session it spawns, so a developer running the
# suite from inside a traced terminal would otherwise be testing their own
# credentials.
_RESOLVED_ENV_KEYS = (
    "ATATUS_NONINTERACTIVE",
    "ATATUS_ENV_FILE",
    "ATATUS_API_KEY",
    "ATATUS_OTLP_ENDPOINT",
    "ATATUS_PROJECT_NAME",
    "ATATUS_USER_ID",
    "ATATUS_KIRO_AGENT",
    "ATATUS_KIRO_SET_DEFAULT",
    "ATATUS_WHEEL_DIR",
)


@pytest.fixture(autouse=True)
def isolate_setup_resolver(monkeypatch):
    """Clear the non-interactive resolver's inputs and its dotenv cache.

    The cache is a module global that lives for the whole pytest process, so
    without this a test that reads a dotenv file hands its values to every test
    that runs after it.
    """
    import core.setup as _setup

    for var in _RESOLVED_ENV_KEYS:
        monkeypatch.delenv(var, raising=False)

    _setup._reset_dotenv_cache()
    yield
    _setup._reset_dotenv_cache()


@pytest.fixture(autouse=True)
def isolated_egress_breaker(tmp_path, monkeypatch):
    """Keep the egress breaker's state file out of the real ~/.atatus tree.

    A test that exercises a failing send would otherwise trip the breaker on the
    developer's own machine and silence their tracing for the cooldown.
    """
    import core.common as _common

    monkeypatch.setattr(_common, "_breaker_file", lambda: tmp_path / "egress-breaker.json")


@pytest.fixture
def tmp_harness_dir(tmp_path, monkeypatch):
    """Create the full ~/.atatus/harness directory tree in a temp location.

    Monkeypatches core.constants so all code sees the temp paths.
    Returns the base directory Path.
    """
    base = tmp_path / ".atatus" / "harness"
    for subdir in ["bin", "run", "logs", "state/claude-code", "state/codex", "state/cursor"]:
        (base / subdir).mkdir(parents=True)

    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", base)
    monkeypatch.setattr(c, "CONFIG_FILE", base / "config.json")
    monkeypatch.setattr(c, "PID_DIR", base / "run")
    monkeypatch.setattr(c, "LOG_DIR", base / "logs")
    monkeypatch.setattr(c, "BIN_DIR", base / "bin")
    monkeypatch.setattr(c, "VENV_DIR", base / "venv")
    monkeypatch.setattr(c, "STATE_BASE_DIR", base / "state")
    return base


@pytest.fixture
def sample_config(tmp_harness_dir):
    """Write a known-good config.json into the temp harness dir.

    Returns the config dict.
    """
    config = {
        "harnesses": {
            "claude-code": {
                "project_name": "claude-code",
                "target": "atatus",
                "endpoint": DEFAULT_OTLP_ENDPOINT,
                "api_key": ""
            },
            "codex": {
                "project_name": "codex",
                "target": "atatus",
                "endpoint": DEFAULT_OTLP_ENDPOINT,
                "api_key": "",
                "collector": {"host": "127.0.0.1", "port": 4318}
            },
            "cursor": {
                "project_name": "cursor",
                "target": "atatus",
                "endpoint": DEFAULT_OTLP_ENDPOINT,
                "api_key": ""
            }
        }
    }
    config_path = tmp_harness_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return config


class _CollectorHandler(BaseHTTPRequestHandler):
    """Minimal mock HTTP handler that records POSTed spans."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server._received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silence request logging in test output


@pytest.fixture
def mock_collector():
    """Start a real HTTP server on a random port.

    Accepts POST /v1/spans (records body) and GET /health (returns 200).
    Yields dict: {"url": "http://127.0.0.1:{port}", "received": [...], "port": int}
    Server is torn down after the test.
    """
    server = HTTPServer(("127.0.0.1", 0), _CollectorHandler)
    server._received = []
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{port}", "received": server._received, "port": port}
    server.shutdown()


@pytest.fixture
def capture_log(tmp_path):
    """Provide a temp log file and a reader function.

    Returns (log_file_path, read_log_fn). read_log_fn() returns list of lines.
    """
    log_file = tmp_path / "test.log"

    def read_log():
        return log_file.read_text().splitlines() if log_file.exists() else []

    return log_file, read_log


# ---------------------------------------------------------------------------
# Inline fixture data (replaces tests/fixtures/*.json)
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_session_start_input():
    """Claude Code session_start hook input."""
    return {"session_id": "sess-abc123", "cwd": "/home/user/project"}


@pytest.fixture
def claude_stop_input():
    """Claude Code stop hook input."""
    return {"session_id": "sess-abc123", "transcript_path": "/tmp/transcript.jsonl"}


@pytest.fixture
def cursor_before_submit_input():
    """Cursor beforeSubmitPrompt hook input."""
    return {
        "hook_event_name": "beforeSubmitPrompt",
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "prompt": "fix the bug"
    }


@pytest.fixture
def cursor_after_shell_input():
    """Cursor afterShellExecution hook input."""
    return {
        "hook_event_name": "afterShellExecution",
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "command": "ls -la",
        "output": "total 0",
        "exit_code": "0"
    }


@pytest.fixture
def golden_span():
    """Expected OTLP span structure for golden/snapshot tests."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test-service"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "test-scope"},
                        "spans": [
                            {
                                "traceId": "0123456789abcdef0123456789abcdef",
                                "spanId": "abcdef1234567890",
                                "name": "Turn 1",
                                "kind": 1,
                                "startTimeUnixNano": "1711987200000000000",
                                "endTimeUnixNano": "1711987201000000000",
                                "attributes": [
                                    {"key": "session.id", "value": {"stringValue": "sess-1"}},
                                    {"key": "input.value", "value": {"stringValue": "hello"}}
                                ],
                                "status": {"code": 1}
                            }
                        ]
                    }
                ]
            }
        ]
    }


SAMPLE_TRANSCRIPT_LINES = [
    '{"type": "user", "message": {"role": "user", "content": "fix the bug"}}',
    '{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I found the issue."}], "model": "claude-sonnet-4-20250514", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}}',
    '{"type": "tool_use", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {"file": "main.py"}}]}}'
]


@pytest.fixture
def transcript_file(tmp_path):
    """Write the sample transcript to a temp file and return its path."""
    tf = tmp_path / "transcript.jsonl"
    tf.write_text("\n".join(SAMPLE_TRANSCRIPT_LINES) + "\n")
    return str(tf)
