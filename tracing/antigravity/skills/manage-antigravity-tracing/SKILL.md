---
name: manage-antigravity-tracing
description: Set up and configure Atatus tracing for Google Antigravity CLI/IDE sessions. Use when users want to set up tracing, configure Atatus for Antigravity, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up antigravity tracing", "configure Atatus for Antigravity", "enable antigravity tracing", "setup-antigravity-tracing", or any request about connecting the Antigravity CLI/IDE to Atatus for observability.
---

# Setup Antigravity Tracing

Configure OpenInference tracing for the **Antigravity CLI/IDE** (Google's Gemini-lineage coding agent) to Atatus. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

This harness is **transcript-driven**: Antigravity hooks are a control plane that carries only pointers (`transcriptPath`, `conversationId`, `workspacePaths`). The real model and tool content lives in the transcript file the agent writes. The Stop hook parses `transcript_full.jsonl` and reconstructs spans -- one trace per user turn.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.gemini/config/hooks.json` for a top-level `"atatus-tracing"` key containing `PreInvocation` and `Stop` entries
   - Check `~/.atatus/harness/config.json` for the `harnesses.antigravity` block
   - If both are present -> Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

2. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 3

3. **Do they need to configure credentials?**
   - Yes -> Go to [Set Up Atatus](#set-up-atatus)

4. **Are they troubleshooting?**
   - Yes -> Jump to [Troubleshoot](#troubleshoot)

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

That value is `api_key` in the shared config, and it is required.

**No Python dependencies are needed** — spans are sent as OTLP/JSON over HTTP.

Then proceed to [Configure Settings](#configure-settings).

## Configure Settings

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.atatus/harness/config.json` to exist for backend credential resolution.

### Ask the user for:

0. **Whether to reuse an existing config**: if `harnesses.antigravity` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config): the Atatus license key
2. **OTLP Endpoint** (optional): only if you were given a collector other than the default `https://otel-rx.atatus.com`
3. **Project name**: defaults to `"antigravity"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.antigravity.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
4. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/antigravity}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.antigravity` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "antigravity": {
      "project_name": "antigravity",
      "target": "atatus",
      "endpoint": "<endpoint>",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.antigravity.endpoint`.

### Activate Antigravity hooks

Antigravity uses `~/.gemini/config/hooks.json` for hook registration (note: this is **not** `settings.json` like Gemini CLI). The schema is inverted vs. Gemini -- the top level maps **hook name -> { event -> handlers }**, and each event's value is a flat list of handler objects (no `matcher` wrapper). `timeout` is in **seconds**.

Install or reinstall via the installer:

```bash
./install.sh antigravity
```

To uninstall:

```bash
./install.sh uninstall antigravity
```

The installer registers a top-level `"atatus-tracing"` block in `~/.gemini/config/hooks.json` with handlers for the `PreInvocation` and `Stop` events, each pointing at the absolute venv binary path (e.g., `~/.atatus/harness/venv/bin/atatus-hook-antigravity-stop`).

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.antigravity`.
2. **Atatus**: Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `~/.gemini/config/hooks.json` contains a top-level `"atatus-tracing"` entry with `PreInvocation` and `Stop` handler lists.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"conversationId":"test","workspacePaths":["/tmp"],"transcriptPath":"/tmp/transcript.jsonl"}' \
     | ATATUS_DRY_RUN=true atatus-hook-antigravity-stop
   ```

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- Antigravity hooks activated via `~/.gemini/config/hooks.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Antigravity session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching Antigravity)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/antigravity.log`; set `ATATUS_VERBOSE=true` in the shell before launching Antigravity to also capture routine hook activity
- Toggle tracing on/off via `ATATUS_TRACE_ENABLED` env var (must be exported in the user's shell -- Antigravity hooks read host env vars)
- Tail the log file at `~/.atatus/harness/logs/antigravity.log` for real-time debugging

## Hook Events

Antigravity fires two hook events that drive tracing. The harness is **transcript-driven**: hook payloads carry only pointers (`conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`), and the actual model and tool content is reconstructed from the transcript file (`transcript_full.jsonl`). The Stop handler parses the transcript and emits one trace per user turn.

| Event | Trigger | Behavior |
|-------|---------|----------|
| `PreInvocation` | Before each model call | Backstop -- flushes any *earlier* turn whose `Stop` was missed (crash/kill). Idempotent. |
| `Stop` | Agent loop ends | Parses the transcript, reconstructs spans for the just-finished turn, and sends them. |

Per user turn, the Stop handler emits:
- one **Turn** span (`CHAIN` kind, fresh `trace_id` -- one trace per turn),
- one **LLM** span per `PLANNER_RESPONSE` model response,
- one **TOOL** span per tool call.

Turn boundaries come from the transcript's `USER_INPUT` records (the ground-truth user-message boundary), not from the hook events themselves.

**Stop output contract:** the Stop hook prints exactly `{}` to stdout. It never emits `{"decision": "continue"}` (which would force the agent loop to re-enter and produce an infinite loop).

### Token counts are intentionally omitted

Antigravity withholds per-turn token usage from every local surface (transcript, SQLite store, CLI logs, hook payloads). This was verified empirically. The harness deliberately leaves `llm.token_count.*` attributes unset -- absent reads correctly in Atatus, while zeros would look like a bug. Tokens are not recovered by intercepting network traffic.

## Troubleshoot

Common issues and fixes for Antigravity:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check hook log: `tail -20 ~/.atatus/harness/logs/antigravity.log` |
| Hooks not firing | Verify `~/.gemini/config/hooks.json` contains a top-level `"atatus-tracing"` block with `PreInvocation` and `Stop` entries pointing at absolute venv binary paths |
| Agent loops or won't exit | Check that the Stop handler is printing exactly `{}` on stdout -- a stray `{"decision": "continue"}` will force re-entry. Inspect `~/.atatus/harness/logs/antigravity.log`. |
| Config missing | Run `./install.sh antigravity` or create `~/.atatus/harness/config.json` manually (include `harnesses.antigravity` section) |
| Endpoint unreachable | Verify connectivity: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching Antigravity |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching Antigravity |
| Wrong project name | Set `harnesses.antigravity.project_name` in `~/.atatus/harness/config.json` (default: `"antigravity"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching Antigravity |
| Tracing not toggling | Ensure `ATATUS_TRACE_ENABLED` is exported in your shell, not just set |
| Token counts missing | Expected -- Antigravity does not expose per-turn token usage on any local surface, so the harness intentionally leaves token attributes unset |
| Transcript not found | The Stop handler reads `transcriptPath` from hook stdin. Confirm the path resolves and the file is non-empty: `tail -5 "$(jq -r .transcriptPath < /tmp/stop-payload.json)"` |
