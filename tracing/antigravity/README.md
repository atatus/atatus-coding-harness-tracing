# Antigravity CLI/IDE Tracing

Automatic OpenInference tracing for Google Antigravity CLI/IDE sessions. Spans are exported to [Atatus](https://atatus.com).

This harness is **transcript-driven**: Antigravity hooks are control-plane triggers that carry only pointers (`transcriptPath`, `conversationId`, `workspacePaths`). The real model and tool content lives in the per-turn `transcript_full.jsonl` written by the agent. The `Stop` hook parses that transcript and reconstructs spans — one trace per user turn.

## Setup

The installer prompts for a project name and your Atatus license key (and offers to keep what is already there when you re-run it), writes credentials to `~/.atatus/harness/config.json`, and registers hooks under the top-level `atatus-tracing` key in `~/.gemini/config/hooks.json` (Antigravity's global hooks file — distinct from Gemini's `~/.gemini/settings.json`).

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
cd atatus-coding-harness-tracing
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
| Atatus endpoint | set by the installer |
| Hook config file | `~/.gemini/config/hooks.json` |
| Hook events registered | `PreInvocation`, `PostInvocation`, `Stop` |
| State directory | `~/.atatus/harness/state/antigravity/` |
| Log file | `~/.atatus/harness/logs/antigravity.log` |

## Verifying tracing

Run any Antigravity CLI/IDE session as you normally would. The installed hooks fire on `PreInvocation` (before each model invocation), `PostInvocation` (right after), and `Stop` (after the user turn completes). `PreInvocation`/`PostInvocation` are both backstops that flush any earlier turn whose `Stop` was missed — `PostInvocation` doesn't change how span timestamps are derived (still the transcript's whole-second fields, see Limitations), it just gives a turn one more, earlier chance to be emitted.

- Errors land in `~/.atatus/harness/logs/antigravity.log` always; set `export ATATUS_VERBOSE=true` before launching Antigravity to also see routine hook activity.
- Confirm spans appear in your configured project in Atatus.
- Each hook has a 30-second timeout — see `HOOK_TIMEOUT_SECONDS` in `constants.py` if you need to adjust.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ATATUS_TRACE_ENABLED`, `ATATUS_DRY_RUN`, `ATATUS_USER_ID`, etc.).

## Trace shape

One trace per user turn. Tool spans hang off the model call that requested them, not off the turn, so a
trace reads as what the agent actually did:

```
Turn 3                     CHAIN   input = the user's prompt, output = the final response
├── LLM call 1: <model>    LLM     one per planner response, with its own tokens
│   ├── run_command        TOOL
│   └── view_file          TOOL
└── LLM call 2: <model>    LLM
    └── grep_search        TOOL
```

The root is CHAIN and every model call is its own LLM span carrying its own token counts, so cost is
attributable to the call that spent it rather than only to the turn. This matches the Claude Code harness
and Arize's whole fleet.

This shape was previously inverted — the turn was `LLM` and the steps were `CHAIN` — to hold "exactly one
LLM-kind span per trace", because the Traces list was a span list filtered on that kind rather than a
group-by-trace query, so a twelve-call turn rendered as twelve rows. That constraint is retired: the page
is trace-grained now. The history is in `ERRORS/error-found-during-arize-to-atatus-migration.md`.

🔴 **Keep the ordinal in the span name.** The UI span bucketer collapses three or more adjacent same-name
siblings and re-parents their children to depth 0, so naming every call `LLM call` alone would render the
trace as flat as it was before the nesting existed.

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
  A turn counts as waiting when some `RUNNING` tool record has no `SYSTEM_MESSAGE` wake-up **after** it.
  Two traps live in that sentence: the `RUNNING` record is never updated, so "has a running tool" is not the
  same question; and wake-ups must be matched to starts **in order**, never counted, because a turn commonly
  opens with an unrelated `SYSTEM_MESSAGE` that would otherwise cancel out a task started later. A turn that is no
  longer the last one is emitted regardless, since it will never settle — so interrupting a wait with a new
  message emits the interrupted turn as it stands, rather than stranding it. The only losing sequence is
  backgrounding a task and then abandoning the session without typing again; that turn is never traced.
- **A backgrounded tool gets its result from the wake-up, matched by task id.** The tool's own result record
  only says the task *started* and is never updated; output and exit code arrive later in a
  `SYSTEM_MESSAGE`. Up to five tasks run concurrently, so they are matched by the `task id` both records
  carry, never by position. ⚠️ Do not reuse that matching to decide whether the turn is still waiting — an
  outstanding task does not mean a waiting agent, and most outstanding tasks belong to turns that finished
  without waiting for them.
- **A tool result that never arrives** (the session ended, or the call was rejected) is still reported, with
  its arguments and no output, rather than dropped.
