# Claude Code Tracing

Automatic OpenInference tracing for the Claude Code CLI and the Claude Agent SDK. Spans are exported to [Atatus](https://atatus.com).

## Setup
The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, and registers the hooks in `~/.claude/settings.json`.

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

`ATATUS_OTLP_ENDPOINT` is optional. Set it only if you are running Atatus on-premise. Each `ATATUS_LOG_*` flag accepts `"true"` or `"false"`. The values above mirror the wizard's defaults; omitting a flag entirely gives you the same posture.

> **`ATATUS_LOG_TOOL_CONTENT` is `false` on purpose.** Tool output is where file bodies, shell stdout, and anything pasted into a session end up. Set it to `"true"` only if you intend to capture all of that.

Env values take precedence over `~/.atatus/harness/config.json`.

Install:

```bash
claude plugin marketplace add atatus/atatus-coding-harness-tracing
claude plugin install atatus-claude-code-tracing@atatus-coding-harness-tracing
```

Uninstall:

```bash
claude plugin uninstall atatus-claude-code-tracing@atatus-coding-harness-tracing
claude plugin marketplace remove atatus-coding-harness-tracing
```

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- claude
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall claude
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat claude
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall claude
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd atatus-coding-harness-tracing
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

## Trace shape

One trace per user turn. Tool spans hang off the model call that requested them, not off the
turn, so a trace reads as what the agent actually did:

```
Turn 3                       CHAIN   input = the user's prompt, output = the final response
├── LLM call 1: <model>      LLM     one per assistant message.id, with its own tokens
│   ├── Bash                 TOOL
│   └── Read                 TOOL
├── LLM call 2: <model>      LLM
│   └── Write                TOOL
├── Permission Request       CHAIN   zero-duration, parented to the turn
└── Notification: info       CHAIN
```

Reconstructed at `Stop` from the session transcript, because Claude Code exposes no hook at
model-request boundaries — the transcript is the only place those boundaries exist. Assistant
records are grouped by `message.id`: Claude Code v2 writes one response as several records
(thinking / text / each `tool_use` on its own line), and folding them back is what makes one
response one span instead of three to five.

Because tool spans come from the transcript rather than from `PostToolUse`, a tool the hook never
reported is still captured. On the reference session that took coverage from 19 of 21 tools to
21 of 21.

A transcript with no stable assistant UUIDs (Claude Code v1) cannot be resolved into model calls,
so the turn falls back to a single flat `LLM` span carrying the whole turn's tokens.

🔴 **Keep the ordinal in the model-call span name.** The UI span bucketer collapses three or more
adjacent same-name siblings and re-parents their children to depth 0, so naming every call
`LLM call` alone would render the trace as flat as it was before the nesting existed.

Token counts live on the model calls, never on the turn: summary queries sum those columns across
every span in a range, so a copy on the turn would count the same usage twice.
`llm.token_count.prompt` is cache-inclusive (`input + cache_read + cache_write`), and
`prompt_details.input` is sent explicitly rather than left to be derived by subtraction.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `claude-code` |
| Project name | `claude-code` |
| Atatus endpoint | set by the installer |
| Hook config file | `~/.claude/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `Notification`, `PermissionRequest`, `PermissionDenied`, `PreCompact`, `PostCompact`, `Elicitation`, `ElicitationResult` |
| State directory | `~/.atatus/harness/state/claude-code/` |
| Log file | `~/.atatus/harness/logs/claude-code.log` |

## Verifying tracing

Run any Claude Code session as you normally would (e.g. `claude` or `claude -p "hello"`). The installed hooks fire on every `SessionStart`, `UserPromptSubmit`, `PreToolUse`, etc.

- Errors and `ATATUS_VERBOSE=true` activity land in `~/.atatus/harness/logs/claude-code.log`. To see routine hook activity (`session_start fired`, `emitted LLM span`, etc.), add `"ATATUS_VERBOSE": "true"` under `env` in `~/.claude/settings.json` and re-run a session.
- Confirm spans appear in your configured project in Atatus.
- Set `"ATATUS_TRACE_ENABLED": "false"` under `env` to temporarily disable tracing without uninstalling, or `"ATATUS_DRY_RUN": "true"` to build spans without sending them.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides.
