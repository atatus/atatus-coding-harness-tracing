---
name: manage-codex-tracing
description: Set up and configure Atatus tracing for OpenAI Codex CLI sessions. Use when users want to set up Codex tracing, configure Atatus for Codex, enable/disable tracing, or troubleshoot Codex tracing issues. Triggers on "set up codex tracing", "configure Atatus for Codex", "enable codex tracing", "setup-codex-tracing", or any request about connecting Codex to Atatus for observability.
---

# Setup Codex Tracing

Configure OpenInference tracing for OpenAI Codex CLI sessions to Atatus.

## Architecture Overview

Codex tracing uses real Codex CLI lifecycle hooks plus the legacy `notify` hook as a token-usage backstop. Spans are sent directly to the backend from the `Stop` hook:

1. **Direct send** (`core/common.py`) — spans are sent directly to Atatus as OTLP/JSON over HTTP from the hook handlers via `send_span()`. Per-harness backend credentials are read from `harnesses.codex.*` in config.

2. **Real Codex hooks** — `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, and `Stop` events are dispatched to three entry points:
   - `atatus-hook-codex-session` (`SessionStart`, `UserPromptSubmit`) — mutates per-thread state file at `~/.atatus/harness/state/codex/state_<thread_id>.json`.
   - `atatus-hook-codex-tool` (`PreToolUse`, `PostToolUse`, `PermissionRequest`) — appends rows to `~/.atatus/harness/state/codex/spans_<thread_id>.jsonl`.
   - `atatus-hook-codex-stop` (`Stop`) — reads state + JSONL, builds the parent LLM span plus TOOL child spans, sends the multi-span payload, then clears turn-scoped state and deletes the JSONL.

3. **Notify hook** (`atatus-hook-codex-notify`) — Fires on `agent-turn-complete`. Real Codex hook payloads don't carry exact token counts, so `notify` is kept purely as a token-usage backstop: it extracts `token_usage` and `last-assistant-message` from the notify payload and writes them into the state file. If the state file doesn't exist (hooks not yet trusted, first run), notify falls back to its legacy behavior of emitting a single flat Turn span.

```
Codex CLI
  |
  |-- SessionStart / UserPromptSubmit --> atatus-hook-codex-session
  |     |--> updates state_<thread_id>.json
  |
  |-- PreToolUse / PostToolUse / PermissionRequest --> atatus-hook-codex-tool
  |     |--> appends row to spans_<thread_id>.jsonl
  |
  |-- agent-turn-complete (notify) --> atatus-hook-codex-notify
  |     |--> writes token_usage into state_<thread_id>.json
  |
  |-- Stop --> atatus-hook-codex-stop
        |--> reads state + spans JSONL
        |--> builds parent LLM span + TOOL child spans
        |--> send_span() --> Atatus
        |--> clears turn-scoped state, deletes spans JSONL
```

**Graceful degradation**: If hooks aren't trusted yet (Codex requires explicit `/hooks` approval), the `notify` hook still produces a single flat Turn span.

## Trust prompt

Codex requires explicit user trust for non-managed hooks before they fire. After install, the user must:

1. Start a Codex session: `codex`
2. Type `/hooks` and approve each `atatus-hook-codex-*` entry.

Without this one-time approval, the real hooks never fire and tracing falls back to `notify`-only (single LLM span per turn, no tool spans).

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Do they already have credentials?**
   - Yes → Jump to [Configure Codex](#configure-codex)
   - No → Continue to step 2

2. **Do they need to configure credentials?**
   - Yes -> Go to [Set Up Atatus](#set-up-atatus)

3. **Are they troubleshooting?**
   - Yes → Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

## Set Up Atatus

The user needs an Atatus account and a license key.

### 1. Create an account

If the user doesn't have an Atatus account, sign up at https://app.atatus.com/auth/join

### 2. Get your license key

Walk the user through creating one:

1. Log in to Atatus
2. Click **Settings** in the top bar
3. Go to **Account Settings**
4. Click **API Keys**
5. Click **New API Key**
6. Give it a name and choose the type **Ingest License Key**
7. Click **Create**
8. Copy the key

That value is `ATATUS_API_KEY`, and it is required.

### 3. Python dependencies

None. Spans are sent as OTLP/JSON over HTTP using the standard library — no `pip install`
step is needed for tracing.

Then proceed to [Configure Codex](#configure-codex).

## Configure Codex

This section configures:
1. **Backend config** at `~/.atatus/harness/config.json`
2. **Environment variables** in `~/.codex/atatus-env.sh`
3. **Hook entries** in `~/.codex/config.toml` (real Codex hooks + `notify` token-usage backstop)
4. **Trust prompt** inside Codex (`/hooks`) — required before non-managed hooks fire

### Determine the integration path

Ask the user: **"Where is the Codex tracing directory located?"**

Common locations:
- If cloned: `./atatus-coding-harness-tracing/tracing/codex`
- If installed via the curl installer: `~/.atatus/harness/tracing/codex`

Store this as `INTEGRATION_PATH` for the hook config.

### Step 1: Write the backend config

Write `~/.atatus/harness/config.json` with the backend credentials. The config file is the single source of truth for backend and harness settings.

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, add or update the `harnesses.codex` entry, and preserve existing backend credentials. Only prompt the user for backend credentials if there is no existing config.

**Atatus:**
```bash
mkdir -p ~/.atatus/harness/{logs,state/codex}
# Merge: add/update harnesses.codex, preserve existing backend settings
atatus-config set harnesses.codex.project_name codex
```

If no config exists yet, create it:
```json
{
  "harnesses": {
    "codex": {
      "project_name": "codex",
      "target": "atatus",
      "endpoint": "https://otel-rx.atatus.com",
      "api_key": ""
    }
  }
}
```


### Step 2: Write the environment file (optional)

Environment variables are optional overrides — all backend credentials are in `~/.atatus/harness/config.json`. If the user needs env-var overrides, create `~/.codex/atatus-env.sh`:

```bash
cat > ~/.codex/atatus-env.sh << 'EOF'
export ATATUS_TRACE_ENABLED=true
EOF
chmod 600 ~/.codex/atatus-env.sh
```

If the user wants to associate spans with a user ID, add `export ATATUS_USER_ID="<user-id>"`.

### Step 3: Add the hook entries to config.toml

Read `~/.codex/config.toml`. Add the `notify` line at the top level (NOT inside any `[section]`) — this is the token-usage backstop:

```toml
notify = ["~/.atatus/harness/venv/bin/atatus-hook-codex-notify"]
```

Then add one `[[hooks.<Event>]]` entry per real Codex lifecycle event:

```toml
[[hooks.SessionStart]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-session"]

[[hooks.UserPromptSubmit]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-session"]

[[hooks.PreToolUse]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-tool"]

[[hooks.PostToolUse]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-tool"]

[[hooks.PermissionRequest]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-tool"]

[[hooks.Stop]]
command = ["~/.atatus/harness/venv/bin/atatus-hook-codex-stop"]
```

**Important:** If `notify` already exists in the config, update the existing line. If `[[hooks.<Event>]]` entries already exist that match our handlers (managed block), leave them alone — the installer manages the block idempotently.

### Step 4: Approve the hooks inside Codex

Codex requires explicit user trust for non-managed hooks before they fire. The user must:

1. Start a Codex session: `codex`
2. Type `/hooks` and approve each `atatus-hook-codex-*` entry.

Until this is done, only the `notify` token-usage backstop will run (flat single-span tracing). The real hooks will not fire.

**Note:** The installer handles Steps 1-3 automatically. Step 4 (the `/hooks` trust prompt) is a one-time user action — the installer cannot automate it.

### Validate

After writing the config, validate:

1. **Check config.toml is valid:**
```bash
cat ~/.codex/config.toml
```
Visually confirm the `notify` line is at the top level and the `[[hooks.SessionStart]]`, `[[hooks.UserPromptSubmit]]`, `[[hooks.PreToolUse]]`, `[[hooks.PostToolUse]]`, `[[hooks.PermissionRequest]]`, and `[[hooks.Stop]]` entries are present.

2. **Check env file:**
```bash
source ~/.codex/atatus-env.sh && echo "ATATUS_TRACE_ENABLED=$ATATUS_TRACE_ENABLED"
```

3. **Check the hooks are trusted inside Codex:**
   Start `codex`, run `/hooks`, and confirm each `atatus-hook-codex-*` entry is listed and approved.

4. **Collector connectivity**:
```bash
curl -sf ${ATATUS_OTLP_ENDPOINT}/v1/traces >/dev/null && echo "collector reachable" || echo "collector not reachable"
```

5. **Dry run test:**
```bash
ATATUS_DRY_RUN=true atatus-hook-codex-notify '{"type":"agent-turn-complete","thread-id":"test-123","turn-id":"turn-1","cwd":"/tmp","input-messages":"hello","last-assistant-message":"hi there"}'
```
Should print: `[atatus] DRY RUN:` followed by the span name.

### Confirm

Tell the user:
- Configuration saved to `~/.codex/config.toml`, `~/.codex/atatus-env.sh`, and `~/.atatus/harness/config.json`
- Spans are sent directly to the backend from the `Stop` hook
- The real Codex hooks (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop`) build rich span trees with TOOL children per tool call
- The `notify` hook is now a token-usage backstop — it writes exact token counts into the per-thread state file so they appear on the parent LLM span
- Until the user approves the hooks via `/hooks` inside Codex, only the `notify`-based fallback runs (single flat LLM span per turn)
- Mention `ATATUS_DRY_RUN=true` to test without sending data
- Mention `ATATUS_VERBOSE=true` and `ATATUS_TRACE_DEBUG=true` for debug output
- Logs: `~/.atatus/harness/logs/codex.log` (errors always; routine activity requires `ATATUS_VERBOSE=true` in `~/.codex/atatus-env.sh` or the shell)

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ATATUS_API_KEY` | Yes | - | Atatus license key |
| `ATATUS_OTLP_ENDPOINT` | No | `https://otel-rx.atatus.com` | Atatus collector URL |
| `ATATUS_PROJECT_NAME` | No | `codex` | Project name in Atatus |
| `ATATUS_USER_ID` | No | - | User ID to attach to all spans as `user.id` attribute |
| `ATATUS_TRACE_ENABLED` | No | `true` | Enable/disable tracing |
| `ATATUS_DRY_RUN` | No | `false` | Print spans instead of sending |
| `ATATUS_VERBOSE` | No | `false` | Enable verbose logging |
| `ATATUS_TRACE_DEBUG` | No | `false` | Write debug JSON to `~/.atatus/harness/state/codex/debug/` |
| `ATATUS_LOG_FILE` | No | `~/.atatus/harness/logs/codex.log` | Log file path |

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Check `ATATUS_TRACE_ENABLED` is `true` in `~/.codex/atatus-env.sh` |
| Hooks not firing | Run `codex` → `/hooks` and confirm each `atatus-hook-codex-*` entry is trusted. If they aren't listed at all, re-run the installer. |
| `notify` hook not firing | Verify `notify` line in `~/.codex/config.toml` points to correct path |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| No output in terminal | Hooks run in background; check `~/.atatus/harness/logs/codex.log` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` in env or `export ATATUS_DRY_RUN=true` |
| Want verbose logging | Set `ATATUS_VERBOSE=true` in env or `export ATATUS_VERBOSE=true` |
| Wrong project name | Set `ATATUS_PROJECT_NAME` in `~/.codex/atatus-env.sh` (default: `codex`) |
| Existing `notify` hook | Codex supports only one `notify` — create a wrapper script that calls both |
| Stale state files | Run: `rm -rf ~/.atatus/harness/state/codex/state_*.json ~/.atatus/harness/state/codex/spans_*.jsonl` |
| Flat spans only (no children) | The real hooks haven't been trusted yet. Run `codex` → `/hooks` and approve each `atatus-hook-codex-*` entry. |
| User ID not appearing on spans | Set `ATATUS_USER_ID` in `~/.codex/atatus-env.sh` or export before running Codex |
