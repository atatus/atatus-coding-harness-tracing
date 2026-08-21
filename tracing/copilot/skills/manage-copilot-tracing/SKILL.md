---
name: manage-copilot-tracing
description: Set up and configure Atatus tracing for GitHub Copilot sessions. Use when users want to set up tracing, configure Atatus for Copilot, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up copilot tracing", "configure Atatus for Copilot", "enable copilot tracing", "setup-copilot-tracing", or any request about connecting GitHub Copilot to Atatus for observability.
---

# Setup Copilot Tracing

Configure OpenInference tracing for **GitHub Copilot** in VS Code Copilot. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 2

2. **Do they need to configure credentials?**
   - Yes -> Go to [Set Up Atatus](#set-up-atatus)

3. **Are they troubleshooting?**
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

0. **Whether to reuse an existing config**: if `harnesses.copilot` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (leave blank unless you are running Atatus on-premise)
2. **Project name**: defaults to `"copilot"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.copilot.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/copilot}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.copilot` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "copilot": {
      "project_name": "copilot",
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
    "copilot": {
      "project_name": "copilot",
      "target": "atatus",
      "api_key": "<key>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.copilot.endpoint`.

### Activate Copilot hooks

Copilot hooks are registered in a single `.github/hooks/hooks.json` file. Create it (or merge Atatus entries into it if it already exists):

```json
{
  "hooks": {
    "SessionStart":      [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-session-start"}],
    "UserPromptSubmit":  [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-user-prompt"}],
    "PreToolUse":        [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-pre-tool"}],
    "PostToolUse":       [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-post-tool"}],
    "Stop":              [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-stop"}],
    "SubagentStop":      [{"type": "command", "command": "~/.atatus/harness/venv/bin/atatus-hook-copilot-subagent-stop"}]
  }
}
```

All `command` values should be absolute paths to the venv binary (e.g. `~/.atatus/harness/venv/bin/atatus-hook-copilot-<event>`).

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials.
2. **Atatus** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `.github/hooks/hooks.json` exists in the project root and each `command` path is the absolute venv binary path.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"hookEventName":"PreToolUse","tool_name":"test"}' | ATATUS_DRY_RUN=true atatus-hook-copilot-pre-tool
   ```

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- Copilot hooks activated via `.github/hooks/hooks.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Copilot session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching Copilot)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/copilot.log`; set `ATATUS_VERBOSE=true` in the shell before launching VS Code / Copilot CLI to also capture routine hook activity

## Hook Events

Copilot fires 6 hook events. Each event maps to a span:

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `SessionStart` | `Session Start` | CHAIN | Session initialization |
| `UserPromptSubmit` | `User Prompt` | CHAIN | User prompt text |
| `PreToolUse` | `Tool: {name}` | TOOL | Tool start; **must print permission response to stdout** |
| `PostToolUse` | `Tool: {name}` | TOOL | Tool result |
| `Stop` | `Agent Stop` | LLM | Per-turn completion; transcript at `~/.copilot/session-state/<session_id>/events.jsonl` is parsed for model name, prompt, and tool-call count |
| `SubagentStop` | `Subagent: {id}` | CHAIN | Subagent completion |

### PreToolUse permission response

The pre-tool handler must print a permission response to stdout:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
```

All other handlers print `{"continue": true}`.

## Troubleshoot

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check hook log: `tail -20 ~/.atatus/harness/logs/copilot.log` |
| Hooks not firing | Verify `.github/hooks/hooks.json` exists in the project root and each `command` path is the absolute venv binary path |
| `PreToolUse` blocking tools | Check the handler prints the correct permission JSON. Test: `echo '{"hookEventName":"PreToolUse","tool_name":"test"}' \| atatus-hook-copilot-pre-tool` |
| Config missing | Run the installer or create `~/.atatus/harness/config.json` manually (include `harnesses.copilot` section) |
| Collector unreachable | Check connectivity: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching Copilot |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching Copilot |
| Wrong project name | Set `harnesses.copilot.project_name` in `~/.atatus/harness/config.json` (default: `"copilot"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching Copilot |
