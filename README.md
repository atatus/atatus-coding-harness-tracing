# Atatus Coding Harness Tracing

Trace AI coding sessions to [Atatus](https://atatus.com) with OpenInference spans. Each harness integration emits spans for prompts, tool calls, model responses, and session lifecycle events.

📖 **Full documentation:** [LLM Monitoring for coding agents](https://docs.atatus.com/docs/llm-monitoring/coding-agents/overview.html)

## Quick start

One command per harness. On macOS or Linux, replacing `claude` with the name from the table below:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- claude
```

On Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat claude
```

The installer asks for your Atatus license key and a project name, then registers the harness's hooks. Traces appear on the **LLM** page in Atatus, not under APM.

> **Privacy posture:** prompts and tool *details* are captured by default; tool *output* is **not**. See [Content logging](#4-content-logging).

## Supported Harnesses

| Harness Integration | Install command | Name |
|---------------------|-----------------|------|
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [macOS / Linux](tracing/claude_code/README.md#macos--linux) · [Windows](tracing/claude_code/README.md#windows-powershell) | `claude` |
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [Claude Plugin](tracing/claude_code/README.md#claude-code-marketplace) | `atatus-claude-code-tracing` |
| [OpenAI Codex CLI](tracing/codex/README.md) | [macOS / Linux](tracing/codex/README.md#macos--linux) · [Windows](tracing/codex/README.md#windows-powershell) | `codex` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | [macOS / Linux](tracing/cursor/README.md#macos--linux) · [Windows](tracing/cursor/README.md#windows-powershell) · [Cursor Plugin](tracing/cursor/README.md#plugin-install) | `cursor` |
| [GitHub Copilot (VS Code + CLI)](tracing/copilot/README.md) | [macOS / Linux](tracing/copilot/README.md#macos--linux) · [Windows](tracing/copilot/README.md#windows-powershell) | `copilot` |
| [Gemini CLI](tracing/gemini/README.md) | [macOS / Linux](tracing/gemini/README.md#macos--linux) · [Windows](tracing/gemini/README.md#windows-powershell) | `gemini` |
| [Kiro CLI](tracing/kiro/README.md) | [macOS / Linux](tracing/kiro/README.md#macos--linux) · [Windows](tracing/kiro/README.md#windows-powershell) | `kiro` |
| [Opencode CLI](tracing/opencode/README.md) | [macOS / Linux](tracing/opencode/README.md#macos--linux) · [Windows](tracing/opencode/README.md#windows-powershell) | `opencode` |
| [Oh My Pi (omp)](tracing/omp/README.md) | [macOS / Linux](tracing/omp/README.md#macos--linux) · [Windows](tracing/omp/README.md#windows-powershell) | `omp` |
| [Devin](tracing/devin/README.md) | [macOS / Linux](tracing/devin/README.md#macos--linux) · [Windows](tracing/devin/README.md#windows-powershell) | `devin` |
| [Google Antigravity](tracing/antigravity/README.md) | [macOS / Linux](tracing/antigravity/README.md#macos--linux) · [Windows](tracing/antigravity/README.md#windows-powershell) | `antigravity` |

> **Each install link opens the ready-to-paste command for your OS — copy it and run it in a terminal**

> Installing Claude Code tracing via the Claude marketplace? See [Claude Code Tracing](tracing/claude_code/README.md#claude-code-marketplace) for the marketplace-specific flow — backend credentials must be set directly in `~/.claude/settings.json` since the install wizard is skipped.

> ⚠️ **Do not install both the package and the Claude Code plugin for `claude`.** Both register hooks, both fire on every event, and every span is recorded **twice** — which doubles the token and cost figures you see. Pick one.

### Setup walkthrough

The installer involves a brief interactive setup. Every harness asks the same
questions in the same order, so the flow below is the whole of it.

#### 0. Already configured?

If this harness already has an entry in `~/.atatus/harness/config.json`, the
installer prints what is stored and offers to keep it:

```
[atatus] Existing 'codex' configuration found:
         Project name : ashif-codex
         License key  : ****...a91f
         User ID      : ashif

Use this existing configuration? [Y/n]:
```

Press Enter (or answer `y`) and the remaining steps are skipped entirely — the
hooks are re-registered and nothing in your config changes. Answer `n` to walk
the steps below with every stored value offered as the default, which is how you
rotate a license key, repoint the endpoint, or rename the project.

The license key is only ever shown as its last four characters.

#### 1. Project name

The Atatus project that spans for this harness are grouped under. On a fresh
install it defaults to the harness name (e.g. `claude-code`, `codex`); on a
re-configure it defaults to the name you chose last time.

This name becomes the OTLP `service.name`, and Atatus auto-creates one project
per distinct value — so if several people install the same harness and all
accept the default, their sessions land in one shared project. Give it a name of
your own if you want them separated.

#### 2. Credentials

- **Atatus license key** — required. Create one under **Settings → Account Settings → API Keys**, choosing the type **Ingest License Key**.
- **OTLP endpoint** — optional. Set it only if you are running Atatus on-premise.

If you've already configured *another* harness, the installer offers a **copy-from** menu so you can reuse those credentials instead of re-entering them. When this harness has its own stored credentials, both prompts default to keeping them — a blank line at each is what leaves them untouched.

#### 3. User ID (optional)

A free-form identifier attached to every span as `user.id`. Useful when multiple teammates report into the same project. Leave blank to skip; on a re-configure, blank keeps the stored value and `-` clears it.

#### 4. Content logging

Three questions that apply to **all** harnesses. Press Enter to accept each default:

| Question | Default |
|---|---|
| Log user prompts? | **yes** |
| Log what tools were asked to do (commands, file paths, URLs)? | **yes** |
| Log what tools returned (file contents, command output)? | **no** — opt in explicitly |

Tool *output* is off because it is the broadest capture surface: file bodies, shell stdout, and anything pasted into a session all arrive through it.

Only answers that differ from these defaults are written to `~/.atatus/harness/config.json`, so the defaults stay authoritative and can be changed by an upgrade. You're asked once, on your first harness install — later installs reuse the stored block, except after a change to the defaults themselves, when you'll be asked to re-confirm once. You can also edit the block by hand, or override per session with the `ATATUS_LOG_*` env vars below.

### Managing an install

All three take the same form as the install command, on either OS:

| Action | Command argument | Notes |
|---|---|---|
| Add another harness | `bash -s -- codex` | Offers to copy the license key from a harness you already set up. Each harness gets its own project name. |
| Update | `bash -s -- update` | Updates the package and re-registers every installed harness. On a terminal you are asked once per harness whether to keep its stored config; with no terminal it keeps them all. Running the installed copy (`~/.atatus/harness/install.sh update`) fetches the current installer and hands over to it first, so an old local copy never updates with stale steps. |
| Remove one harness | `bash -s -- uninstall codex` | Removes that harness's hooks. Everything else is left alone. |
| Remove everything | `bash -s -- uninstall` | Full wipe: venv, package and `~/.atatus/harness/config.json`. |

Pass `--with-skills` on install to also symlink that harness's `manage-*-tracing` skill into the current directory's `.agents/skills/`, so a coding agent in that workspace can help manage the configuration.

To stop sending traces without uninstalling anything, set `ATATUS_TRACE_ENABLED=false`.

#### Unattended installs

With `--non-interactive` (or no terminal at all, which is what `update` gets from
cron or CI) nothing is asked. A fresh install reads every value from the file
named by `ATATUS_ENV_FILE` and errors if a required one is missing. Over an
already-configured harness the stored values are reused, and only values named in
that file override them.

Ambient `ATATUS_*` variables are deliberately *not* consulted for a harness that
is already configured: every installed harness exports `ATATUS_API_KEY`,
`ATATUS_OTLP_ENDPOINT` and `ATATUS_PROJECT_NAME` into the sessions it spawns, so
honouring them would let an update run from inside a traced terminal silently
repoint a harness at whatever that session carried.

### Environment variables

Most settings live in `.atatus/harness/config.json`, but a small set of env vars affect runtime behavior on every harness. The installers wire most of these for you; set them yourself when you want to override behavior for a single session or debug locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `ATATUS_TRACE_ENABLED` | `true` | Master toggle. Set to `false` to disable hooks without uninstalling. |
| `ATATUS_VERBOSE` | `false` | Enables `[atatus] ...` log lines in `~/.atatus/harness/logs/<harness>.log`. Errors are always logged; verbose adds routine activity (hook fires, span emits, state transitions). |
| `ATATUS_DRY_RUN` | `false` | Build spans but skip the backend send. Useful for confirming hook wiring without writing data. |
| `ATATUS_USER_ID` | — | Attached to every span as `user.id`. Mirrors the `user_id` field in `config.json`; env wins if both are set. |
| `ATATUS_PROJECT_NAME` | per-harness | Overrides `harnesses.<name>.project_name` from `config.json` for a single session. |
| `ATATUS_LOG_FILE` | per-harness | Path the harness writes its log to. Adapters default to `~/.atatus/harness/logs/<harness>.log`. |
| `ATATUS_TRACE_DEBUG` | `false` | Dump raw hook payloads as JSON under `~/.atatus/harness/state/<harness>/debug/`. Codex hooks use this for span-tree inspection. |
| `ATATUS_TRANSCRIPT_WAIT_MS` | `300` | How long a Stop hook waits for the harness to flush its transcript before reading model and token counts. Those two live only in the transcript, and the hook can fire before the write lands — losing that race emits a span with no model and zero tokens. `0` disables the wait. |
| `ATATUS_LOG_PROMPTS` | `true` | Capture prompt and response text on spans. Set to `false` to emit spans with metadata only. |
| `ATATUS_LOG_TOOL_DETAILS` | `true` | Capture tool names and arguments (file paths, commands, queries). |
| `ATATUS_LOG_TOOL_CONTENT` | **`false`** | Capture tool *output* — file bodies, shell stdout, search results. Off by default: this is the broadest of the three, and it is where file contents and anything pasted into a session end up. Opt in explicitly. |
| `ATATUS_DISABLE_FORK` | `false` | Testing only. Hooks normally hand the OTLP POST to a detached background process so the harness is never kept waiting on it; this sends inline instead, which is what makes emitted spans observable to an in-process test. |
| `ATATUS_OTLP_TIMEOUT` | `5` | Socket timeout in seconds for the OTLP POST. |
| `ATATUS_EGRESS_FAILURE_THRESHOLD` | `2` | Consecutive transport failures against one collector URL before sends to it are skipped. A response of any status counts as reachable — only refused/timeout/DNS/TLS failures count. |
| `ATATUS_EGRESS_COOLDOWN` | `60` | Seconds to skip sends to an unreachable collector before trying it again. |
| `OTEL_RESOURCE_ATTRIBUTES` | — | Standard OTel attribute string (`team=payments,environment=prod`) added to every span. Overrides `config.json` `attributes`/`harnesses.<name>.attributes` on key collision; set per-harness by placing it in that harness's settings env block. |

**Backend overrides** (set if you want env to take priority over `config.json` for a single run):

| Variable | Description |
|----------|-------------|
| `ATATUS_API_KEY` | Atatus license key. Required — without it spans are dropped. |
| `ATATUS_OTLP_ENDPOINT` | Collector URL. Optional; set it only if you are running Atatus on-premise. |

> Claude Code plugin reads env vars from `~/.claude/settings.json` under the `env` block

## Links

- [LLM Monitoring documentation](https://docs.atatus.com/docs/llm-monitoring/coding-agents/overview.html) — installation, configuration, privacy and per-harness guides
- [Span kinds](https://docs.atatus.com/docs/llm-monitoring/reference/span-kinds.html) — how to read a trace
- [Cost and tokens](https://docs.atatus.com/docs/llm-monitoring/reference/cost-and-tokens.html) — how spend is calculated
- [Atatus](https://atatus.com)
- [OpenInference](https://github.com/Arize-ai/openinference)

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the contribution process.

## License

[Apache 2.0](LICENSE). Attribution for the original work this is derived from is in [NOTICE](NOTICE), as section 4 of the licence requires.
