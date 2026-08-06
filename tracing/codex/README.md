# Codex CLI Tracing

Automatic OpenInference tracing for the OpenAI Codex CLI. Spans are exported to [Atatus](https://atatus.com).

## Setup
The installer prompts for your Atatus license key and project name, writes credentials to `~/.atatus/harness/config.json`, and registers the hook entries plus the `notify` token-usage backstop in `~/.codex/config.toml`. After installing, approve the hooks via Codex's `/hooks` command (one time per user account).

Pass `--with-skills` to also symlink the `manage-codex-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Codex tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.sh | bash -s -- codex
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.sh | bash -s -- uninstall codex
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat codex
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall codex
```

### Local setup

```bash
git clone https://github.com/atatus/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh codex
```

Uninstall:

```bash
./install.sh uninstall codex
```

**Windows (PowerShell)**

Install:

```powershell
install.bat codex
```

Uninstall:

```powershell
install.bat uninstall codex
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `codex` |
| Project name | `codex` |
| Atatus endpoint | `https://otel-rx.atatus.com` |
| Hook config file | `~/.codex/config.toml` |
| Hook events handled | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop` (via real Codex hooks); `agent-turn-complete` (via `notify`) for token usage |
| Env override file | `~/.codex/atatus-env.sh` |
| State directory | `~/.atatus/harness/state/codex/` (state files + tool span JSONLs) |
| Log file | `~/.atatus/harness/logs/codex.log` |

## Trust prompt

Codex requires explicit user trust for non-managed hooks before they fire. After install, run:

1. `codex` (start a session)
2. Type `/hooks` and approve each `atatus-hook-codex-*` entry.

Without this one-time approval, hooks won't fire and traces will be limited to the `notify`-based fallback (single LLM span per turn, no tool spans).

## Verifying tracing

Run any Codex command:

```bash
codex exec "explain what this file does" path/to/file.py
```

Then check:

- Hook activity in `~/.atatus/harness/logs/codex.log`.
- Per-thread state files in `~/.atatus/harness/state/codex/` show recent activity.
- Spans appear in your configured project in Atatus.

Errors are always logged. For routine hook activity, add `export ATATUS_VERBOSE=true` to `~/.codex/atatus-env.sh` (or your shell) and re-run. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_TRACE_DEBUG`, etc.).

## Troubleshooting

**Hooks not firing.** Run `codex` → `/hooks` and confirm the `atatus-hook-codex-*` entries are listed and trusted. If they aren't listed at all, re-run the installer.

**No spans appear.** Re-source your shell profile (or open a new terminal) so `~/.codex/atatus-env.sh` is loaded. Check `~/.atatus/harness/logs/codex.log` for backend/auth errors. Confirm the hooks are trusted via `/hooks`.

**Disable temporarily.** Untrust the entries via `codex` → `/hooks`, or set `ATATUS_TRACE_ENABLED=false` in `~/.codex/atatus-env.sh` and restart Codex. Full uninstall: `./install.sh uninstall codex`.
