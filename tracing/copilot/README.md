# GitHub Copilot Tracing

Automatic OpenInference tracing for GitHub Copilot in VS Code. Spans are exported to [Atatus](https://atatus.com).

## Setup
The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, and registers Copilot Chat hooks at `.github/hooks/hooks.json`.

Pass `--with-skills` to also symlink the `manage-copilot-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Copilot tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- copilot
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall copilot
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat copilot
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall copilot
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd atatus-coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh copilot
```

Uninstall:

```bash
./install.sh uninstall copilot
```

**Windows (PowerShell)**

Install:

```powershell
install.bat copilot
```

Uninstall:

```powershell
install.bat uninstall copilot
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `copilot` |
| Project name | `copilot` |
| Atatus endpoint | set by the installer |
| Hook config file | `.github/hooks/hooks.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStart`, `SubagentStop`, `PermissionRequest` |
| State directory | `~/.atatus/harness/state/copilot/` |
| Log file | `~/.atatus/harness/logs/copilot.log` |

## Verifying tracing

Use GitHub Copilot Chat in VS Code (or the Copilot CLI) inside the workspace that contains `.github/hooks/hooks.json`. The hooks fire on `SessionStart`, `UserPromptSubmit`, tool invocations, and `Stop`.

- Errors land in `~/.atatus/harness/logs/copilot.log` always; set `export ATATUS_VERBOSE=true` before launching VS Code / Copilot CLI to also see routine hook activity.
- Confirm spans appear in your configured project in Atatus.
- The hooks file is per-workspace — repeat the install in each repo where you want Copilot tracing.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, etc.).
