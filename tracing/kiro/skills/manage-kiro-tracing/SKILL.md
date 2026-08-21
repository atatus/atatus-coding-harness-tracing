---
name: manage-kiro-tracing
description: Set up and configure Atatus tracing for Kiro CLI sessions. Use when users want to set up Kiro tracing, configure Atatus for Kiro, enable/disable tracing, choose or set a default traced agent, or troubleshoot Kiro tracing issues. Triggers on "set up kiro tracing", "configure Atatus for Kiro", "enable kiro tracing", "setup-kiro-tracing", "kiro agent tracing", or any request about connecting Kiro CLI to Atatus for observability.
---

# Setup Kiro Tracing

Configure OpenInference tracing for **Kiro CLI** sessions to Atatus. Spans are sent directly to the backend from hooks — no background process or backend-specific dependencies are needed in the user's environment. Each traced session emits LLM turns, tool calls, cost in credits, model information, and turn duration.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.kiro/agents/` for an agent file containing `atatus-hook-kiro` in its `hooks` block
   - Check `~/.atatus/harness/config.json` for the `harnesses.kiro` block
   - If both are present → Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

2. **Do they already have credentials?**
   - Yes → Jump to [Configure Settings](#configure-settings)
   - No → Continue to step 3

3. **Do they need to configure credentials?**
   - Yes -> Go to [Set Up Atatus](#set-up-atatus)

4. **Are they troubleshooting?**
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

That value is `api_key` in the shared config, and it is required.

**No Python dependencies are needed** — spans are sent as OTLP/JSON over HTTP.

Then proceed to [Configure Settings](#configure-settings).

## Configure Settings

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.atatus/harness/config.json` to exist for backend credential resolution.

Configuration has two parts:

1. **Backend config** (`~/.atatus/harness/config.json`) — backend credentials and per-harness settings, read by `send_span()`.
2. **Kiro agent config** (`~/.kiro/agents/<agent>.json`) — the agent the user runs with, containing the tracing `hooks` block.

### Ask the user for:

0. **Whether to reuse an existing config**: if `harnesses.kiro` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (leave blank unless you are running Atatus on-premise)
2. **Project name**: defaults to `"kiro"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.kiro.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)
4. **Agent name** (Kiro-specific): defaults to `atatus-traced`. The hooks are written into `~/.kiro/agents/<name>.json`. Use an existing agent if the user wants to add tracing to their current workflow without switching agents.
5. **Set as Kiro's default?** (Kiro-specific): If yes, the installer runs `kiro-cli agent set-default <name>` so `kiro-cli chat` (no `--agent` flag) uses the traced agent.

### Write the backend config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/kiro}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.kiro` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "kiro": {
      "project_name": "kiro",
      "target": "atatus",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.kiro.endpoint`.

### Activate Kiro hooks

Kiro registers hooks per agent. Each agent is a JSON file under `~/.kiro/agents/<name>.json`. The installer either creates the file fresh (default agent: `atatus-traced`) or merges hooks into an existing agent the user picks.

A freshly-created `atatus-traced` agent looks like:

```json
{
  "name": "atatus-traced",
  "description": "Kiro agent with Atatus tracing hooks installed.",
  "prompt": null,
  "mcpServers": {},
  "tools": ["*"],
  "toolAliases": {},
  "allowedTools": [],
  "resources": [],
  "hooks": {
    "agentSpawn":       [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-kiro" }],
    "userPromptSubmit": [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-kiro" }],
    "preToolUse":       [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-kiro" }],
    "postToolUse":      [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-kiro" }],
    "stop":             [{ "command": "~/.atatus/harness/venv/bin/atatus-hook-kiro" }]
  },
  "toolsSettings": {},
  "includeMcpJson": true,
  "model": null
}
```

All five events route to a single `atatus-hook-kiro` CLI entry point that dispatches based on the event name in the payload.

If the user already has an agent JSON they want to trace, merge the five entries above into its existing `hooks` block — do not overwrite the rest of the agent definition.

To install or reinstall via the installer:

```bash
./install.sh kiro
```

To uninstall (removes only the Atatus hook entries from each agent file; deletes the agent file only if the installer created it):

```bash
./install.sh uninstall kiro
```

### Set as default agent (optional)

If the user wants `kiro-cli chat` to use the traced agent automatically:

```bash
kiro-cli agent set-default atatus-traced
```

Otherwise, they explicitly pass `--agent`:

```bash
kiro-cli chat --agent atatus-traced
```

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the file contains the `harnesses.kiro` block.
2. **Agent file exists**: Run `cat ~/.kiro/agents/<agent>.json` to verify the `hooks` block has all five events pointing at `atatus-hook-kiro`.
3. **Atatus** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
4. **Kiro accepts the agent config** (optional, requires `kiro-cli` on PATH): `kiro-cli agent validate --path ~/.kiro/agents/<agent>.json`.

### Confirm

Tell the user:
- Backend config saved to `~/.atatus/harness/config.json`
- Tracing hooks registered in `~/.kiro/agents/<agent>.json`
- Run a session with `kiro-cli chat` (if you set it as default) or `kiro-cli chat --agent <agent>`
- Spans are sent directly to the backend from hooks — no background process needed
- Traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching Kiro)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/kiro.log`; set `ATATUS_VERBOSE=true` in the shell before launching Kiro to also capture routine hook activity

## Hook Events

Kiro fires 5 hook events. The first three accumulate state; only `postToolUse` and `stop` emit spans.

| Event | Emits span? | Span name | Kind | Description |
|-------|-------------|-----------|------|-------------|
| `agentSpawn` | No | — | — | Initializes per-session state (trace correlation, tool stack) |
| `userPromptSubmit` | No | — | — | Starts a turn: generates `trace_id` and `span_id`, saves raw prompt |
| `preToolUse` | No | — | — | Pushes tool input + start time to a FIFO stack (Kiro doesn't expose a tool-call id) |
| `postToolUse` | Yes | `Tool: {name}` | TOOL | Pops matching tool slot, builds TOOL span with serialized input/output |
| `stop` | Yes | LLM turn span | LLM | Builds the parent LLM span for the turn, enriched from the session sidecar at `~/.kiro/sessions/cli/<session_id>.json` (model, cost, duration, context usage) |

### Span attributes (LLM span)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Kiro session UUID |
| `openinference.span.kind` | `LLM` |
| `input.value` | User prompt |
| `output.value` | Assistant response |
| `llm.model_name` | Model ID from the session sidecar (e.g. `auto`) |
| `llm.token_count.prompt` / `.completion` / `.total` | Token counts when reported (omitted when 0) |
| `kiro.cost.credits` | Cost in credits from metering data |
| `kiro.metering_usage` | Full metering usage JSON |
| `kiro.turn_duration_ms` | Turn duration in milliseconds |
| `kiro.agent_name` | Name of the Kiro agent (e.g. `atatus-traced`) |
| `kiro.context_usage_percentage` | Context window usage percentage |

### Span attributes (TOOL span)

| Attribute | Description |
|-----------|-------------|
| `tool.name` | Tool name (alias form) |
| `tool.description` | Purpose of the tool call (from `__tool_use_purpose` in tool input) |
| `input.value` | Serialized tool input JSON |
| `output.value` | Serialized tool response JSON |

TOOL spans are parented to the LLM turn they belong to via the FIFO tool-state stack.

## Known limitations

- **Token counts are typically 0.** Kiro currently meters in credits, not tokens. Token count attributes are omitted when the value is 0. See `kiro.cost.credits` for usage tracking instead.
- **FIFO tool matching.** Kiro does not expose a tool-call ID, so pre/post tool events are matched using a FIFO stack. This assumes serial tool execution within a session — concurrent tool calls would mismatch.
- **Sidecar read is fail-soft.** The session sidecar at `~/.kiro/sessions/cli/<session_id>.json` may not exist or may lag behind hook events due to a flush race. When this happens, the LLM span is emitted with basic attributes only (no model name, cost, or duration).

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check hook log: `tail -20 ~/.atatus/harness/logs/kiro.log` |
| Hooks not firing | Verify the agent JSON has all five hooks under `hooks` and that each `command` resolves to the `atatus-hook-kiro` venv binary. Run `kiro-cli agent validate --path ~/.kiro/agents/<agent>.json` if `kiro-cli` is on PATH |
| Wrong agent in use | Either pass `--agent <name>` to `kiro-cli chat`, or set the agent as default: `kiro-cli agent set-default <name>` |
| Config missing | Run `./install.sh kiro` or create `~/.atatus/harness/config.json` manually with a `harnesses.kiro` section |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| LLM spans missing model name / cost | The session sidecar at `~/.kiro/sessions/cli/<session_id>.json` was unavailable when `stop` fired. Confirm the sidecar exists for the session — enrichment is fail-soft so the span is emitted without those attributes |
| Tool spans mismatched or orphaned | Concurrent tool execution can break the FIFO match. The handler emits an "orphan" TOOL span when the stack is empty — search the hook log for `no pending tool slot` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching Kiro |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching Kiro |
| Wrong project name | Set `harnesses.kiro.project_name` in `~/.atatus/harness/config.json` (default: `"kiro"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching Kiro |
