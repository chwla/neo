"""Tier 5 — chat integration wiring (plan section CHT).

`memory_chat.py` is the seam between the memory layer and the chat endpoint. It
does two things: pick a recall mode from the user's prompt, and assemble the
recall and prompt objects a request needs.

Its most important property is stated in its own docstring — it fails *closed*.
Any configuration or binding error returns None rather than a half-wired
runtime, because a chat request that silently recalls from the wrong database is
worse than one that recalls nothing at all.
"""

from __future__ import annotations

from datetime import UTC as UTC_TZ

import pytest
from sqlalchemy.orm import Session

from app.services import memory_chat
from app.services.memory.prompt import RecallPromptOrchestrator
from app.services.memory.queries import RecallMode
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.settings import MemorySettings
from tests.memory.conftest import OWNER_ID, PROFILE_ID


def _runtime(session: Session, database_identity: str, **overrides):
    base = {
        "owner_id": OWNER_ID,
        "database_identity": database_identity,
        "profile_id": PROFILE_ID,
        "request_id": "request-1",
    }
    base.update(overrides)
    return memory_chat.build_chat_memory_runtime(session, **base)


class TestModeSelection:
    @pytest.mark.parametrize(
        "prompt",
        [
            "what do you remember about me?",
            "show me my saved memories",
            "show my saved memories",
            "summarise my current goals",
            "summarize my current preferences",
        ],
    )
    def test_a_broad_ask_selects_broad_mode(
        self, session: Session, database_identity: str, prompt: str
    ) -> None:
        """CHT-01 — "what do you remember" is a request to enumerate, not to match.

        Scoped-lexical recall would score these prompts against stored text and
        return whatever happened to share a word with "remember".
        """

        runtime = _runtime(session, database_identity)
        assert runtime is not None

        assert runtime.context_factory(prompt).mode is RecallMode.BROAD

    @pytest.mark.parametrize(
        "prompt",
        [
            "what's my name?",
            "help me plan a trip",
            "do you remember anything",
            "remind me what my goals are",
        ],
    )
    def test_a_specific_prompt_selects_scoped_lexical(
        self, session: Session, database_identity: str, prompt: str
    ) -> None:
        """CHT-02 — everything else is a normal turn with a relevance question.

        The expected mode is written out rather than recomputed from
        `_BROAD_MEMORY_QUERY`. Deriving it from the same regex the code consults
        would make the assertion circular: it would hold no matter what
        `context_for` did with the result.
        """

        runtime = _runtime(session, database_identity)
        assert runtime is not None

        assert runtime.context_factory(prompt).mode is RecallMode.SCOPED_LEXICAL

    def test_the_broad_phrase_wins_even_when_the_question_is_specific(
        self, session: Session, database_identity: str
    ) -> None:
        """Pinning a rough edge: the phrase is matched anywhere in the prompt.

        "what do you remember about the api" is a narrow question, but it
        contains the broad trigger phrase and so enumerates instead of matching.
        Recorded rather than changed — broad mode returns more than needed rather
        than less, so the failure is wasted budget, not a wrong answer.
        """

        runtime = _runtime(session, database_identity)
        assert runtime is not None

        mode = runtime.context_factory("what do you remember about the api").mode

        assert mode is RecallMode.BROAD

    def test_the_broad_pattern_is_anchored_to_whole_phrases(self) -> None:
        """A bare "remember" must not trigger enumeration of everything stored."""

        assert memory_chat._BROAD_MEMORY_QUERY.search("remember") is None
        assert memory_chat._BROAD_MEMORY_QUERY.search("please remember my name") is None

    def test_mode_selection_is_case_insensitive(
        self, session: Session, database_identity: str
    ) -> None:
        runtime = _runtime(session, database_identity)
        assert runtime is not None

        assert runtime.context_factory("WHAT DO YOU REMEMBER?").mode is RecallMode.BROAD


class TestContextFactory:
    def test_the_context_carries_its_request_identity(
        self, session: Session, database_identity: str
    ) -> None:
        """CHT-03 — every field recall checks its binding against."""

        runtime = _runtime(session, database_identity, session_id="session-9")
        assert runtime is not None

        context = runtime.context_factory("what's my name?")

        assert str(context.owner_id) == OWNER_ID
        assert context.database_identity == database_identity
        assert context.profile_id == PROFILE_ID
        assert context.request_id == "request-1"
        assert context.session_id == "session-9"
        assert context.current_time is not None
        assert context.current_time.tzinfo is not None

    def test_the_active_project_is_propagated(
        self, session: Session, database_identity: str
    ) -> None:
        """CHT-04 — project scope decides whether project memories are visible."""

        runtime = _runtime(session, database_identity, active_project_id="project-7")
        assert runtime is not None

        assert runtime.context_factory("what's my name?").active_project_id == "project-7"

    def test_no_active_project_leaves_the_scope_unset(
        self, session: Session, database_identity: str
    ) -> None:
        runtime = _runtime(session, database_identity)
        assert runtime is not None

        assert runtime.context_factory("what's my name?").active_project_id is None

    @pytest.mark.parametrize(
        ("flag", "value"),
        [("incognito", True), ("memory_enabled", False)],
    )
    def test_the_gating_flags_are_propagated(
        self, session: Session, database_identity: str, flag: str, value: bool
    ) -> None:
        """CHT-05 — these are what recall and the orchestrator gate on.

        The runtime is still built when memory is disabled for the *request*;
        the gate is carried in the context so recall reports a reason code rather
        than the caller having to remember to skip it.
        """

        runtime = _runtime(session, database_identity, **{flag: value})
        assert runtime is not None

        assert getattr(runtime.context_factory("what's my name?"), flag) is value

    def test_each_call_gets_a_fresh_timestamp(
        self, session: Session, database_identity: str, monkeypatch
    ) -> None:
        """`current_time` drives freshness scoring, so it must not be frozen at build.

        The clock is stubbed to return a different instant on each call, so the
        assertion distinguishes "read per call" from "read once at build time".
        Comparing two real `now()` readings with `>=` would pass either way — the
        two would simply be equal — which is why this is not written that way.
        """

        from datetime import datetime as real_datetime, timedelta

        base = real_datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC_TZ)
        ticks = iter(base + timedelta(minutes=index) for index in range(10))

        class StubClock:
            @staticmethod
            def now(_tz=None) -> real_datetime:
                return next(ticks)

        monkeypatch.setattr(memory_chat, "datetime", StubClock)

        runtime = _runtime(session, database_identity)
        assert runtime is not None

        first = runtime.context_factory("one").current_time
        second = runtime.context_factory("two").current_time

        assert first == base
        assert second == base + timedelta(minutes=1)

    def test_the_context_budget_comes_from_settings(
        self, session: Session, database_identity: str
    ) -> None:
        """CHT-06a — the recall budget is the configured one, not a local default."""

        flags = MemorySettings.from_settings(memory_chat.get_settings())
        runtime = _runtime(session, database_identity)
        assert runtime is not None

        context = runtime.context_factory("what's my name?")

        assert context.maximum_records == flags.recall_max_records
        assert context.maximum_characters == flags.recall_max_chars
        assert context.lexical_available == flags.lexical_recall_enabled


class TestRuntimeAssembly:
    def test_the_runtime_wires_recall_into_the_orchestrator(
        self, session: Session, database_identity: str
    ) -> None:
        """CHT-06b — one orchestrator, over a recall service, with a usage recorder.

        The plan also mentioned direct answers; those are wired in `chat.py`
        rather than here, so this asserts what this function actually assembles.
        """

        runtime = _runtime(session, database_identity)

        assert runtime is not None
        assert isinstance(runtime.orchestrator, RecallPromptOrchestrator)
        assert isinstance(runtime.orchestrator.recall_service, CanonicalRecallService)
        assert runtime.orchestrator.usage_recorder is not None

    def test_a_disabled_memory_setting_yields_no_runtime_at_all(
        self, session: Session, database_identity: str, monkeypatch
    ) -> None:
        """CHT-07 — corrected: it returns None rather than an inert runtime.

        The plan expected "a runtime that injects nothing". Returning None is the
        stronger form of the same intent: there is no object to accidentally call,
        so a caller that forgets to check cannot recall from a disabled store.
        """

        monkeypatch.setattr(
            memory_chat.MemorySettings,
            "from_settings",
            staticmethod(lambda _settings: MemorySettings(enabled=False)),
        )

        assert _runtime(session, database_identity) is None

    def test_a_binding_mismatch_fails_closed(
        self, session: Session
    ) -> None:
        """The docstring's promise: no legacy fallback, no half-wired runtime.

        A database identity that does not match the one the store is bound to is
        exactly the "recalling from the wrong profile" case, and it must produce
        nothing rather than a runtime pointed at the wrong records.
        """

        assert _runtime(session, "/tmp/some-other-profile.db") is None

    def test_a_foreign_owner_fails_closed(
        self, session: Session, database_identity: str, other_owner_id: str
    ) -> None:
        runtime = _runtime(session, database_identity, owner_id=other_owner_id)

        assert runtime is None

    def test_the_stable_policy_is_re_exported_for_the_chat_layer(self) -> None:
        """`chat.py` imports the policy from here, so the alias has to hold."""

        from app.services.memory.prompt import STABLE_MEMORY_POLICY

        assert memory_chat.STABLE_MEMORY_POLICY is STABLE_MEMORY_POLICY
        assert "STABLE_MEMORY_POLICY" in memory_chat.__all__
