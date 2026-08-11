"""Tests that a deleted memory stops being replayed into later prompts.

Purging the record is not enough on its own: the forget request and the reply
confirming it both spell out the value that was removed, and replaying them let
the model read the deleted fact off the transcript and repeat it back — the
"I no longer remember that you use a Garmin watch" answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.services.chat import _FORGOTTEN_TURN_PLACEHOLDER, NeoChatService


@dataclass
class _Message:
    role: str
    content: str
    metadata_json: str | None = None


def _forgot(count: int = 1) -> str:
    return json.dumps({"memory_extraction": {"status": "completed", "forgotten_memories": count}})


def _saved(count: int = 2) -> str:
    return json.dumps(
        {"memory_extraction": {"status": "completed", "saved_durable_memories": count}}
    )


class TestHistoryRedaction:
    def test_a_forget_exchange_is_blanked_on_both_sides(self) -> None:
        messages = [
            _Message("user", "Remember I use a Garmin watch."),
            _Message("assistant", "Noted.", _saved()),
            _Message("user", "Forget that I use a Garmin watch."),
            _Message("assistant", "I removed your Garmin watch.", _forgot()),
        ]

        turns = NeoChatService._history_turns(messages)
        contents = [turn.content for turn in turns]

        assert contents[2] == _FORGOTTEN_TURN_PLACEHOLDER
        assert contents[3] == _FORGOTTEN_TURN_PLACEHOLDER
        assert "Garmin" not in " ".join(contents[2:])

    def test_turns_that_only_saved_are_left_intact(self) -> None:
        messages = [
            _Message("user", "Remember I use a Garmin watch."),
            _Message("assistant", "Noted.", _saved()),
        ]

        contents = [turn.content for turn in NeoChatService._history_turns(messages)]
        assert contents == ["Remember I use a Garmin watch.", "Noted."]

    def test_an_unrelated_turn_before_the_forget_survives(self) -> None:
        messages = [
            _Message("user", "I want to improve my 5K running."),
            _Message("assistant", "Noted.", _saved()),
            _Message("user", "Forget my Garmin watch."),
            _Message("assistant", "Removed.", _forgot()),
        ]

        contents = [turn.content for turn in NeoChatService._history_turns(messages)]
        assert contents[0] == "I want to improve my 5K running."
        assert contents[1] == "Noted."

    def test_zero_forgotten_memories_is_not_a_forget_turn(self) -> None:
        messages = [
            _Message("user", "Forget something I never said."),
            _Message("assistant", "Nothing matched.", _forgot(0)),
        ]

        contents = [turn.content for turn in NeoChatService._history_turns(messages)]
        assert _FORGOTTEN_TURN_PLACEHOLDER not in contents

    def test_malformed_metadata_is_ignored(self) -> None:
        messages = [
            _Message("user", "Hello"),
            _Message("assistant", "Hi", "not json"),
        ]

        contents = [turn.content for turn in NeoChatService._history_turns(messages)]
        assert contents == ["Hello", "Hi"]

    def test_roles_are_preserved(self) -> None:
        messages = [
            _Message("user", "Forget my watch."),
            _Message("assistant", "Removed.", _forgot()),
        ]

        turns = NeoChatService._history_turns(messages)
        assert [turn.role for turn in turns] == ["user", "assistant"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
