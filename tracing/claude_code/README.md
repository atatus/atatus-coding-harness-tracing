# Claude Code Tracing

Automatic OpenInference tracing for the Claude Code CLI and the Claude Agent SDK. Spans are exported to [Atatus](https://atatus.com).

## Setup
The installer prompts for your Atatus license key and project name, writes credentials to `~/.atatus/harness/config.json`, and registers the hooks in `~/.claude/settings.json`.

Pass `--with-skills` to also symlink the `manage-claude-code-tracing` skill into the current directory's `.agents/skills/` so Claude can help you manage the configuration interactively.

### Claude Code marketplace

The marketplace flow registers the hooks but skips the interactive wizard, so backend credentials and content-logging preferences must be set directly in `~/.claude/settings.json` under `env`:

```json
{
  "env": {
    "ATATUS_PROJECT_NAME": "claude-code",
    "ATATUS_API_KEY": "<your-atatus-api-key>",
    "ATATUS_LOG_PROMPTS": "true",
    "ATATUS_LOG_TOOL_DETAILS": "true",
    "ATATUS_LOG_TOOL_CONTENT": "false"
  }
}
```

`ATATUS_OTLP_ENDPOINT` is optional and defaults to `https://otel-rx.atatus.com`. Each `ATATUS_LOG_*` flag accepts `"true"` or `"false"`. The values above mirror the wizard's defaults; omitting a flag entirely gives you the same posture.

> **`ATATUS_LOG_TOOL_CONTENT` is `false` on purpose.** Tool output is where file bodies, shell stdout, and anything pasted into a session end up. Set it to `"true"` only if you intend to capture all of that.

Env values take precedence over `~/.atatus/harness/config.json`.

Install:

```bash
claude plugin marketplace add atatus/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing
```

Uninstall:

```bash
claude plugin uninstall claude-code-tracing@coding-harness-tracing
claude plugin marketplace remove atatus/coding-harness-tracing
```

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.sh | bash -s -- claude
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.sh | bash -s -- uninstall claude
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat claude
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall claude
```

### Local setup

```bash
git clone https://github.com/atatus/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh claude
```

Uninstall:

```bash
./install.sh uninstall claude
```

**Windows (PowerShell)**

Install:

```powershell
install.bat claude
```

Uninstall:

```powershell
install.bat uninstall claude
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `claude-code` |
| Project name | `claude-code` |
| Atatus endpoint | `https://otel-rx.atatus.com` |
| Hook config file | `~/.claude/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `Notification`, `PermissionRequest`, `PermissionDenied`, `PreCompact`, `PostCompact` |
| State directory | `~/.atatus/harness/state/claude-code/` |
| Log file | `~/.atatus/harness/logs/claude-code.log` |

## Verifying tracing

Run any Claude Code session as you normally would (e.g. `claude` or `claude -p "hello"`). The installed hooks fire on every `SessionStart`, `UserPromptSubmit`, `PreToolUse`, etc.

- Errors and `ATATUS_VERBOSE=true` activity land in `~/.atatus/harness/logs/claude-code.log`. To see routine hook activity (`session_start fired`, `emitted LLM span`, etc.), add `"ATATUS_VERBOSE": "true"` under `env` in `~/.claude/settings.json` and re-run a session.
- Confirm spans appear in your configured project in Atatus.
- Set `"ATATUS_TRACE_ENABLED": "false"` under `env` to temporarily disable tracing without uninstalling, or `"ATATUS_DRY_RUN": "true"` to build spans without sending them.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides.
