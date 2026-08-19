# Antigravity CLI/IDE Tracing

Automatic OpenInference tracing for Google Antigravity CLI/IDE sessions. Spans are exported to [Atatus](https://atatus.com).

This harness is **transcript-driven**: Antigravity hooks are control-plane triggers that carry only pointers (`transcriptPath`, `conversationId`, `workspacePaths`). The real model and tool content lives in the per-turn `transcript_full.jsonl` written by the agent. The `Stop` hook parses that transcript and reconstructs spans — one trace per user turn.

## Setup

The installer prompts for your Atatus license key and project name, writes credentials to `~/.atatus/harness/config.json`, and registers hooks under the top-level `atatus-tracing` key in `~/.gemini/config/hooks.json` (Antigravity's global hooks file — distinct from Gemini's `~/.gemini/settings.json`).

Pass `--with-skills` to also symlink the `manage-antigravity-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Antigravity tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- antigravity
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.sh | bash -s -- uninstall antigravity
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat antigravity
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall antigravity
```

### Local setup

```bash
git clone https://github.com/atatus/atatus-coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh antigravity
```

Uninstall:

```bash
./install.sh uninstall antigravity
```

**Windows (PowerShell)**

Install:

```powershell
install.bat antigravity
```

Uninstall:

```powershell
install.bat uninstall antigravity
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `antigravity` |
| Project name | `antigravity` |
| Atatus endpoint | `https://otel-rx.atatus.com` |
| Hook config file | `~/.gemini/config/hooks.json` |
| Hook events registered | `PreInvocation`, `Stop` |
| State directory | `~/.atatus/harness/state/antigravity/` |
| Log file | `~/.atatus/harness/logs/antigravity.log` |

## Verifying tracing

Run any Antigravity CLI/IDE session as you normally would. The installed hooks fire on `PreInvocation` (before each model invocation) and `Stop` (after the user turn completes).

- Errors land in `~/.atatus/harness/logs/antigravity.log` always; set `export ATATUS_VERBOSE=true` before launching Antigravity to also see routine hook activity.
- Confirm spans appear in your configured project in Atatus.
- Each hook has a 30-second timeout — see `HOOK_TIMEOUT_SECONDS` in `constants.py` if you need to adjust.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, etc.).

## Trace shape

One trace per user turn. Tool spans hang off the model call that requested them, not off the turn, so a
trace reads as what the agent actually did:

```
Turn 3                LLM     input = the user's prompt, output = the final response
├── Model call 1      CHAIN   one per planner response
│   ├── run_command   TOOL
│   └── view_file     TOOL
└── Model call 2      CHAIN
    └── grep_search   TOOL
```

🔴 **The turn is the trace's only LLM-kind span, and it is the root.** Consumers count LLM-kind spans as
turns — the LLM Traces list is a span list filtered on that kind, not a group-by-trace query — so a second
LLM-kind span anywhere in the trace shows up as a second turn. Claude Code and Codex get this for free:
their hooks fire once per turn, so a turn *is* a single model span. Antigravity's transcript exposes every
model call, and those boundaries are kept as **CHAIN** steps precisely so the extra fidelity does not
inflate turn counts. If you ever change a step back to `LLM`, a twelve-call turn becomes twelve rows again.

A step span covers the model call *and* the tools it ran, so its children sit inside it. The model's own
response time is kept separately as `llm.latency_ms`, and the turn reports `llm.call_count`.

A planner response that issued tool calls but wrote no text is not an empty span: the calls are its output,
reported as `llm.output_messages[].message.tool_calls`. A planner response with no text, no reasoning and no
tool calls produces no span at all.

## Model identification

Antigravity names the model in the transcript only on the turn where the user *switched* models, and names
it as a display label rather than an id. Three sources are combined so every span carries one:

1. `conversations/<conversationId>.db` → `gen_metadata`, which records the id the request actually ran
   against (`claude-sonnet-4-6`). This is the only per-request source and the only one shaped like something
   a pricing table can match.
2. The transcript's settings-change block, carried forward across turns.
3. The CLI's `settings.json`, i.e. the currently selected model.

`llm.model_name` gets the id; the human label is kept alongside as `antigravity.model_label`. Both live on
the turn span only — repeating the model on every step would multiply a single turn's contribution to every
model-keyed count downstream. The store has
no public schema, so a value that does not look like a model id is discarded and a label-derived id is used
instead — a schema change degrades this to sources 2/3 rather than putting an arbitrary string in the model
field.

## Limitations

- **Token counts come from the conversation store, not the transcript.** Neither the hook payload nor the
  transcript carries usage; `conversations/<conversationId>.db` does, one `gen_metadata` row per model call.
  Rows are joined to calls **by ordinal** — the i-th row is the i-th planner response — which is the only
  link between the two. A call with no row contributes nothing rather than borrowing its neighbour's
  numbers, and a turn with no usable rows emits **no `llm.token_count.*` attributes at all**: a zero would
  read downstream as a turn that was priced and cost nothing.

  The counts live on the turn span only, alongside the model, because the summary queries sum those columns
  across every span in range. `prompt` is the whole prompt with `prompt_details.cache_read` as a subset of
  it, matching the convention the consumer reads. No cache-*write* field has been found, so that column
  stays 0.

  ⚠️ The store's protobuf has **no public schema**. The field layout is documented in `hooks/usage.py` and
  is guarded: an implausible value or an unreadable row degrades to "no usage" rather than to a wrong
  number. If Antigravity renumbers those fields, token counts silently stop — they do not go wrong.
- **Durations are second-granular.** Every timestamp the transcript offers — its own `created_at` and the
  `Created At:` / `Completed At:` lines inside tool results — is whole seconds, so a sub-second tool
  legitimately reports a zero duration.
- **A turn waiting on a background task is not emitted until it resumes and finishes.** Antigravity's `Stop`
  hook means "the agent yielded", and backgrounding a tool makes it yield — so a stop is not proof the turn
  ended. Emitting there would freeze the turn at the pause and lose everything after it, tokens included.
  A turn counts as waiting when it has more `RUNNING` tool records than `SYSTEM_MESSAGE` wake-ups; note the
  `RUNNING` record is never updated, so "has a running tool" is not the same question. A turn that is no
  longer the last one is emitted regardless, since it will never settle — so interrupting a wait with a new
  message emits the interrupted turn as it stands, rather than stranding it. The only losing sequence is
  backgrounding a task and then abandoning the session without typing again; that turn is never traced.
- **A tool result that never arrives** (the session ended, or the call was rejected) is still reported, with
  its arguments and no output, rather than dropped.
