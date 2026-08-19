---
name: manage-cursor-tracing
description: Set up and configure Atatus tracing for Cursor IDE sessions. Use when users want to set up tracing, configure Atatus for Cursor, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up cursor tracing", "configure Atatus for Cursor", "enable cursor tracing", "setup-cursor-tracing", or any request about connecting Cursor to Atatus for observability.
---

# Setup Cursor Tracing

Configure OpenInference tracing for Cursor IDE sessions to Atatus. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **First, determine the install type.** Was tracing installed as a **Cursor plugin** (via
   `/add-plugin` in Cursor 2.5+, which registers hooks automatically), or **manually** by running
   `install.sh`?
   - If unsure, ask directly: "Did you install Atatus tracing as a Cursor plugin (via /add-plugin),
     or by running install.sh?"
   - Heuristic 1: look for a `cursor-tracing` plugin directory in Cursor's installed-plugins location.
   - Heuristic 2: check whether `.cursor/hooks.json` already contains `atatus-hook-cursor` entries —
     if so it is a manual `install.sh` install; if absent, it is a plugin install (or a fresh setup).
   - This only affects the [Activate Cursor hooks](#activate-cursor-hooks) step. Credentials are
     identical for both.

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

0. **Whether to reuse an existing config**: if `harnesses.cursor` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (default: `https://otel-rx.atatus.com`)
2. **Project name**: defaults to `"cursor"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.cursor.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/cursor}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.cursor` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "cursor": {
      "project_name": "cursor",
      "target": "atatus",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

**Atatus:**
```json
{
  "harnesses": {
    "cursor": {
      "project_name": "cursor",
      "target": "atatus",
      "endpoint": "https://otel-rx.atatus.com",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.cursor.endpoint`.

### Activate Cursor hooks

**This step depends on install type** (see [How to Use This Skill](#how-to-use-this-skill) step 1).

#### Plugin install (Cursor `/add-plugin`)

**Skip this step entirely.** The plugin's bundled `hooks/hooks.json` already registers every Cursor
hook event. There is nothing to write to `.cursor/hooks.json`. After saving credentials, tell the
user to start a new Cursor session — traces begin on the next interaction.

> **Warning:** do NOT add `.cursor/hooks.json` entries on top of a plugin install. Cursor would then
> route each event to the handler twice — once via the plugin, once via the project file — producing
> duplicate spans for every hook. If the user has entries pointing at `atatus-hook-cursor` left over
> from an earlier `install.sh` setup, remove them before relying on the plugin.

#### Manual install (`install.sh`)

Cursor uses a `.cursor/hooks.json` file in the project root to route hook events to the handler. All events route to a single `atatus-hook-cursor` CLI entry point, which dispatches based on `hook_event_name` in the JSON payload.

Create `.cursor/hooks.json` in the user's project (or merge into it if it already exists):

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "sessionEnd": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeSubmitPrompt": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterAgentResponse": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterAgentThought": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeShellExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterShellExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeMCPExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterMCPExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeReadFile": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterFileEdit": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "stop": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeTabFileRead": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterTabFileEdit": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "postToolUse": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }]
  }
}
```

If the user already has a `.cursor/hooks.json` with other hooks, merge the Atatus entries into the existing arrays for each event.

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials.
2. **Atatus** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**:
   - **Manual install**: verify `.cursor/hooks.json` exists in the project root and contains the Atatus hook entries.
   - **Plugin install**: no project-level `.cursor/hooks.json` is needed — the plugin registers hooks itself. Confirm `cursor-tracing` is listed as installed in Cursor.

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- Cursor hooks activated via `.cursor/hooks.json`
- Spans are sent directly to the backend from hooks — no background process needed
- After saving, open a new Cursor session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching Cursor)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/cursor.log`; set `ATATUS_VERBOSE=true` in the shell before launching Cursor to also capture routine hook activity

## Hook Events

### IDE Hooks

Cursor IDE fires 15 hook events. Here's what each one traces:

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `sessionStart` | Session Start | CHAIN | Root span for the conversation; captures session metadata |
| `beforeSubmitPrompt` | User Prompt | CHAIN | Root span for the turn; captures prompt text, model, attachments |
| `afterAgentResponse` | Agent Response | LLM | LLM response text and model name; span is deferred and sent at end-of-turn (on `stop`) so it can carry per-turn token usage |
| `afterAgentThought` | Agent Thinking | CHAIN | Agent thinking/reasoning text |
| `beforeShellExecution` | (state push) | -- | Saves command and start time to disk state |
| `afterShellExecution` | Shell | TOOL | Merged span with command input and output |
| `beforeMCPExecution` | (state push) | -- | Saves tool name, input, and start time |
| `afterMCPExecution` | MCP: {tool} | TOOL | Merged span with tool input and result |
| `beforeReadFile` | Read File | TOOL | File path being read |
| `afterFileEdit` | File Edit | TOOL | File path and edit details |
| `beforeTabFileRead` | Tab Read File | TOOL | Tab file read (file path) |
| `afterTabFileEdit` | Tab File Edit | TOOL | Tab file edit (path and edits) |
| `postToolUse` | Tool: {name} | TOOL | Generic tool span; postToolUse is suppressed for tools with a dedicated handler (Shell, Read, File Edit, Tab ops, MCP) to avoid duplicate spans |
| `stop` | Agent Stop | CHAIN | Per-turn stop event with status / loop_count / duration metadata; per-turn token counts are attached to the deferred `Agent Response` (LLM) span when it is sent at end-of-turn |
| `sessionEnd` | Session End | CHAIN | End-of-session span with duration and final status |

Shell and MCP events use a disk-backed state stack to merge before/after context into single spans with both input and output.

### CLI Hooks

Cursor CLI currently emits a smaller hook surface than the IDE. The supported
CLI hooks in this package are:

- `sessionStart`
- `sessionEnd`
- `beforeShellExecution`
- `afterShellExecution`
- `afterFileEdit`
- `postToolUse`
- `stop`

Cursor CLI hooks do not currently emit afterAgentResponse or afterAgentThought.

Full Cursor CLI assistant and thinking coverage requires parsing --output-format stream-json, which is out of scope for this change.

### What We Capture

- **`sessionStart`** produces a `Session Start` CHAIN span that acts as the root for the conversation.
- **`sessionEnd`** produces a `Session End` CHAIN span with `cursor.session.duration_ms`, `cursor.session.final_status`, `cursor.session.reason`, and end-of-session token counts when available.
- **`stop`** produces an `Agent Stop` CHAIN span carrying per-turn status / loop_count / duration metadata. On the Cursor IDE, per-turn token usage is attached to the `Agent Response` (LLM) span instead: that span is deferred from `afterAgentResponse` and sent at end-of-turn when `stop` fires, populated from the `stop` payload with `llm.token_count.prompt` (the total prompt — Cursor's `input_tokens` is the uncached remainder, so cache reads/writes are added back in), `llm.token_count.completion`, the OpenInference cache subsets `llm.token_count.prompt_details.cache_read` / `llm.token_count.prompt_details.cache_write`, `llm.token_count.total`, and `llm.model_name`. Cursor CLI does not emit `afterAgentResponse`, so there is no LLM span to attach to; for the CLI path, token counts remain on the `Agent Stop` / `Session End` CHAIN span as before.
- **`postToolUse`** produces a generic `Tool: <name>` span ONLY for tools without a dedicated handler. Shell, file read/edit, tab file ops, and MCP execution are handled by their dedicated `before*`/`after*` events; the generic postToolUse is suppressed for these to avoid duplicate spans.

Every span includes `cursor.conversation.id` as a span attribute. Since `sessionStart` and per-turn activity use different `trace_id` values, `cursor.conversation.id` is the recommended cross-trace join key in Atatus. To gather all activity for a Cursor session regardless of trace, filter spans by `attributes.cursor.conversation.id = "<id>"`.

### Hooks JSON Example (IDE + CLI)

> **Plugin users: do NOT hand-write `.cursor/hooks.json` from this example.** It is a reference for
> the **manual `install.sh` path only**. Under a `/add-plugin` install the plugin already registers
> every event, and adding these on top routes each hook twice — duplicate spans for everything. See
> [Activate Cursor hooks > Plugin install](#plugin-install-cursor-add-plugin).

When configuring `.cursor/hooks.json` on a manual `install.sh` install, include both IDE and CLI events:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "sessionEnd": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeSubmitPrompt": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterAgentResponse": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterAgentThought": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeShellExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterShellExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeMCPExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterMCPExecution": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeReadFile": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterFileEdit": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "stop": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "beforeTabFileRead": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "afterTabFileEdit": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }],
    "postToolUse": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-cursor" }]
  }
}
```

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check hook log: `tail -20 ~/.atatus/harness/logs/cursor.log` |
| Config missing | Run the installer or create `~/.atatus/harness/config.json` manually (include `harnesses.cursor` section) |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| Hooks not firing (manual install) | Verify `.cursor/hooks.json` exists in the project root and paths are correct (use absolute paths) |
| Hooks not firing (plugin install) | Verify `cursor-tracing` is enabled in Cursor, start a fresh Cursor session after installing, and check `~/.atatus/harness/logs/cursor.log` for errors |
| Duplicate spans / every event traced twice | A plugin install *plus* manual `.cursor/hooks.json` entries pointing at `atatus-hook-cursor` fires each hook twice. Remove the manual entries and keep one install path. |
| Shell/MCP spans missing input | State push failed -- check that `~/.atatus/harness/state/cursor/` is writable |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching Cursor |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching Cursor |
| Wrong project name | Set `harnesses.cursor.project_name` in `~/.atatus/harness/config.json` (default: `"cursor"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching Cursor |
