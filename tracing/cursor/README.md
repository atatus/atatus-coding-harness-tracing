# Cursor IDE Tracing

Automatic OpenInference tracing for the Cursor IDE and Cursor CLI. Spans are exported to [Atatus](https://atatus.com).

## Plugin install

Cursor 2.5+ can install this from the Cursor marketplace instead of running `install.sh`. The plugin
auto-registers every hook event and lazily bootstraps a dedicated Python venv on first hook fire, in
`~/.atatus/harness/cursor-plugin-venv` — deliberately separate from the `install.sh`-managed
`~/.atatus/harness/venv`, so the two cannot fight over pip file ownership.

```text
/add-plugin atatus/atatus-coding-harness-tracing
```

(Or point at your own team or private marketplace mirroring this repo.)

**Credentials.** The plugin route skips the interactive wizard, so configure the backend one of two
ways:

- **Recommended:** run the bundled `manage-cursor-tracing` skill once from any agent session — it
  writes `~/.atatus/harness/config.json` for you.
- **Or** export `ATATUS_API_KEY` (and `ATATUS_OTLP_ENDPOINT` if you are running Atatus on-premise)
  in the environment Cursor launches from. On macOS a GUI-launched Cursor may not inherit
  exports from your shell profile, so the config.json route is the more reliable of the two.

If no backend is configured the hooks fail open — they no-op and never block Cursor.

## Setup
The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, and registers the hooks in `~/.cursor/hooks.json`.

Pass `--with-skills` to also symlink the `manage-cursor-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Cursor tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- cursor
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall cursor
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat cursor
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall cursor
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd atatus-coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh cursor
```

Uninstall:

```bash
./install.sh uninstall cursor
```

**Windows (PowerShell)**

Install:

```powershell
install.bat cursor
```

Uninstall:

```powershell
install.bat uninstall cursor
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `cursor` |
| Project name | `cursor` |
| Atatus endpoint | set by the installer |
| Hook config file | `~/.cursor/hooks.json` |
| Hook events registered | `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `afterAgentResponse`, `afterAgentThought`, `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`, `beforeTabFileRead`, `afterTabFileEdit`, `postToolUse`, `stop` |
| Events emitted by Cursor CLI | `sessionStart`, `sessionEnd`, `beforeShellExecution`, `afterShellExecution`, `afterFileEdit`, `postToolUse`, `stop` (subset of the above; remaining events are IDE-only) |
| State directory | `~/.atatus/harness/state/cursor/` |
| Log file | `~/.atatus/harness/logs/cursor.log` |

## Trace topology

Each user turn creates one `User Prompt` CHAIN root. The IDE and CLI both defer this root until `stop`.

Thinking, tool, response, and stop spans use the root trace ID and parent span ID. If Cursor changes a generation ID, the active conversation supplies the canonical IDs.

The root starts at `beforeSubmitPrompt` and ends at `stop`. A `sessionEnd` event or the next prompt closes a pending root once.

Cursor IDE suppresses the standalone `Session Start` trace because user turns provide the useful roots. Cursor CLI can omit `beforeSubmitPrompt` entirely. In that case, `sessionStart` supplies a fallback parent for CLI spans.

Duplicate or late-arriving `stop` events are rejected by a per-conversation, per-generation terminal marker. That marker list is durable and bounded (not a single "latest generation" value), so a stale `stop` for an old, already-closed generation is still recognized and rejected even after several newer turns have completed — it cannot resolve to, and close, the turn that is currently active.

Each turn has one `Agent Response` LLM span. Multiple response fragments remain in order within that span. Its inferred interval starts after the preceding observed turn activity and ends at the final response event. The `cursor.llm.usage.scope` and `cursor.llm.timing.scope` attributes both use `turn`.

Token totals from `stop` occur once on the turn LLM span. Session totals use only `cursor.session.token_count.*` attributes. They do not duplicate OpenInference LLM totals.

Shell spans use the numeric exit code for status. Zero maps to `OK`, and nonzero maps to `ERROR`. Missing or invalid codes map to `UNSET`. The `shell.exit_code` attribute remains a string. Cursor output does not determine status when Cursor omits an exit code.

Point events, such as thinking and stop, keep zero duration when Cursor provides no interval.

## Verifying tracing

Use Cursor (IDE or `agent` CLI) as normal. The hooks are registered per user in `~/.cursor/hooks.json`, so they fire on agent activity in any workspace.

- Errors land in `~/.atatus/harness/logs/cursor.log` always; set `export ATATUS_VERBOSE=true` before launching Cursor to also see routine hook activity.
- Confirm spans appear in your configured project in Atatus.
- IDE-only events (e.g. `beforeReadFile`, `beforeMCPExecution`, `afterAgentResponse`) only fire when running through the Cursor IDE; the CLI emits the subset listed in **Events emitted by Cursor CLI** above.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, etc.).
