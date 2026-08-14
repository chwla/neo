"""Tier 7 — the core end-to-end journeys, E2E-01..08.

Everything below this tier is pinned, which is the precondition for these being
useful: a red journey here has one plausible cause rather than five.

Each test is a sequence a real user performs — say something, come back later,
correct it, delete it — through the real extraction coordinator, the real store
and the real recall service. The only faked collaborator is the model, because a
local LLM is neither deterministic nor available in CI.

Two things that are easy to get wrong and silently produce a green suite:

**Extraction and recall must share one database.** The coordinator opens and
migrates its own from `execution_context.database_url`, which is *not* the
`engine` fixture. `_recall_for` builds a recall service against that same file
with the same binding identity.

**A scripted span must cite the message it arrived on.** `doubles.assertion`
defaults to `message_id="m1"`; a second turn using `m2` with a default span is
rejected as `source_message_not_user_authorized`. That rejection is invisible
from the recall side — the store simply has one fewer record than expected,
which reads exactly like correct de-duplication.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.repositories.memory import MemoryRepository
from app.services.memory.extraction_contracts import ExtractionMode, ExtractionRequest
from app.services.memory.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallQuery,
)
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.settings import MemorySettings
from tests.memory.conftest import FROZEN_NOW, OWNER_ID, PROFILE_ID
from tests.memory.doubles import assertion, model_output, retraction, scripted_model

SKETCHING = "I want to improve at urban sketching"
GOAL_VALUE = "improve at urban sketching"
IDENTITY = "My name is Soham"


def _request(message: str, *, message_id: str = "m1", **overrides) -> ExtractionRequest:
    fields = {
        "request_id": f"request-{message_id}",
        "owner_id": OWNER_ID,
        "conversation_id": "c1",
        "session_id": "s1",
        "message_id": message_id,
        "user_message": message,
        "mode": ExtractionMode.POST_TURN_AUTOMATIC,
        "source_content_hash": ExtractionRequest.content_hash(message),
    }
    fields.update(overrides)
    return ExtractionRequest(**fields)


@pytest.fixture
def profile_database(execution_context) -> str:
    """The path the coordinator migrates and writes to."""

    return execution_context.database_url.replace("sqlite:///", "", 1)


def _recall_for(path: str, execution_context, **flags) -> CanonicalRecallService:
    engine = create_engine(f"sqlite:///{path}", future=True)
    repository = MemoryRepository(
        Session(engine),
        owner_id=OWNER_ID,
        database_identity=execution_context.database_identity,
    )
    return CanonicalRecallService(repository, flags=MemorySettings(**flags))


def _context(execution_context, **overrides) -> MemoryQueryContext:
    base = {
        "owner_id": OWNER_ID,
        "database_identity": execution_context.database_identity,
        "profile_id": PROFILE_ID,
        "request_id": "recall-1",
        "current_time": FROZEN_NOW,
        "mode": RecallMode.SCOPED_LEXICAL,
    }
    base.update(overrides)
    return MemoryQueryContext(**base)


def _query(execution_context, text: str, **overrides) -> RecallQuery:
    return RecallQuery(context=_context(execution_context, **overrides), text=text)


def _goal_turn(message: str, value: str, *, message_id: str = "m1", **extra):
    """One accepted goal assertion, with its span citing the right message."""

    return model_output(
        assertions=[
            assertion(
                message,
                value,
                memory_type="goal",
                domain_hint="art",
                typed_value=value,
                message_id=message_id,
                **extra,
            )
        ]
    )


def _store_goal(coordinator, adapter_context) -> None:
    coordinator.process(_request(SKETCHING), adapter_context)


class TestStoreAndRecall:
    def test_a_stated_goal_is_recalled_on_a_later_turn(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-01 — the journey the whole system exists for.

        The user says something once; a later, differently-worded question gets
        it back. The recall query shares no whole word with the stored value
        beyond "sketching", so this is the lexical path doing real work.
        """

        coordinator = extraction_coordinator_factory(
            scripted_model({SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE)})
        )
        _store_goal(coordinator, adapter_context)

        result = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "sketching practice")
        )

        assert [item.memory.display_text for item in result.items] == [GOAL_VALUE]

    def test_restating_the_same_fact_creates_no_second_record(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-02 — the duplicate bug that grew the store on every re-read.

        The restatement is worded differently and arrives on its own message, so
        only the folded value identifies it as the same fact. Both turns are
        asserted to have been *applied*, because a rejected second turn would
        also leave one record and look identical from recall.
        """

        restated = "I still want to improve at urban sketching"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    restated: _goal_turn(restated, GOAL_VALUE, message_id="m2"),
                }
            )
        )

        first = coordinator.process(_request(SKETCHING), adapter_context)
        second = coordinator.process(
            _request(restated, message_id="m2"), replace(adapter_context, message_id="m2")
        )

        assert first.status.value == "applied"
        assert second.status.value == "applied"

        result = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        )
        assert len(result.items) == 1

    def test_asking_what_is_remembered_writes_nothing(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-12's regression, reachable from this half.

        Re-reading your own memory used to re-extract it and append a copy, so
        the store grew every time the user asked what was in it.
        """

        question = "what do you remember about me?"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    question: model_output(),
                }
            )
        )
        _store_goal(coordinator, adapter_context)
        coordinator.process(
            _request(question, message_id="m2"), replace(adapter_context, message_id="m2")
        )

        result = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        )

        assert len(result.items) == 1


class TestCorrectionAndDeletion:
    def test_a_named_correction_replaces_the_goal(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-03 — the user names what they are dropping, so it can be dropped.

        Handled deterministically: `_IMPLICIT_GOAL_CORRECTION` produces a linked
        retraction and assertion from one turn, with both halves grounded in the
        user's own words. No model is scripted for the second turn because none
        is consulted.
        """

        correction = (
            "I no longer want to improve at urban sketching. "
            "I want to improve at watercolour."
        )
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE, slot_hint="current_primary_goal")}
            )
        )
        _store_goal(coordinator, adapter_context)

        coordinator.process(
            _request(correction, message_id="m2", explicit_memory_intent=True),
            replace(adapter_context, message_id="m2"),
        )

        recall = _recall_for(profile_database, execution_context)
        remaining = [
            item.memory.display_text
            for item in recall.recall(_query(execution_context, "improve")).items
        ]

        assert GOAL_VALUE not in remaining

    def test_an_unnamed_correction_goes_to_review_rather_than_deleting(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-03b — the case that surprised me, and the behaviour is right.

        "Actually, now I want to improve at watercolour" implies the old goal is
        finished but never says so. A model proposing a retraction for it is
        proposing to delete a value the user did not write, and `ground_retraction`
        refuses — so the turn resolves to `unlinked_exclusive_slot_conflict` and
        goes to review with the occupant attached.

        The original goal is therefore still stored. That reads like a bug until
        you notice the alternative: deleting a goal on the strength of an
        inference. Review is the correct destination for an ambiguous delete.
        """

        correction = "Actually, now I want to improve at watercolour"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(
                        SKETCHING, GOAL_VALUE, slot_hint="current_primary_goal"
                    ),
                    correction: model_output(
                        assertions=[
                            assertion(
                                correction, "improve at watercolour", memory_type="goal",
                                domain_hint="art", typed_value="improve at watercolour",
                                slot_hint="current_primary_goal",
                                correction_group="g1", message_id="m2",
                            )
                        ],
                        retractions=[
                            retraction(
                                correction, "improve at watercolour",
                                old_value_hint=GOAL_VALUE,
                                correction_group="g1", message_id="m2",
                            )
                        ],
                    ),
                }
            )
        )
        _store_goal(coordinator, adapter_context)

        result = coordinator.process(
            _request(correction, message_id="m2"), replace(adapter_context, message_id="m2")
        )

        assert result.status.value == "needs_review"
        assert [decision.reason for decision in result.decisions] == [
            "unlinked_exclusive_slot_conflict"
        ]

        recall = _recall_for(profile_database, execution_context)
        remaining = [
            item.memory.display_text
            for item in recall.recall(_query(execution_context, "urban sketching")).items
        ]
        assert remaining == [GOAL_VALUE], "an unnamed correction must not delete anything"

    def test_a_forgotten_fact_stops_being_recalled(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-04 — the delete a user asks for out loud.

        Recall is asserted *before* the forget as well: without that, a journey
        where nothing was ever stored would report a successful deletion.
        """

        forget = "Forget that I want to improve at urban sketching"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    forget: model_output(
                        retractions=[
                            retraction(
                                forget, GOAL_VALUE, old_value_hint=GOAL_VALUE,
                                explicit_forget=True, message_id="m2",
                            )
                        ]
                    ),
                }
            )
        )
        _store_goal(coordinator, adapter_context)

        before = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        )
        assert before.items, "the fact must be recallable before it is forgotten"

        coordinator.process(
            _request(forget, message_id="m2", explicit_memory_intent=True),
            replace(adapter_context, message_id="m2"),
        )

        after = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        )
        assert after.items == ()


class TestScoping:
    def test_a_project_scoped_memory_is_invisible_outside_its_project(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-07 — project scope decides who can read a memory at all."""

        from tests.memory import factories

        coordinator = extraction_coordinator_factory(
            scripted_model({SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE)})
        )
        _store_goal(coordinator, adapter_context)

        engine = create_engine(f"sqlite:///{profile_database}", future=True)
        factories.insert_record(
            engine,
            display_text="ship the atlas release",
            scope_type="project",
            scope_project_id="project-7",
            slot_key="goal:global:independent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        )

        recall = _recall_for(profile_database, execution_context)
        outside = recall.recall(_query(execution_context, "atlas release"))
        inside = recall.recall(
            _query(execution_context, "atlas release", active_project_id="project-7")
        )

        assert outside.items == ()
        assert [item.memory.display_text for item in inside.items] == [
            "ship the atlas release"
        ]

    def test_a_domain_filter_excludes_another_domains_memory(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-06 — a rule set for one topic must not govern another.

        A user who wants terse video advice has not asked for terse answers about
        their health.
        """

        coordinator = extraction_coordinator_factory(
            scripted_model({SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE)})
        )
        _store_goal(coordinator, adapter_context)

        recall = _recall_for(profile_database, execution_context)
        # The stored domain is `global`, not the model's "art" hint: `_domain_for`
        # could not ground "art" in the message and fell back (decisions.md 35).
        # Using the hint here would have made this test pass for the wrong reason
        # -- an empty result from a domain that does not exist.
        own_domain = recall.recall(
            _query(execution_context, "urban sketching", allowed_domains=frozenset({"global"}))
        )
        other_domain = recall.recall(
            _query(execution_context, "urban sketching", allowed_domains=frozenset({"health"}))
        )

        assert own_domain.items, "the domain filter must not exclude its own domain"
        assert other_domain.items == ()
        # `domain_filtered_count` stays 0 here: `allowed_domains` is applied in the
        # SQL that fetches candidates, so an excluded record is never fetched and
        # never counted as a post-fetch drop. The pair of assertions above is the
        # real evidence -- a single empty-result assertion would also pass against
        # a store that held nothing.


class TestDirectIdentityAnswer:
    def test_an_identity_fact_is_answerable_without_a_model(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-08 — "what's my name" answered from the store, not from a guess.

        There is no model on either side of this journey: the deterministic
        preparser stores the name without one, and the direct-answer path returns
        it without one. The coordinator is built with `model=None` to make that
        explicit — if the preparser stopped handling names, this fails rather
        than quietly falling back.
        """

        from app.services.memory.direct_answer import DirectMemoryAnswerService
        from app.services.memory.prompt import RecallPromptOrchestrator

        coordinator = extraction_coordinator_factory(None)
        coordinator.process(
            _request(IDENTITY, explicit_memory_intent=True), adapter_context
        )

        recall = _recall_for(profile_database, execution_context)
        answers = DirectMemoryAnswerService(RecallPromptOrchestrator(recall))

        answer = answers.answer(
            "what's my name?",
            context=_context(execution_context, mode=RecallMode.DETERMINISTIC),
        )

        assert answer == "From your saved memory: Soham"


class TestForgetAndRestate:
    def test_explicitly_restating_a_forgotten_fact_stores_it_again(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-05 — a forget is not a permanent ban on ever saying it again.

        `forget` leaves a tombstone that blocks *resurrection*, so an automatic
        re-extraction of the same fact stays blocked. An explicit request from
        the user is the documented exception (PLN-08): the person who deleted it
        is the person asking for it back.

        The three states are asserted in sequence — present, gone, present —
        because any two of them alone are satisfiable by a journey where nothing
        ever happened.
        """

        forget = "Forget that I want to improve at urban sketching"
        restate = "Remember that I want to improve at urban sketching"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    forget: model_output(
                        retractions=[
                            retraction(
                                forget, GOAL_VALUE, old_value_hint=GOAL_VALUE,
                                explicit_forget=True, message_id="m2",
                            )
                        ]
                    ),
                    restate: _goal_turn(restate, GOAL_VALUE, message_id="m3"),
                }
            )
        )

        _store_goal(coordinator, adapter_context)
        assert _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        ).items, "stored"

        coordinator.process(
            _request(forget, message_id="m2", explicit_memory_intent=True),
            replace(adapter_context, message_id="m2"),
        )
        assert _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        ).items == (), "forgotten"

        coordinator.process(
            _request(restate, message_id="m3", explicit_memory_intent=True),
            replace(adapter_context, message_id="m3"),
        )

        restored = _recall_for(profile_database, execution_context).recall(
            _query(execution_context, "urban sketching")
        )
        assert [item.memory.display_text for item in restored.items] == [GOAL_VALUE]


class TestSensitiveWithoutRequest:
    def test_a_sensitive_fact_mentioned_in_passing_is_not_stored(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-09 — stating a diagnosis is not asking for it to be remembered.

        An innocuous fact is stored first, for two reasons: it proves the
        pipeline is working in this test rather than silently doing nothing, and
        it creates the database — a journey that stores nothing never migrates
        one, so the recall service would raise `memory_schema_or_binding_unavailable`
        instead of returning an empty result.
        """

        diagnosis = "I was diagnosed with asthma last year"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    diagnosis: model_output(
                        assertions=[
                            assertion(
                                diagnosis, "diagnosed with asthma",
                                memory_type="knowledge", domain_hint="health",
                                typed_value="diagnosed with asthma",
                                sensitivity_hint="sensitive", message_id="m2",
                            )
                        ]
                    ),
                }
            )
        )
        _store_goal(coordinator, adapter_context)

        result = coordinator.process(
            _request(diagnosis, message_id="m2"), replace(adapter_context, message_id="m2")
        )

        recall = _recall_for(profile_database, execution_context)
        assert recall.recall(_query(execution_context, "urban sketching")).items, (
            "the ordinary fact must still be stored, or this proves nothing"
        )
        assert recall.recall(_query(execution_context, "asthma diagnosis")).items == ()
        assert result.status.value != "applied"

    def test_the_diagnosis_text_is_not_written_anywhere(
        self, extraction_coordinator_factory, adapter_context, execution_context,
        profile_database,
    ) -> None:
        """E2E-09b — refused means absent, not merely unrecalled.

        A record excluded from recall but sitting in the store in plaintext would
        satisfy the test above while being the failure that matters.
        """

        from sqlalchemy import create_engine

        from tests.memory.test_privacy import _assert_absent

        diagnosis = "I was diagnosed with asthma last year"
        coordinator = extraction_coordinator_factory(
            scripted_model(
                {
                    SKETCHING: _goal_turn(SKETCHING, GOAL_VALUE),
                    diagnosis: model_output(
                        assertions=[
                            assertion(
                                diagnosis, "diagnosed with asthma",
                                memory_type="knowledge", domain_hint="health",
                                typed_value="diagnosed with asthma",
                                sensitivity_hint="sensitive", message_id="m2",
                            )
                        ]
                    ),
                }
            )
        )
        _store_goal(coordinator, adapter_context)
        coordinator.process(
            _request(diagnosis, message_id="m2"), replace(adapter_context, message_id="m2")
        )

        _assert_absent(create_engine(f"sqlite:///{profile_database}", future=True), "asthma")
