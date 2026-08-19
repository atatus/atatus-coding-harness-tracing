# Gemini CLI Tracing

Automatic OpenInference tracing for Gemini CLI sessions. Spans are exported to [Atatus](https://atatus.com).

## Setup
The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, and registers the hooks in `~/.gemini/settings.json`.

Pass `--with-skills` to also symlink the `manage-gemini-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Gemini tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- gemini
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall gemini
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat gemini
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall gemini
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd atatus-coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh gemini
```

Uninstall:

```bash
./install.sh uninstall gemini
```

**Windows (PowerShell)**

Install:

```powershell
install.bat gemini
```

Uninstall:

```powershell
install.bat uninstall gemini
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `gemini` |
| Project name | `gemini` |
| Atatus endpoint | `https://otel-rx.atatus.com` |
| Hook config file | `~/.gemini/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeTool`, `AfterTool` |
| State directory | `~/.atatus/harness/state/gemini/` |
| Log file | `~/.atatus/harness/logs/gemini.log` |

## Verifying tracing

Run any Gemini CLI session as you normally would (e.g. `gemini` or `gemini -p "hello"`). The installed hooks fire on `SessionStart`, model and tool boundaries, and `SessionEnd`.

- Errors land in `~/.atatus/harness/logs/gemini.log` always; set `export ATATUS_VERBOSE=true` before launching Gemini to also see routine hook activity.
- Confirm spans appear in your configured project in Atatus.
- Each hook has a 30-second timeout (Gemini's default is 60s) — see `HOOK_TIMEOUT_MS` in `constants.py` if you need to adjust.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, etc.).
