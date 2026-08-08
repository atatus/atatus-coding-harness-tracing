# Contributing to Coding Harness Tracing

Thanks for your interest in contributing! This repo emits OpenTelemetry / OpenInference spans from AI coding-assistant harnesses (Claude Code, Codex, Copilot, Cursor, Gemini, Kiro) to [Atatus](https://atatus.com).

## What we welcome

We accept contributions in the following areas:

- **Bug fixes** — incorrect spans, broken installs, regressions.
- **Reliability improvements** — better error handling, more robust hooks, clearer logs.
- **Documentation** — clarifications, fixes, examples, and per-harness guides.
- **New harness integrations** — adding tracing support for an AI coding assistant that isn't already covered.

For larger features or behavior changes, please **open an issue first** so we can discuss the approach before code is written. Keep PRs focused and reasonably sized — small, well-scoped changes are much easier to review and merge.

## Issues first

Before starting non-trivial work:

1. Search [existing issues](https://github.com/atatus/coding-harness-tracing/issues) to see if it's already being tracked.
2. If not, open one using the issue templates (bug report, feature request, or new harness integration).
3. Wait for a quick maintainer ack on the approach for anything beyond a small fix.

## Development setup

Fork the repo and clone your fork:

```bash
git clone https://github.com/<your-username>/coding-harness-tracing.git
cd coding-harness-tracing
```

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras --dev
```

Install the git hooks:

```bash
uv run pre-commit install
```

## Running tests & lint

These commands match what CI runs in `.github/workflows/ci.yml`.

Run the test suite:

```bash
uv run pytest tests/ -m "not slow"
```

Run lint, formatting, and type checks:

```bash
uv run pre-commit run --all-files
```

Both must be green before you open a PR.

## Agentic code review (before opening a PR)

This repo ships an agentic code-review skill at [`.agents/skills/code-review/`](.agents/skills/code-review/). Run it on your changes and address its findings **before** opening a PR.

The skill:

- Reviews your diff for correctness and repo conventions (Python 3.9 floor, shared `core/` patterns, per-harness layout under `tracing/<harness>/`).
- Runs the test and lint gates above and surfaces any failures.
- Can post its findings directly as a PR review comment.

**How to invoke:** open the repo in an agent harness that supports skills (e.g. Claude Code, Cursor, Codex) and invoke the `code-review` skill on your branch.

## Opening a PR

When you're ready to open a pull request:

- Target the `main` branch.
- Use a **conventional-commit-style** PR title — e.g. `fix:`, `feat:`, `docs:`, `chore:`, `refactor:`.
- Reference the issue in the PR body with `resolves #<issue-number>` (the PR template prompts for this).
- Keep the change focused — avoid mixing unrelated edits in the same PR.
- Fill out the PR template, including the test plan.

## Code review expectations

A maintainer will review your PR. Respond to feedback by pushing updates to the same branch — the discussion stays in the PR thread. CI must pass before a maintainer can merge.

## Licensing of contributions

There is no separate agreement to sign. Contributions are accepted under the project's
[Apache-2.0 licence](LICENSE) — as §5 of that licence provides, anything you intentionally submit
for inclusion is licensed under those same terms, unless you state otherwise.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
