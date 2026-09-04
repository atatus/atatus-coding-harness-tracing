---
name: manage-opencode-tracing
description: Set up and configure Atatus tracing for opencode terminal coding sessions. Use when users want to set up tracing, configure Atatus for opencode, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up opencode tracing", "configure Atatus for opencode", "enable opencode tracing", "setup-opencode-tracing", or any request about connecting opencode to Atatus for observability.
---

# Setup opencode Tracing

Configure OpenInference tracing for **opencode** terminal coding sessions to Atatus. Unlike the other harnesses in this repo, opencode loads its extensions [in-process inside its Bun runtime](https://opencode.ai/docs/plugins/) — there is no per-event subprocess. The integration ships as a small TypeScript plugin shim that pulls snapshots via the [opencode SDK](https://opencode.ai/docs/sdk/) and spawns a Python reconciler (`atatus-hook-opencode`) which emits spans. Spans are sent directly to the backend from the reconciler — no separate buffer/collector service is required.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.config/opencode/plugin/atatus-tracing.ts` for the Atatus plugin shim
   - Check `~/.atatus/harness/config.json` for the `harnesses.opencode` block
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

0. **Whether to reuse an existing config**: if `harnesses.opencode` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (leave blank unless you are running Atatus on-premise)
2. **Project name**: defaults to `"opencode"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.opencode.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/opencode}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.opencode` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "opencode": {
      "project_name": "opencode",
      "target": "atatus",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

Set the Atatus license key here under `api_key`, or export `ATATUS_API_KEY` in the
environment — it is sent as the `api-key` header. The env var takes precedence over the
config value.

**Atatus:**
```json
{
  "harnesses": {
    "opencode": {
      "project_name": "opencode",
      "target": "atatus",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.opencode.endpoint`.

### Activate the opencode plugin

opencode auto-discovers plugins under `~/.config/opencode/plugin/` ([config docs](https://opencode.ai/docs/config/)) — there is no `opencode.json` edit and no host settings file to register. The installer drops the Atatus plugin shim at `~/.config/opencode/plugin/atatus-tracing.ts`; opencode picks it up on next launch.

Install or reinstall via the installer:

```bash
./install.sh opencode
```

To uninstall:

```bash
./install.sh uninstall opencode
```

Uninstall deletes the plugin file at `~/.config/opencode/plugin/atatus-tracing.ts` (only if it carries the Atatus header marker — the installer never touches the user's own plugins) and removes the `harnesses.opencode` block from `~/.atatus/harness/config.json`.

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.opencode`.
2. **Atatus** (if applicable): Run `curl -sf --connect-timeout 3 --max-time 5 <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Plugin installed**: Verify `~/.config/opencode/plugin/atatus-tracing.ts` exists and starts with the Atatus header marker.
4. **Reconciler entry point**: Verify the reconciler binary exists at `~/.atatus/harness/venv/bin/atatus-hook-opencode` (or `~/.atatus/harness/venv/Scripts/atatus-hook-opencode.exe` on Windows). The shim spawns this binary by absolute path — it does not rely on PATH resolution. `install.sh` installs it as a venv entry point.

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- opencode plugin shim installed at `~/.config/opencode/plugin/atatus-tracing.ts`
- opencode auto-discovers the plugin on next launch — no `opencode.json` edit required
- Spans are sent directly to the backend from the reconciler — no background process needed
- After saving, open a new opencode session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching opencode)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors and reconciler stderr are always written to `~/.atatus/harness/logs/opencode.log` (the adapter redirects Python stderr there via `ATATUS_LOG_FILE`); set `ATATUS_VERBOSE=true` in the shell before launching opencode to also capture routine reconciler activity (snapshot ingest, span emits, dedup hits)
- Toggle tracing on/off via `ATATUS_TRACE_ENABLED` env var (must be exported in the user's shell before launching opencode — the shim and reconciler inherit host env vars)
- Tail the log file at `~/.atatus/harness/logs/opencode.log` for real-time debugging
- Mention `ATATUS_TRACE_DEBUG=true` to dump raw snapshot payloads under `~/.atatus/harness/state/debug/` (files are named `opencode_reconcile_<ts>.json` / `opencode_close_<ts>.json`) for inspection

## Architecture (How spans are produced)

opencode is fundamentally different from every other harness in this repo: extensions are [plugins](https://opencode.ai/docs/plugins/) loaded **in-process** inside opencode's Bun runtime. The Atatus integration is split into two pieces:

1. **TypeScript plugin shim** at `~/.config/opencode/plugin/atatus-tracing.ts`. A dumb bridge. On `message.updated` (assistant completed) and `session.idle` it pulls the authoritative session snapshot via `client.session.messages({ path: { id } })` (see the [opencode SDK docs](https://opencode.ai/docs/sdk/)), then spawns `atatus-hook-opencode` detached and pipes the snapshot to stdin. The shim contains no tracing logic.
2. **Python snapshot reconciler** (`atatus-hook-opencode`). Reads the snapshot, walks `{info, parts}[]`, and emits any NEW `Turn`/`LLM`/`TOOL` spans deduped by message id and tool `callID`. opencode's `AssistantMessage` already carries final, cumulative `tokens` and `cost`, so no per-delta coalescing is needed.

## Span tree

Each trace covers one **turn** (one user prompt → the assistant's response → `session.idle`) and preserves the requesting-message and subagent hierarchy:

| Span | Kind | Description |
|------|------|-------------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt; `output.value` is the assistant's final text. Timestamps come from `message.time.created` / `time.completed`. |
| `LLM: <model>` | LLM | Child of `Turn`, or of a child session's `AGENT`. The assistant message. Carries the message ID, `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. |
| `<tool>` | TOOL | Child of the requesting `LLM`, correlated by `ToolPart.messageID`; falls back to `Turn` only when that relation is unavailable. Records `tool.name`, redacted input/output, and `tool.command`/`tool.file_path`/`tool.query`/`tool.url` where applicable. Timestamps come from `toolPart.state.time.start` / `.end`. |
| `Agent: <name>` | AGENT | Child of a `task` TOOL when the SDK child session has the matching `Session.parentID`; child LLM/TOOL spans nest below it. |

## Troubleshoot

Common issues and fixes for opencode:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check reconciler log: `tail -20 ~/.atatus/harness/logs/opencode.log`. Confirm the plugin is in place: `ls ~/.config/opencode/plugin/atatus-tracing.ts`. |
| Plugin not loading | opencode loads plugins from `~/.config/opencode/plugin/` on startup. If the file exists but isn't loading, restart opencode and check the opencode CLI output for plugin errors. |
| Reconciler entry point missing | The shim spawns the reconciler by absolute path; verify the binary exists at `~/.atatus/harness/venv/bin/atatus-hook-opencode` (or `~/.atatus/harness/venv/Scripts/atatus-hook-opencode.exe` on Windows). Rerun `./install.sh opencode` to reinstall the venv entry point. |
| Spans appear partial / missing tool spans | Snapshots are pulled on `message.updated` (assistant complete) and `session.idle`. Pending or running tool parts won't emit a span until they reach `completed` or `error` state. Wait for the turn to finish. |
| Duplicate spans | The reconciler dedupes by message id and tool `callID`. If you still see duplicates, set `ATATUS_VERBOSE=true` and check `~/.atatus/harness/logs/opencode.log` for dedup hits to confirm state tracking is working. |
| Sub-agent (`task` tool) trace not linked to parent | Wait for the parent session to reach `session.idle`. Linking needs a completed `task` part carrying `state.metadata.sessionId` plus a child session whose `Session.parentID` matches the requesting session; mismatched or foreign snapshots are rejected. A session already running when the plugin was upgraded may need restarting. |
| Collector unreachable | Check connectivity: `curl -sf --connect-timeout 3 --max-time 5 <endpoint>/v1/traces` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching opencode |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching opencode |
| Want raw snapshot payloads for inspection | Set `ATATUS_TRACE_DEBUG=true` env var; payloads land under `~/.atatus/harness/state/debug/` as `opencode_reconcile_<ts>.json` / `opencode_close_<ts>.json` |
| Wrong project name | Set `harnesses.opencode.project_name` in `~/.atatus/harness/config.json` (default: `"opencode"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching opencode |
| Tracing not toggling | Ensure `ATATUS_TRACE_ENABLED` is exported in your shell, not just set — the opencode process and any plugin-spawned reconciler inherit host env vars |
