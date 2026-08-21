---
name: manage-omp-tracing
description: Set up and configure Atatus tracing for Oh My Pi (omp) terminal coding sessions. Use when users want to set up tracing, configure Atatus for Oh My Pi, enable/disable omp tracing, or troubleshoot tracing issues. Triggers on "set up omp tracing", "configure Atatus for Oh My Pi", "configure Atatus for omp", "enable omp tracing", "setup-omp-tracing", or any request about connecting omp / Oh My Pi to Atatus for observability.
---

# Setup omp Tracing

Configure OpenInference tracing for **Oh My Pi (omp)** terminal coding sessions to Atatus. Like opencode, omp loads its extensions [in-process inside its Bun runtime](https://omp.sh/docs/hooks) — there is no per-event subprocess. The integration ships as a small TypeScript hook shim that forwards omp's once-fired lifecycle events to a Python handler (`atatus-hook-omp`) which emits spans. Spans are sent directly to the backend from the handler — no separate buffer/collector service is required.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.omp/extensions/atatus-tracing.ts` for the Atatus hook shim
   - Check `~/.omp/agent/settings.json` for the shim's path in the `extensions` array
   - Check `~/.atatus/harness/config.json` for the `harnesses.omp` block
   - If all are present -> Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

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

0. **Whether to reuse an existing config**: if `harnesses.omp` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (leave blank unless you are running Atatus on-premise)
2. **Project name**: defaults to `"omp"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.omp.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/omp}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.omp` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "omp": {
      "project_name": "omp",
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
    "omp": {
      "project_name": "omp",
      "target": "atatus",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.omp.endpoint`.

### Activate the omp hook

omp does **not** auto-discover an extensions directory — the `extensions` key in `~/.omp/agent/settings.json` is an explicit array of file/dir paths omp loads ([hook docs](https://omp.sh/docs/hooks)). The installer drops the Atatus hook shim at `~/.omp/extensions/atatus-tracing.ts` **and** registers that absolute path in the `extensions` array, e.g.:

```json
{ "extensions": ["/Users/me/.omp/extensions/atatus-tracing.ts"] }
```

omp picks up the registered extension on next launch.

Install or reinstall via the installer:

```bash
./install.sh omp
```

To uninstall:

```bash
./install.sh uninstall omp
```

Uninstall removes the shim's path from the `extensions` array, deletes the hook file at `~/.omp/extensions/atatus-tracing.ts` (only if it carries the Atatus header marker — the installer never touches the user's own extensions), and removes the `harnesses.omp` block from `~/.atatus/harness/config.json`.

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.omp`.
2. **Atatus** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hook installed**: Verify `~/.omp/extensions/atatus-tracing.ts` exists and starts with the Atatus header marker.
4. **Hook registered**: Verify the shim's absolute path appears in the `extensions` array of `~/.omp/agent/settings.json` (omp does not auto-discover — registration is required).
5. **Handler entry point**: Verify the handler binary exists at `~/.atatus/harness/venv/bin/atatus-hook-omp` (or `~/.atatus/harness/venv/Scripts/atatus-hook-omp.exe` on Windows). The shim spawns this binary by absolute path — it does not rely on PATH resolution. `install.sh` installs it as a venv entry point.

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- omp hook shim installed at `~/.omp/extensions/atatus-tracing.ts`
- Shim path registered in the `extensions` array of `~/.omp/agent/settings.json` — omp requires explicit registration (no auto-discovery)
- Spans are sent directly to the backend from the handler — no background process needed
- After saving, open a new omp session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching omp)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors and handler stderr are always written to `~/.atatus/harness/logs/omp.log` (the adapter redirects Python stderr there via `ATATUS_LOG_FILE`); set `ATATUS_VERBOSE=true` in the shell before launching omp to also capture routine handler activity (event dispatch, span emits, state transitions)
- Toggle tracing on/off via `ATATUS_TRACE_ENABLED` env var (must be exported in the user's shell before launching omp — the shim and handler inherit host env vars)
- Tail the log file at `~/.atatus/harness/logs/omp.log` for real-time debugging
- Mention `ATATUS_TRACE_DEBUG=true` to dump raw event payloads under `~/.atatus/harness/state/debug/` (files are named `omp_before_agent_start_<ts>.json` / `omp_turn_end_<ts>.json` / `omp_agent_end_<ts>.json` / `omp_session_shutdown_<ts>.json`) for inspection

## Architecture (How spans are produced)

omp loads its extensions **in-process** inside its Bun runtime ([hook docs](https://omp.sh/docs/hooks)). Unlike opencode, omp exposes rich, **once-fired** lifecycle events that already carry final, structured data, so the Atatus integration is a stateful event-forward (no snapshot reconciliation, no dedup). It is split into two pieces:

1. **TypeScript hook shim** at `~/.omp/extensions/atatus-tracing.ts`. A dumb bridge. On a whitelist of lifecycle events — `before_agent_start` (the prompt), `turn_end` (the completed `AssistantMessage` with inline token usage + model, plus that turn's `toolResults`), `agent_end` (run finished), and `session_shutdown` — it spawns `atatus-hook-omp` detached and pipes the event payload to stdin. The shim contains no tracing logic and never blocks omp's event loop.
2. **Python event handler** (`atatus-hook-omp`). A small state machine keyed by session id. It dispatches on `payload["type"]`, accumulates per-session state, and emits `Turn`/`LLM`/`TOOL` spans on receipt. Pairs each `ToolCall` with its `ToolResultMessage` by id to build TOOL spans with both input args and output.

## Span tree

Each trace covers one **agent run** (one user prompt → the agent's internal turn/tool-use loop → its final answer). A run usually contains several model calls; one trace covers all of them. The tree:

| Span | Kind | Description |
|------|------|-------------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt (from `before_agent_start`); `output.value` is the final assistant message's text. One per agent run. |
| `LLM: <model>` | LLM | Child of `Turn`. One per `turn_end` (one per model call in the loop). Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. omp surfaces token usage inline on the assistant message, so it **is** captured. |
| `<tool>` | TOOL | Child of `Turn`. One per `ToolResultMessage` in a `turn_end`, paired with its originating `ToolCall` by id. Records `tool.name`, redacted input args + output, and tool-specific attributes; errors are recorded with span status. |

## Troubleshoot

Common issues and fixes for omp:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check handler log: `tail -20 ~/.atatus/harness/logs/omp.log`. Confirm the hook is in place: `ls ~/.omp/extensions/atatus-tracing.ts`. |
| Hook not loading | omp does **not** auto-discover an extensions dir. Confirm the shim's absolute path is in the `extensions` array of `~/.omp/agent/settings.json`, then restart omp and check its CLI output for extension errors. |
| Handler entry point missing | The shim spawns the handler by absolute path; verify the binary exists at `~/.atatus/harness/venv/bin/atatus-hook-omp` (or `~/.atatus/harness/venv/Scripts/atatus-hook-omp.exe` on Windows). Rerun `./install.sh omp` to reinstall the venv entry point. |
| Missing LLM or tool spans | Spans emit on `turn_end` (one LLM span per model call, one TOOL span per tool result). If a turn hasn't completed yet, its spans won't appear until the event fires. Wait for the agent run to finish (`agent_end`). |
| Trace missing the final answer | `output.value` on the `Turn` span comes from the final assistant message. Confirm the run reached `agent_end`. |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching omp |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching omp |
| Want raw event payloads for inspection | Set `ATATUS_TRACE_DEBUG=true` env var; payloads land under `~/.atatus/harness/state/debug/` as `omp_before_agent_start_<ts>.json` / `omp_turn_end_<ts>.json` / `omp_agent_end_<ts>.json` / `omp_session_shutdown_<ts>.json` |
| Wrong project name | Set `harnesses.omp.project_name` in `~/.atatus/harness/config.json` (default: `"omp"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching omp |
| Tracing not toggling | Ensure `ATATUS_TRACE_ENABLED` is exported in your shell, not just set — the omp process and any shim-spawned handler inherit host env vars |
