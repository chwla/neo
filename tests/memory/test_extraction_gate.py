"""Tests for the gate that keeps extraction off turns with nothing to remember.

Extraction reads the supporting window as well as the current message, so
running it on a question re-asserted facts from earlier turns as new candidates.
Asking "what do you remember about my goals?" created three records.
"""

from __future__ import annotations

import pytest

from app.services.memory.policy import turn_may_contain_memory


class TestSkipsQuestions:
    @pytest.mark.parametrize(
        "prompt",
        [
            "What do you remember about my urban-sketching goals, tools, and advice "
            "preferences? Only use saved memory.",
            "What do you remember about my running goals?",
            "Tell me about my preferences",
            "Do I have any goals saved?",
            "Give me an urban sketching practice session for tomorrow.",
            "How do I improve my sketching?",
            "",
            "   ",
        ],
    )
    def test_no_extraction_for_turns_that_state_nothing(self, prompt: str) -> None:
        assert turn_may_contain_memory(prompt) is False


class TestRunsOnStatements:
    @pytest.mark.parametrize(
        "prompt",
        [
            "Remember this for future chats: I want to improve at urban sketching.",
            "Remember this too: For urban sketching, I currently use a fineliner pen.",
            "Forget that I use a fineliner pen and a pocket watercolor set.",
            "I want to improve at urban sketching.",
            "My name is Soham.",
            "For urban sketching, I currently use a pocket watercolor set.",
            "I no longer use a fineliner pen.",
            "Call me Soham.",
        ],
    )
    def test_extraction_runs_when_the_user_states_something(self, prompt: str) -> None:
        assert turn_may_contain_memory(prompt) is True

    def test_a_statement_after_a_question_still_runs(self) -> None:
        """A mixed turn must not be skipped because it opens with a question."""

        assert turn_may_contain_memory("What should I practice? I prefer 25-minute sessions.")

    def test_explicit_instruction_without_first_person_runs(self) -> None:
        assert turn_may_contain_memory("Remember: the studio closes at 6pm.")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
