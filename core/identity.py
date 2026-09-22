#!/usr/bin/env python3
"""Per-harness login-identity detection.

Each `detect_*` function reads whatever local, already-authenticated state a
harness leaves on disk (or, for the two that have no static file, a short CLI
call) and returns an email or, where that's genuinely all a harness has,
a login username. Every function returns "" rather than raising on any
failure — a missing file, a malformed one, a binary not on PATH, or a
subprocess timeout are all just "not detectable right now", never an error
worth surfacing to the user or interrupting tracing for.

The two functions that shell out (`detect_claude_code_login_id`,
`detect_kiro_login_id`) bound the subprocess with a short timeout so a hung
CLI can never block a session for long. Callers are expected to run these
once per session and cache the result (including a "" result) rather than
re-invoking them per hook call — see core/common.py's get_user_login_id and
each adapter's ensure_session_initialized.
"""

import base64
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

_SUBPROCESS_TIMEOUT = 1.5


def _read_json(path: Path) -> dict:
    with open(path, "r") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload claims without verifying the signature.

    We already own this token (it's sitting in our own home directory) —
    decoding it is just reading data we can already see, not authenticating.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    data = json.loads(decoded)
    return data if isinstance(data, dict) else {}


def detect_claude_code_login_id() -> str:
    """Email of the logged-in Claude Code account.

    File first (sub-millisecond, no subprocess); `claude auth status` only as
    a fallback when the file is missing/unreadable/lacks the field. Both
    respect CLAUDE_CONFIG_DIR from the environment however it was set (a
    plain env var, a wrapper like claude-switch, or a shell alias/function
    that exported it before exec-ing the real `claude` binary) — we never
    need to know which.
    """
    try:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        json_path = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
        data = _read_json(json_path)
        email = data.get("oauthAccount", {}).get("emailAddress", "")
        if email:
            return str(email)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        match = re.search(r'"email"\s*:\s*"([^"]*)"', result.stdout or "")
        if match:
            return match.group(1)
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def detect_codex_login_id() -> str:
    """Email claim inside the ChatGPT-login id_token in ~/.codex/auth.json."""
    try:
        data = _read_json(Path.home() / ".codex" / "auth.json")
        id_token = data.get("tokens", {}).get("id_token", "")
        if not id_token:
            return ""
        claims = _decode_jwt_payload(id_token)
        return str(claims.get("email", "") or "")
    except Exception:
        return ""


def detect_gemini_login_id() -> str:
    """Active Google account email from ~/.gemini/google_accounts.json."""
    try:
        data = _read_json(Path.home() / ".gemini" / "google_accounts.json")
        return str(data.get("active", "") or "")
    except Exception:
        return ""


def detect_copilot_login_id() -> str:
    """GitHub username of the logged-in Copilot account.

    GitHub logins are usernames, never emails — Copilot has no local email
    surface at all, so this attribute holds a username for this harness only.
    """
    try:
        data = _read_json(Path.home() / ".copilot" / "config.json")
        return str(data.get("lastLoggedInUser", {}).get("login", "") or "")
    except Exception:
        return ""


def _parse_kiro_whoami(text: str) -> str:
    match = re.search(r"^Email:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def start_kiro_login_probe(dest_path: Path) -> None:
    """Fire-and-forget `kiro-cli user whoami`, never blocking the caller.

    Measured on a real machine, `kiro-cli user whoami` takes ~1.9s — too slow
    to run inline without stalling a hook response. There's also no static
    file to read instead (confirmed in research). So this launches it fully
    detached (its own session, closed stdio) and has it atomically write its
    output to dest_path once it finishes; read_kiro_login_probe() below picks
    that up from a *later* hook call. The calling process returns immediately
    either way.
    """
    tmp_path = dest_path.with_name(dest_path.name + ".tmp")
    try:
        subprocess.Popen(
            [
                "sh",
                "-c",
                f"kiro-cli user whoami > {shlex.quote(str(tmp_path))} 2>/dev/null "
                f"&& mv {shlex.quote(str(tmp_path))} {shlex.quote(str(dest_path))}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def read_kiro_login_probe(dest_path: Path) -> Optional[str]:
    """Read a probe started by start_kiro_login_probe().

    Returns the detected email, "" if the probe finished but found none
    (not logged in, or `kiro-cli` exited non-zero so the rename never
    happened... in which case this keeps returning None below, not ""),
    or None if the probe hasn't produced dest_path yet — callers should
    treat None as "still pending, check again on the next hook call",
    not as a final answer.
    """
    try:
        text = dest_path.read_text()
    except OSError:
        return None
    return _parse_kiro_whoami(text)


def detect_opencode_login_id() -> str:
    """Email of an OAuth-based provider in ~/.local/share/opencode/auth.json.

    Only OAuth-style providers carry an email; an API-key provider (all this
    was verified against) has none, which is a real product state, not a bug.
    """
    try:
        data = _read_json(Path.home() / ".local" / "share" / "opencode" / "auth.json")
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("email"):
                return str(entry["email"])
    except Exception:
        pass
    return ""


def detect_omp_login_id() -> str:
    """Best-effort mirror of the other file-based detectors for omp.

    Unverified: omp isn't installed anywhere this could be tested against a
    real login. Kept defensive (try/except, no assumptions beyond "some
    settings file under ~/.omp/ might carry an email") until someone with a
    real omp install confirms the actual shape.
    """
    try:
        data = _read_json(Path.home() / ".omp" / "agent" / "settings.json")
        for key in ("email", "user_email", "account_email"):
            if data.get(key):
                return str(data[key])
    except Exception:
        pass
    return ""


def detect_antigravity_login_id() -> str:
    """Active account email from ~/.gemini/google_accounts.json or oauth_creds.json."""
    gemini_dirs = []
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if app_data:
        gemini_dirs.append(Path(app_data).parent)
    gemini_dirs.append(Path.home() / ".gemini")

    for g_dir in gemini_dirs:
        try:
            data = _read_json(g_dir / "google_accounts.json")
            email = data.get("active")
            if email:
                return str(email)
        except Exception:
            pass

        try:
            creds = _read_json(g_dir / "oauth_creds.json")
            id_token = creds.get("id_token", "")
            if id_token:
                claims = _decode_jwt_payload(id_token)
                email = claims.get("email")
                if email:
                    return str(email)
        except Exception:
            pass
    return ""
