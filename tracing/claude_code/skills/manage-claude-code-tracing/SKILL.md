---
name: manage-claude-code-tracing
description: Set up and configure Atatus tracing for Claude Code sessions or Agent SDK applications. Use when users want to set up tracing, configure Atatus, create a new Atatus project, get an API key, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up tracing", "configure Atatus", "enable tracing", "setup-claude-code-tracing", "create Atatus project", "get Atatus API key", "agent sdk tracing", or any request about connecting Claude Code or the Agent SDK to Atatus for observability.
---

# Setup Tracing

Configure OpenInference tracing for Claude Code sessions or Agent SDK applications to Atatus. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Are they using the Claude Code CLI or the Agent SDK?**
   - CLI -> Continue to step 2
   - Agent SDK (Python or TypeScript) -> Go to [Agent SDK Setup](#agent-sdk-setup)

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

**Important:** For marketplace installs, users must run this setup skill before tracing will work. The `send_span()` function requires `~/.atatus/harness/config.json` to exist for backend credential resolution.

Configuration has two parts:

1. **Backend config** (`~/.atatus/harness/config.json`) -- backend credentials and per-harness settings, read by `send_span()`. This skill creates it.
2. **Claude settings** (`~/.claude/settings.json` or `.claude/settings.local.json`) -- tracing feature flags and user-level env vars

### Ask the user for:

1. **Scope**: Global (`~/.claude/settings.json`) or project-local (`.claude/settings.local.json`)
2. **Credentials** (only if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (default: `https://otel-rx.atatus.com`)
3. **Project name** (optional): defaults to `"claude-code"`, stored under `harnesses.claude-code.project_name`
4. **User ID** (optional): Set `ATATUS_USER_ID` to identify spans by user (useful for teams)

### Write the backend config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.claude-code` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "claude-code": {
      "project_name": "claude-code",
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
    "claude-code": {
      "project_name": "claude-code",
      "target": "atatus",
      "endpoint": "https://otel-rx.atatus.com",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.claude-code.endpoint`.

### Write the Claude settings

**Determine the config file:**
- Global: `~/.claude/settings.json`
- Project-local: `.claude/settings.local.json` (create directory if needed: `mkdir -p .claude`)

Read the file (or create `{}` if it doesn't exist), then merge env vars into the `"env"` object.

```json
{
  "env": {
    "ATATUS_TRACE_ENABLED": "true"
  }
}
```

If a custom project name was provided, set it in `harnesses.claude-code.project_name` in the config (`~/.atatus/harness/config.json`), not as an env var.

If a user ID was provided, also set `"ATATUS_USER_ID": "<id>"`. This adds a `user.id` attribute to all traced spans.

**Example workflow:**
```bash
# For project-local
mkdir -p .claude
echo '{}' > .claude/settings.local.json
# Then use an editor to add env vars
```

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials.
2. **Atatus** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.

### Confirm

Tell the user:
- Backend config saved to `~/.atatus/harness/config.json`
- Claude settings saved to the chosen file:
  - Global: `~/.claude/settings.json`
  - Project-local: `.claude/settings.local.json`
- Restart the Claude Code session for tracing to take effect
- Spans are sent directly to the backend from hooks — no background process needed
- After restarting, traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/claude-code.log`; set `ATATUS_VERBOSE` to `"true"` under `env` in `~/.claude/settings.json` to also capture routine hook activity (session_start, span emits, state transitions)

**Note**: Project-local settings override global settings for the same variables.

## Agent SDK Setup

For users building with the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) (Python or TypeScript), the tracing plugin loads as a local plugin. **This section provides code and configuration for the developer to add to their application** -- the agent cannot set this up at runtime since plugin paths and settings must be configured before the SDK session starts.

**Important:** The user must use `ClaudeSDKClient` -- the standalone `query()` function does **not** support hooks, so tracing will not work with it.

### How to guide the user

When a user asks about Agent SDK tracing setup, provide them with the steps below to integrate into their own code. Do NOT try to execute `export` commands or modify their application source -- instead, give them the snippets to copy.

### 1. Choose a backend

Ask the user which backend they want. If they don't have credentials yet, walk them through [Set Up Atatus](#set-up-atatus) first, then return here.

### 2. Get the plugin path

Ask the user: **"Have you already installed this plugin via the Claude Code CLI?"**

**If yes (already installed via the `install.sh` / `install.bat` flow):** The plugin lives inside the harness install directory at `~/.atatus/harness/tracing/claude_code`.

**If yes (installed via the Claude marketplace):** They can reference it from the CLI cache. Tell them to check `~/.claude/plugins/installed_plugins.json` for the exact path.

**If no:** Tell them to clone the repo into their project:
```bash
git clone https://github.com/atatus/coding-harness-tracing.git
```
The plugin path will be `./coding-harness-tracing/tracing/claude_code`.

> Tip: `tracing.claude_code.agent_sdk.claude_options()` returns a pre-configured `ClaudeAgentOptions` with the plugin path and `setting_sources=["user"]` already wired in, so users can skip the manual plumbing in step 5 below when the harness is installed via `install.sh`.

No Python dependencies are needed -- spans are sent as OTLP/JSON over HTTP.

### 3. Set up the backend config

Ensure `~/.atatus/harness/config.json` has the correct backend credentials (see [Configure Settings](#configure-settings) above).

### 4. Create a settings file

The Agent SDK spawns a Claude Code subprocess that does **not** inherit the user's shell environment variables. Tracing env vars must be passed via a settings file referenced in the `ClaudeAgentOptions`.

Tell the user to create a `settings.local.json` file (or similar):

```json
{
  "env": {
    "ATATUS_TRACE_ENABLED": "true"
  }
}
```

Optional env vars that can also be added to the settings file:
- `ATATUS_USER_ID`: User identifier added as `user.id` attribute to all spans (useful for teams)
- `ATATUS_DRY_RUN`: Set to `"true"` to test without sending data
- `ATATUS_VERBOSE`: Set to `"true"` for debug output

To customize the project name, set it in `harnesses.claude-code.project_name` in the config (`~/.atatus/harness/config.json`) rather than as an env var.

### 5. Add the plugin to their code

Give the user the appropriate snippet to add to their application. They must use `ClaudeSDKClient` and pass both the plugin path (from step 2) and the settings file (from step 4):

**Python:**
```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

PLUGIN_PATH = "./coding-harness-tracing/tracing/claude_code"  # or ~/.atatus/harness/tracing/claude_code if installed via install.sh

options = ClaudeAgentOptions(
    plugins=[{"type": "local", "path": PLUGIN_PATH}],
    settings="./settings.local.json",
)
async with ClaudeSDKClient(options=options) as client:
    await client.query("Your prompt here")
    async for message in client.receive_response():
        print(message)
```

**TypeScript:**
```typescript
import { ClaudeSDKClient } from "@anthropic-ai/claude-agent-sdk";

const PLUGIN_PATH = "./coding-harness-tracing/tracing/claude_code"; // or ~/.atatus/harness/tracing/claude_code if installed via install.sh

const client = new ClaudeSDKClient({
  plugins: [{ type: "local", path: PLUGIN_PATH }],
  settings: "./settings.local.json"
});

await client.connect();
await client.query("Your prompt here");
for await (const message of client.receiveResponse()) {
  console.log(message);
}
await client.close();
```

### 6. Validate

Tell the user to add `"ATATUS_DRY_RUN": "true"` to their settings file to verify hooks fire without sending data, and check `~/.atatus/harness/logs/claude-code.log` for output.

### Agent SDK Compatibility

For full Agent SDK documentation, see: https://platform.claude.com/docs/en/agent-sdk/overview

- **Important**: You must use `ClaudeSDKClient` -- the standalone `query()` function does not support hooks and tracing will not work.
- **Hook coverage**: The CLI registers a broad hook set (16 events as of this writing, including session-lifecycle, prompt/tool, compaction, and permission events). The Agent SDKs may expose a smaller subset depending on version; check the SDK's `HookEvent` enum / type for the events your SDK version supports.
- **Python SDK** in particular has historically not supported `SessionStart`, `SessionEnd`, `Notification`, and `PermissionRequest`. The plugin handles this automatically -- session state is lazily initialized on the first `UserPromptSubmit`, so core tracing (LLM spans, tool spans, subagent spans) works fully.
- Tracing env vars must be passed via a settings file in `ClaudeAgentOptions` -- the SDK subprocess does not inherit shell environment variables.
- If the user is **troubleshooting** an existing Agent SDK setup, you can help by checking log files (`~/.atatus/harness/logs/claude-code.log`), verifying the settings file contains the correct env vars, verifying `~/.atatus/harness/config.json` has correct backend credentials, or enabling dry-run mode.

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Check `ATATUS_TRACE_ENABLED` is `"true"` in Claude settings, and verify config exists: `cat ~/.atatus/harness/config.json` |
| Config missing | Run the installer or create `~/.atatus/harness/config.json` manually (include `harnesses` section) |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| No output in terminal | Hook stderr is discarded by Claude Code; check `~/.atatus/harness/logs/claude-code.log` |
| Want to test without sending | Set `ATATUS_DRY_RUN` to `"true"` in env config |
| Want verbose logging | Set `ATATUS_VERBOSE` to `"true"` in env config |
| Wrong project name | Set `harnesses.claude-code.project_name` in `~/.atatus/harness/config.json` (default: `"claude-code"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` in env config to add `user.id` to all spans |
