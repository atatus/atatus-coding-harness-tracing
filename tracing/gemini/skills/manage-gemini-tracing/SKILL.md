---
name: manage-gemini-tracing
description: Set up and configure Atatus tracing for Gemini CLI sessions. Use when users want to set up tracing, configure Atatus for Gemini, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up gemini tracing", "configure Atatus for Gemini", "enable gemini tracing", "setup-gemini-tracing", or any request about connecting Gemini CLI to Atatus for observability.
---

# Setup Gemini Tracing

Configure OpenInference tracing for **Gemini CLI** sessions to Atatus. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.gemini/settings.json` for 8 hook entries with `name: atatus-tracing`
   - Check `~/.atatus/harness/config.json` for the `harnesses.gemini` block
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

0. **Whether to reuse an existing config**: if `harnesses.gemini` already has a
   `target`, show what is stored (license key as its last 4 characters only) and ask
   whether to keep it. If yes, change nothing and just re-register the hooks.
1. **Credentials** (only if reconfiguring, or if no existing config):
   - Atatus license key, and optionally a custom OTLP endpoint
     (leave blank unless you are running Atatus on-premise)
2. **Project name**: defaults to `"gemini"` on a fresh install, and to the stored name when one exists. Stored under `harnesses.gemini.project_name`. This becomes the OTLP `service.name`, so everyone who accepts the default shares one project — suggest a distinct name when that is not what they want
3. **User ID** (optional): Set `ATATUS_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.atatus/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.atatus/harness/{bin,run,logs,state/gemini}`

**Important: read-merge-write.** If `~/.atatus/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.gemini` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Atatus:**
```json
{
  "harnesses": {
    "gemini": {
      "project_name": "gemini",
      "target": "atatus",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.gemini.endpoint`.

### Activate Gemini hooks

Gemini CLI uses `~/.gemini/settings.json` for hook registration. Hooks are configured under `hooks.<EventName>` as an array of matcher/hook objects.

Install or reinstall via the installer:

```bash
./install.sh gemini
```

To uninstall:

```bash
./install.sh uninstall gemini
```

The installer registers all 8 hook events (`SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeTool`, `AfterTool`) in `~/.gemini/settings.json` with `name: atatus-tracing` on each entry.

### Validate

1. **Config exists**: Run `cat ~/.atatus/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.gemini`.
2. **Atatus** (if applicable): Run `curl -sf --connect-timeout 3 --max-time 5 <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `~/.gemini/settings.json` contains 8 hook entries with `name: atatus-tracing`.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"event":"BeforeModel"}' | ATATUS_DRY_RUN=true atatus-hook-gemini-before-model
   ```

### Confirm

Tell the user:
- Config saved to `~/.atatus/harness/config.json`
- Gemini CLI hooks activated via `~/.gemini/settings.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Gemini CLI session and traces will appear in the Atatus dashboard under the project name
- Mention `ATATUS_DRY_RUN=true` to test without sending data (set as env var before launching Gemini CLI)
- Mention `ATATUS_VERBOSE=true` for debug output
- Errors are always written to `~/.atatus/harness/logs/gemini.log`; set `ATATUS_VERBOSE=true` in the shell before launching Gemini CLI to also capture routine hook activity
- Toggle tracing on/off via `ATATUS_TRACE_ENABLED` env var (must be exported in the user's shell -- Gemini hooks read host env vars)
- Tail the log file at `~/.atatus/harness/logs/gemini.log` for real-time debugging

## Hook Events

Gemini CLI fires 8 hook events. Each event is registered in `~/.gemini/settings.json` and maps to a dedicated CLI entry point.

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `SessionStart` | Session Start | CHAIN | Session initialization |
| `SessionEnd` | Session End | CHAIN | Session termination |
| `BeforeAgent` | Agent Turn | CHAIN | User prompt to agent |
| `AfterAgent` | Agent Turn | CHAIN | Agent completion |
| `BeforeModel` | LLM Call | LLM | Model invocation start with prompt |
| `AfterModel` | LLM Call | LLM | Model response with tokens |
| `BeforeTool` | Tool: {name} | TOOL | Tool invocation start |
| `AfterTool` | Tool: {name} | TOOL | Tool result |

## Troubleshoot

Common issues and fixes for Gemini CLI:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.atatus/harness/config.json`. Check hook log: `tail -20 ~/.atatus/harness/logs/gemini.log` |
| Hooks not firing | Verify `~/.gemini/settings.json` contains the 8 hook entries with `name: atatus-tracing` for all events |
| Config missing | Run `./install.sh gemini` or create `~/.atatus/harness/config.json` manually (include `harnesses.gemini` section) |
| Collector unreachable | Check connectivity: `curl -sf --connect-timeout 3 --max-time 5 <endpoint>/v1/traces` |
| Want to test without sending | Set `ATATUS_DRY_RUN=true` env var before launching Gemini CLI |
| Want verbose logging | Set `ATATUS_VERBOSE=true` env var before launching Gemini CLI |
| Wrong project name | Set `harnesses.gemini.project_name` in `~/.atatus/harness/config.json` (default: `"gemini"`) |
| Spans missing user attribution | Set `ATATUS_USER_ID` env var before launching Gemini CLI |
| Tracing not toggling | Ensure `ATATUS_TRACE_ENABLED` is exported in your shell, not just set |
