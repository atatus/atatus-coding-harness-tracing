"""Tests for tracing.copilot.hooks.transcript.parse_transcript.

The parser reads the real shapes Copilot writes to events.jsonl:
``session.start``, ``session.model_change``, ``user.message``,
``assistant.message`` and ``tool.execution_start``. Everything after the last
``user.message`` is the "latest turn" — the one whose Stop hook is firing.
"""

from __future__ import annotations

import json

from tracing.copilot.hooks.transcript import parse_transcript


def _write_jsonl(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


class TestParseTranscriptHappyPath:
    def test_extracts_model_from_assistant_message(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.start", "data": {"copilotVersion": "1.0.40"}},
                {"type": "user.message", "data": {"content": "hi"}},
                {"type": "assistant.message", "data": {"model": "gpt-5-mini", "content": "hello"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"
        assert s["copilot_version"] == "1.0.40"

    def test_model_change_is_only_a_fallback(self, tmp_path):
        """With no assistant.message, the selected model is the best we have."""
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.start", "data": {"copilotVersion": "1.0.40"}},
                {"type": "session.model_change", "data": {"newModel": "gpt-5-mini"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"
        assert s["copilot_version"] == "1.0.40"

    def test_assistant_message_model_beats_model_change(self, tmp_path):
        """`newModel` is the user's selection; only assistant.message names the served model."""
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-4"}},
                {"type": "assistant.message", "data": {"model": "claude-sonnet-4.5", "content": "hey"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "claude-sonnet-4.5"

    def test_model_change_carries_the_reasoning_effort(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [{"type": "session.model_change", "data": {"newModel": "gpt-5.6-luna", "reasoningEffort": "high"}}],
        )
        assert parse_transcript(f)["effort"] == "high"

    def test_the_latest_model_change_wins(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-5.6-luna", "reasoningEffort": "low"}},
                {"type": "session.model_change", "data": {"newModel": "gpt-5.6-luna", "reasoningEffort": "xhigh"}},
            ],
        )
        assert parse_transcript(f)["effort"] == "xhigh"

    def test_a_resumed_session_reports_its_effort(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.resume", "data": {"reasoningEffort": "medium"}}])
        assert parse_transcript(f)["effort"] == "medium"

    def test_auto_model_reports_no_effort(self, tmp_path):
        """With the model on auto, Copilot writes a null effort, not a level."""
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.model_change", "data": {"newModel": "auto", "reasoningEffort": None}}])
        assert parse_transcript(f)["effort"] == ""

    def test_extracts_user_prompt_from_user_message(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "user.message", "data": {"content": "do the thing"}}])
        s = parse_transcript(f)
        assert s["input_text"] == "do the thing"

    def test_extracts_answer_and_output_tokens_from_assistant_message(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "why?"}},
                {"type": "assistant.message", "data": {"model": "gpt-5", "content": "because", "outputTokens": 42}},
            ],
        )
        s = parse_transcript(f)
        assert s["output_text"] == "because"
        assert s["output_tokens"] == 42

    def test_output_tokens_sum_across_the_turn(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "go"}},
                {"type": "assistant.message", "data": {"content": "part one", "outputTokens": 10}},
                {"type": "assistant.message", "data": {"content": "part two", "outputTokens": 5}},
            ],
        )
        s = parse_transcript(f)
        assert s["output_tokens"] == 15
        assert s["output_text"] == "part two"

    def test_counts_tool_execution_start_events(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "run stuff"}},
                {"type": "tool.execution_start", "data": {"toolName": "bash"}},
                {"type": "tool.execution_start", "data": {"toolName": "read"}},
                {"type": "tool.execution_end", "data": {"toolName": "read"}},
            ],
        )
        s = parse_transcript(f)
        assert s["tool_count"] == 2


class TestParseTranscriptTurnScoping:
    """Only the turn that just ended is summarised — a new prompt resets it."""

    def test_new_user_message_resets_turn_scoped_fields(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "first"}},
                {"type": "assistant.message", "data": {"content": "old answer", "outputTokens": 99}},
                {"type": "tool.execution_start", "data": {"toolName": "bash"}},
                {"type": "user.message", "data": {"content": "second"}},
                {"type": "assistant.message", "data": {"content": "new answer", "outputTokens": 7}},
            ],
        )
        s = parse_transcript(f)
        assert s["input_text"] == "second"
        assert s["output_text"] == "new answer"
        assert s["output_tokens"] == 7
        assert s["tool_count"] == 0

    def test_model_survives_the_turn_reset(self, tmp_path):
        """The model is session-scoped, so an earlier turn's model still counts."""
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "first"}},
                {"type": "assistant.message", "data": {"model": "gpt-5-mini", "content": "answer"}},
                {"type": "user.message", "data": {"content": "second"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"
        assert s["output_text"] == ""


class TestParseTranscriptModelPlaceholders:
    """`auto`/`default`/`""` are selector values, not models."""

    def test_auto_selection_is_not_a_model(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.model_change", "data": {"newModel": "auto"}}])
        s = parse_transcript(f)
        assert s["model_name"] == ""

    def test_default_selection_is_not_a_model(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.model_change", "data": {"newModel": "default"}}])
        s = parse_transcript(f)
        assert s["model_name"] == ""

    def test_placeholder_does_not_clobber_a_real_model(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-5-mini"}},
                {"type": "session.model_change", "data": {"newModel": "auto"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"

    def test_placeholder_on_assistant_message_ignored(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-5-mini"}},
                {"type": "assistant.message", "data": {"model": "auto", "content": "hi"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"


class TestParseTranscriptDefensive:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert parse_transcript(tmp_path / "nope.jsonl") == {}

    def test_blank_file_returns_zeroed_summary(self, tmp_path):
        f = tmp_path / "events.jsonl"
        f.write_text("", encoding="utf-8")
        s = parse_transcript(f)
        assert s["events_seen"] == 0
        assert s["model_name"] == ""
        assert s["output_text"] == ""
        assert s["output_tokens"] == 0

    def test_malformed_lines_are_skipped(self, tmp_path):
        f = tmp_path / "events.jsonl"
        f.write_text(
            "not json\n"
            + json.dumps({"type": "assistant.message", "data": {"model": "gpt-5", "content": "hi"}})
            + "\nalso not json\n",
            encoding="utf-8",
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5"
        assert s["events_seen"] == 1

    def test_unknown_event_kinds_do_not_crash(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "something.exotic", "data": {"x": 1}}])
        s = parse_transcript(f)
        assert s["events_seen"] == 1

    def test_null_data_field_does_not_crash(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.start", "data": None}])
        s = parse_transcript(f)
        assert s["events_seen"] == 1
        assert s["copilot_version"] == ""

    def test_missing_type_field_does_not_crash(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"data": {"something": "here"}}])
        s = parse_transcript(f)
        assert s["events_seen"] == 1

    def test_non_int_output_tokens_ignored(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "assistant.message", "data": {"content": "hi", "outputTokens": "lots"}}])
        s = parse_transcript(f)
        assert s["output_tokens"] == 0


class TestParseTranscriptOverwriteSemantics:
    def test_last_model_change_wins(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-4"}},
                {"type": "session.model_change", "data": {"newModel": "gpt-5-mini"}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-5-mini"

    def test_last_user_prompt_wins(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "first prompt"}},
                {"type": "user.message", "data": {"content": "second prompt"}},
            ],
        )
        s = parse_transcript(f)
        assert s["input_text"] == "second prompt"

    def test_empty_model_name_does_not_overwrite(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "session.model_change", "data": {"newModel": "gpt-4"}},
                {"type": "session.model_change", "data": {"newModel": ""}},
            ],
        )
        s = parse_transcript(f)
        assert s["model_name"] == "gpt-4"

    def test_empty_prompt_does_not_overwrite(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user.message", "data": {"content": "real prompt"}},
                {"type": "user.message", "data": {"content": ""}},
            ],
        )
        s = parse_transcript(f)
        assert s["input_text"] == "real prompt"


class TestParseTranscriptReturnShape:
    def test_all_expected_keys_present(self, tmp_path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [{"type": "session.start", "data": {}}])
        s = parse_transcript(f)
        assert set(s.keys()) == {
            "model_name",
            "effort",
            "copilot_version",
            "input_text",
            "output_text",
            "output_tokens",
            "tool_count",
            "events_seen",
        }

    def test_hook_start_events_are_not_parsed(self, tmp_path):
        """`hook.start` is our own hook firing, not session content."""
        f = tmp_path / "events.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "hook.start", "data": {"hookType": "userPromptSubmitted", "input": {"prompt": "hello"}}},
                {"type": "hook.start", "data": {"hookType": "preToolUse", "input": {}}},
            ],
        )
        s = parse_transcript(f)
        assert s["events_seen"] == 2
        assert s["input_text"] == ""
        assert s["tool_count"] == 0

    def test_missing_file_returns_truly_empty_dict(self, tmp_path):
        s = parse_transcript(tmp_path / "nonexistent.jsonl")
        assert s == {}
        assert len(s) == 0
