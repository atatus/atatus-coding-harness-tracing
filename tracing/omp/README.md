# omp Tracing

Automatic OpenInference tracing for [Oh My Pi (omp)](https://github.com/can1357/oh-my-pi) terminal coding sessions. Spans are exported to [Atatus](https://atatus.com).

## What gets traced

One trace per **agent run** — one user prompt → the agent's internal turn/tool-use loop → its final answer. A single agent run usually contains several model calls (omp re-prompts the model after each round of tool calls), so one trace covers all of them. Each trace is a tree:

| Span | Kind | Notes |
|------|------|-------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt (from `before_agent_start`); `output.value` is the final assistant message's text. One per agent run. |
| `LLM: <model>` | LLM | Child of `Turn`. One per `turn_end` (one per model call in the loop). Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. |
| `<tool>` | TOOL | Child of `Turn`. One per `ToolResultMessage` in a `turn_end`, paired with its originating `ToolCall` by id. Records `tool.name`, redacted input args + output, and tool-specific attributes. Errors are recorded with span status. |

Token usage **is** captured — omp surfaces cumulative `usage` (input/output/reasoning tokens, cache read/write, and cost) inline on each assistant message, unlike vendors that withhold it from local surfaces.

Timestamps come from omp's own millisecond clocks where present (`AssistantMessage.timestamp` / `duration`, `turn_start.timestamp`, `ToolResultMessage.timestamp`), falling back to wall-clock time on the tracing process when absent.

## Architecture

omp loads its extensions **in-process** inside its Bun runtime ([hook docs](https://omp.sh/docs/hooks)) — there is no per-event subprocess and no host-spawned hook. Unlike opencode, omp exposes rich, **once-fired** lifecycle events that already carry final, structured data, so the integration is a straightforward stateful event-forward (no snapshot reconciliation, no dedup). It is split into two pieces:

1. **TypeScript hook shim** (`~/.omp/extensions/atatus-tracing.ts`). A dumb bridge. On a small whitelist of lifecycle events — `before_agent_start` (carries the prompt), `turn_end` (carries the completed `AssistantMessage` with inline token usage + model, plus that turn's `toolResults`), `agent_end` (run finished), and `session_shutdown` — it spawns `atatus-hook-omp` detached and pipes the event payload to stdin. The shim contains no tracing logic and never blocks omp's event loop.
2. **Python event handler** (`atatus-hook-omp`). A small state machine keyed by session id. It dispatches on `payload["type"]`, accumulates per-session state, and emits `Turn`/`LLM`/`TOOL` spans on receipt. Pairs each `ToolCall` with its `ToolResultMessage` by id to build TOOL spans with both input args and output.

Because omp's lifecycle events fire exactly once each and carry final data, there is no message-id or callID dedup — spans are emitted as events arrive.

## Setup

The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, copies the hook shim into `~/.omp/extensions/atatus-tracing.ts`, **and** registers the shim's absolute path in the `extensions` array of `~/.omp/agent/settings.json`. omp does **not** auto-discover an extensions directory ([extension loading docs](https://omp.sh/docs/hooks)) — explicit registration is required, and the installer handles it. Spans are sent directly to the backend from the handler — no separate buffer/collector service is required.

Pass `--with-skills` to also symlink the `manage-omp-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage omp tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- omp
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall omp
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat omp
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall omp
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd atatus-coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh omp
```

Uninstall:

```bash
./install.sh uninstall omp
```

**Windows (PowerShell)**

Install:

```powershell
install.bat omp
```

Uninstall:

```powershell
install.bat uninstall omp
```

Uninstall removes the shim's path from the `extensions` array in `~/.omp/agent/settings.json`, deletes the hook file at `~/.omp/extensions/atatus-tracing.ts` (only if it carries the Atatus header marker — your own extensions are left alone), and removes the `harnesses.omp` block from `~/.atatus/harness/config.json`.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `omp` |
| Project name | `omp` |
| Atatus endpoint | set by the installer |
| Hook file | `~/.omp/extensions/atatus-tracing.ts` |
| Registration | absolute path in `extensions` array of `~/.omp/agent/settings.json` |
| Lifecycle events forwarded | `before_agent_start`, `turn_end`, `agent_end`, `session_shutdown` |
| Span tree | `Turn` (CHAIN) → `LLM` / `TOOL` |
| Trace granularity | one trace per agent run |
| State directory | `~/.atatus/harness/state/omp/` |
| Log file | `~/.atatus/harness/logs/omp.log` |

## Verifying tracing

Run any omp session as you normally would. omp loads the registered extension on startup; the shim forwards lifecycle events to the Python handler.

- Errors and handler stderr land in `~/.atatus/harness/logs/omp.log` always (the adapter redirects Python stderr there via `ATATUS_LOG_FILE`); set `export ATATUS_VERBOSE=true` before launching omp to also see routine handler activity (event dispatch, span emits, state transitions).
- Confirm spans appear in your configured project in Atatus.
- Set `ATATUS_TRACE_DEBUG=true` to dump the raw event payloads under `~/.atatus/harness/state/debug/` (files are named `omp_before_agent_start_<ts>.json`, `omp_turn_end_<ts>.json`, `omp_agent_end_<ts>.json`, `omp_session_shutdown_<ts>.json`) for inspection.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, `ATATUS_PROJECT_NAME`, `ATATUS_VERBOSE`, `ATATUS_TRACE_DEBUG`, etc.).
