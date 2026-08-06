# Atatus Coding Harness Tracing

Trace AI coding sessions to [Atatus](https://atatus.com) with OpenInference spans. Each harness integration emits spans for prompts, tool calls, model responses, and session lifecycle events.

## Supported Harnesses

| Harness Integration | Install command | Name |
|---------------------|-----------------|------|
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [macOS / Linux](tracing/claude_code/README.md#macos--linux) · [Windows](tracing/claude_code/README.md#windows-powershell) | `claude` |
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [Claude Plugin](tracing/claude_code/README.md#claude-code-marketplace) | `claude-code-tracing` |
| [OpenAI Codex CLI](tracing/codex/README.md) | [macOS / Linux](tracing/codex/README.md#macos--linux) · [Windows](tracing/codex/README.md#windows-powershell) | `codex` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | [macOS / Linux](tracing/cursor/README.md#macos--linux) · [Windows](tracing/cursor/README.md#windows-powershell) | `cursor` |
| [GitHub Copilot (VS Code + CLI)](tracing/copilot/README.md) | [macOS / Linux](tracing/copilot/README.md#macos--linux) · [Windows](tracing/copilot/README.md#windows-powershell) | `copilot` |
| [Gemini CLI](tracing/gemini/README.md) | [macOS / Linux](tracing/gemini/README.md#macos--linux) · [Windows](tracing/gemini/README.md#windows-powershell) | `gemini` |
| [Kiro CLI](tracing/kiro/README.md) | [macOS / Linux](tracing/kiro/README.md#macos--linux) · [Windows](tracing/kiro/README.md#windows-powershell) | `kiro` |
| [Opencode CLI](tracing/opencode/README.md) | [macOS / Linux](tracing/opencode/README.md#macos--linux) · [Windows](tracing/opencode/README.md#windows-powershell) | `opencode` |
| [Oh My Pi (omp)](tracing/omp/README.md) | [macOS / Linux](tracing/omp/README.md#macos--linux) · [Windows](tracing/omp/README.md#windows-powershell) | `omp` |

> **Each install link opens the ready-to-paste command for your OS — copy it and run it in a terminal**

> Installing Claude Code tracing via the Claude marketplace? See [Claude Code Tracing](tracing/claude_code/README.md#claude-code-marketplace) for the marketplace-specific flow — backend credentials must be set directly in `~/.claude/settings.json` since the install wizard is skipped.

### Setup walkthrough

The installer involves a brief interactive setup. The steps below run in order:

#### 1. Credentials

- **Atatus license key** — required. Create one under **Settings → Account Settings → API Keys**, choosing the type **Ingest License Key**.
- **OTLP endpoint** — optional. Defaults to `https://otel-rx.atatus.com`; set it only if you have been given a different collector URL.

If you've already configured another harness, the installer offers a **copy-from** menu so you can reuse those credentials instead of re-entering them.

#### 2. Project name

The Atatus project that spans for this harness are grouped under. Defaults to the harness name (e.g. `claude-code`, `codex` etc).

#### 3. User ID (optional)

A free-form identifier attached to every span as `user.id`. Useful when multiple teammates report into the same project. Leave blank to skip.

#### 4. Content logging

Three Y/n opt-outs that apply to **all** harnesses:

- Log user prompts?
- Log what tools were asked to do (commands, file paths, URLs)?
- Log what tools returned (file contents, command output)?

You're only asked these the first time you install a harness — subsequent installs reuse the existing `logging:` block. You can edit them later in `~/.atatus/harness/config.json`.

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
| `ATATUS_LOG_TOOL_CONTENT` | `true` | Capture tool *output* — file bodies, shell stdout, search results. This is the broadest of the three: it is where file contents and anything pasted into a session end up. |
| `ATATUS_DISABLE_FORK` | `false` | Testing only. Stops the opencode handler forking a background process so hooks run synchronously. |
| `OTEL_RESOURCE_ATTRIBUTES` | — | Standard OTel attribute string (`team=payments,environment=prod`) added to every span. Overrides `config.json` `attributes`/`harnesses.<name>.attributes` on key collision; set per-harness by placing it in that harness's settings env block. |

**Backend overrides** (set if you want env to take priority over `config.json` for a single run):

| Variable | Description |
|----------|-------------|
| `ATATUS_API_KEY` | Atatus license key. Required — without it spans are dropped. |
| `ATATUS_OTLP_ENDPOINT` | Collector URL. Optional; defaults to `https://otel-rx.atatus.com`. |

> Claude Code plugin reads env vars from `~/.claude/settings.json` under the `env` block

## Links

- [Atatus](https://atatus.com)
- [OpenInference](https://github.com/Arize-ai/openinference)

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the contribution process, and the CLA.

## License

[Apache 2.0](LICENSE)
