"""Tier 7 — end-to-end journeys (plan section E2E-10..18) and CNC-05.

Every other file in this suite tests one layer. These walk a whole path — a turn
arrives, extraction runs, a record lands, recall finds it, a forget removes it —
and assert the property a user would actually notice.

They are written last on purpose. A journey crossing five layers is only
diagnostic when the layers beneath it are already pinned; written first, a red
E2E means opening four investigations at once. Everything under these is now
covered, so a failure here points at the seam between layers rather than at any
one of them.

Two rules these follow that the single-layer tests do not need:

* **Assert what the user sees, not what a function returned.** "The status says
  applied" and "the memory is gone from the store" are different claims, and
  only the second is the one that matters at this level.
* **Prove the setup worked before asserting the outcome.** A journey that
  silently failed at step one satisfies most "and then nothing bad happened"
  assertions. Several of these carry an explicit positive control for exactly
  that reason.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import ExtractionStatus
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.maintenance import MemoryIndexMaintenance
from app.services.memory.policy import MAX_RECALL_CONTEXT_CHARS
from app.services.memory.taxonomy import MemoryType
from tests.memory.conftest import FROZEN_NOW, OWNER_ID
from tests.memory.doubles import FakeEmbeddingProvider, assertion, model_output, scripted_model
from tests.memory.factories import insert_record
from tests.memory.test_extraction_coordinator import PYTHON_MESSAGE, python_model, request_for
from tests.memory.test_recall import _query


def recalled_texts(recall_service, tmp_path, text: str) -> set[str]:
    result = recall_service.recall(_query(tmp_path, text))
    return {item.memory.display_text for item in result.items}


class TestSensitiveAndProhibitedJourneys:
    def test_a_sensitive_fact_is_stored_without_plaintext_anywhere(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """E2E-10 — the whole path for a fact the user asked to keep private.

        The value must reach the store, and must not be readable in it. This
        walks the turn and then sweeps *every table* in the profile database for
        the plaintext, rather than checking the one column it is supposed to be
        encrypted in — the value passes through the candidate, the operation's
        command payload, and the source excerpt on the way, and any of those
        could hold it in the clear.
        """

        from sqlalchemy import create_engine, inspect
        from sqlalchemy import text as sql_text

        message = "Remember that my home address is 12 Baker Street."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            "12 Baker Street",
                            memory_type="identity",
                            domain_hint="contact",
                            sensitivity_hint="sensitive",
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message, explicit_memory_intent=True), adapter_context
        )
        assert result.status is ExtractionStatus.APPLIED, "setup failed: nothing was stored"

        records = chat_adapter.list_active_memories(adapter_context)
        assert len(records) == 1
        assert records[0].sensitivity is Sensitivity.SENSITIVE

        database = create_engine(adapter_context.execution.database_url)
        leaked: list[str] = []
        with database.connect() as connection:
            for table in inspect(connection).get_table_names():
                rows = connection.execute(sql_text(f"SELECT * FROM {table}")).fetchall()  # noqa: S608
                for row in rows:
                    if "Baker Street" in str(tuple(row)):
                        leaked.append(table)
        assert leaked == [], f"the plaintext address survives in: {sorted(set(leaked))}"

    def test_a_prohibited_fact_never_reaches_the_store(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """E2E-11 — refused before the model, and the turn says why.

        The refusal has to be legible: a turn that silently did nothing is
        indistinguishable from one where extraction was switched off, and the
        assistant needs to be able to tell the user it declined.
        """

        from tests.memory.doubles import RecordingModel

        message = "My credit card number is 4111 1111 1111 1111."
        model = RecordingModel(model_output())
        result = extraction_coordinator_factory(model).process(
            request_for(message), adapter_context
        )
        assert result.status is ExtractionStatus.REJECTED
        assert result.diagnostic.reason_codes == ("prohibited_source_rejected_before_model",)
        assert model.call_count == 0
        assert chat_adapter.list_active_memories(adapter_context) == ()


class TestTurnsThatMustNotWrite:
    def test_asking_what_you_remember_creates_nothing(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """E2E-12 — the regression that produced three records from one question.

        The setup matters: there must already be memories, because the original
        bug re-asserted facts read from the supporting window as if they were
        new. A store that was empty to begin with would pass whatever happened.
        """

        extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        before = chat_adapter.list_active_memories(adapter_context)
        assert len(before) == 1, "setup failed: nothing to re-assert"

        question = "What do you remember about my goals?"
        result = extraction_coordinator_factory(scripted_model({question: model_output()})).process(
            request_for(question, message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert result.status is ExtractionStatus.NO_ACTION
        after = chat_adapter.list_active_memories(adapter_context)
        assert {item.memory_id for item in after} == {item.memory_id for item in before}

    def test_incognito_writes_nothing_and_recalls_nothing(
        self,
        extraction_coordinator_factory,
        adapter_context,
        chat_adapter,
        recall_service,
        engine,
        tmp_path,
    ) -> None:
        """E2E-13 — both halves, because either alone is half a promise.

        Writing nothing but still reading existing memories would leak the
        user's history into a session they marked private; reading nothing but
        still writing would record it.
        """

        insert_record(engine, display_text="improve at urban sketching")
        assert recalled_texts(recall_service, tmp_path, "sketching"), "setup failed"

        incognito = replace(
            adapter_context,
            execution=replace(adapter_context.execution, is_incognito=True),
        )
        result = extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), incognito
        )
        assert result.status is ExtractionStatus.DISABLED
        assert chat_adapter.list_active_memories(adapter_context) == ()

        gated = recall_service.recall(_query(tmp_path, "sketching", context={"incognito": True}))
        assert gated.items == ()

    def test_memory_disabled_writes_nothing_and_recalls_nothing(
        self,
        extraction_coordinator_factory,
        adapter_context,
        chat_adapter,
        recall_service,
        engine,
        tmp_path,
    ) -> None:
        """E2E-14 — the same two halves for the persistent setting.

        Separate from incognito because they are separate switches: one is a
        per-session choice, the other a profile-wide one, and a bug in either
        gate is invisible from the other's tests.
        """

        insert_record(engine, display_text="improve at urban sketching")
        assert recalled_texts(recall_service, tmp_path, "sketching"), "setup failed"

        disabled = replace(
            adapter_context,
            execution=replace(adapter_context.execution, memory_enabled=False),
        )
        result = extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), disabled
        )
        assert result.status is ExtractionStatus.DISABLED
        assert chat_adapter.list_active_memories(adapter_context) == ()

        gated = recall_service.recall(
            _query(tmp_path, "sketching", context={"memory_enabled": False})
        )
        assert gated.items == ()


class TestRecoveryJourneys:
    def test_a_mutation_killed_partway_leaves_a_consistent_store(
        self, mutation_service_factory, adapter_context, chat_adapter, engine
    ) -> None:
        """E2E-15 — the crash case, using the kernel's own failure injection.

        A process dying mid-write is what a transaction boundary exists for. The
        kernel has injection points so this can be exercised without killing a
        process.

        `test_mutations.py` already covers rollback stage by stage, so the claim
        here is the one only a journey can make: **the store is not left
        wedged.** After a crashed write, the next write has to succeed and be
        recallable. A rollback that also poisoned the idempotency ledger, or
        left a lock, would satisfy every "nothing was written" assertion while
        making the profile unusable — and that is the failure a user would
        actually experience.
        """

        from sqlalchemy import func, select

        from app.models.memory import MemoryRecord as MemoryRecordRow
        from app.services.memory.mutations import RetryPolicy
        from tests.memory.factories import create_command

        def fail_before_commit(stage: str) -> None:
            # "outbox_creation" is the last stage inside the transaction, so a
            # failure there is the closest thing to the process dying with the
            # record written but nothing committed.
            if stage == "outbox_creation":
                raise RuntimeError("simulated process death")

        crashed = mutation_service_factory(
            failure_injector=fail_before_commit,
            retry_policy=RetryPolicy(attempts=1),
        )
        result = crashed.execute(create_command(idempotency_key="memory:manual:killed"))
        assert result.outcome.value == "failed"

        with engine.connect() as connection:
            assert connection.scalar(select(func.count(MemoryRecordRow.id))) == 0

        # The part only an end-to-end test asserts: the store still works.
        healthy = mutation_service_factory(retry_policy=RetryPolicy(attempts=1))
        recovered = healthy.execute(create_command(idempotency_key="memory:manual:after-crash"))
        assert recovered.outcome.value == "created"
        with engine.connect() as connection:
            assert connection.scalar(select(func.count(MemoryRecordRow.id))) == 1

    def test_dropping_both_indexes_and_rebuilding_restores_recall(
        self, engine, tmp_path, recall_service
    ) -> None:
        """E2E-16 — derived data is reconstructible, which is the whole claim.

        The positive control is the point here, and the parallel session's
        warning is why. `rebuild_owner` defaults to wall-clock `now`, while the
        fixtures write records dated `FROZEN_NOW`; call it without passing the
        clock and it indexes *nothing*, and every "recall still works"
        assertion below would pass against an empty index if recall had a
        lexical path that did not need one.

        So this asserts the rebuild reported eligible records before asserting
        recall agrees, and checks the index is genuinely empty in between.
        """

        for index in range(5):
            insert_record(engine, display_text=f"urban sketching note {index}")

        fts = SqliteMemoryFtsIndex(engine)
        vectors = SqliteMemoryVectorIndex(engine)
        maintenance = MemoryIndexMaintenance(
            engine,
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            fts_index=fts,
            vector_index=vectors,
            repair_scheduler=lambda request: None,
            embedding_provider=FakeEmbeddingProvider(),
        )

        before = recalled_texts(recall_service, tmp_path, "sketching")
        assert before, "setup failed: nothing was recallable to begin with"

        assert fts.clear_owner(OWNER_ID) >= 0
        vectors.clear_owner(OWNER_ID)
        assert fts.list_metadata_for_owner(OWNER_ID) == []

        result = maintenance.rebuild_owner(now=FROZEN_NOW)
        assert result.canonical_eligible_count == 5, "the rebuild indexed nothing"
        assert result.pending_target_count >= 5

        after = recalled_texts(recall_service, tmp_path, "sketching")
        assert after == before

    def test_reconciliation_during_a_mutation_does_not_corrupt_derived_state(
        self, engine, tmp_path, mutation_coordinator, adapter_context
    ) -> None:
        """CNC-05 — a sweep and a write, concurrently, against one file.

        Reconciliation reads the canonical records and the derived rows to
        decide what has drifted. A write landing between those two reads could
        make it "detect" drift that is merely a snapshot boundary, and schedule
        a repair for a record that was never wrong.

        What must hold is narrower than "no drift is ever reported": a
        concurrent write may legitimately look like drift for one pass. What
        must not happen is corruption — the sweep must finish, report coherent
        counts, and leave the store readable.

        **Both halves run against the same database file, which took a
        correction.** The first version built maintenance on the `engine`
        fixture (`memory.db`) while the writes went through the mutation
        coordinator, which builds its own engine from `database_url`
        (`profile.db`). Two different files, so there was no contention at all
        and the test would have passed against a completely unsynchronised
        implementation. Everything here now goes through the coordinator's
        database.
        """

        from dataclasses import replace as dataclass_replace
        from uuid import uuid4

        from sqlalchemy import create_engine

        from app.services.memory.adapters import GenericMemoryAdapter
        from tests.memory.test_adapters import structured
        from tests.memory.test_concurrency import run_concurrently

        adapter = GenericMemoryAdapter(mutation_coordinator)
        for index in range(30):
            adapter.create(
                adapter_context,
                dataclass_replace(
                    structured(),
                    slot_key=f"goal:global:independent:{uuid4()}",
                    canonical_value=f"note {index}",
                    display_text=f"note {index}",
                ),
                idempotency_key=f"memory:manual:seed-{index}",
            )

        shared = create_engine(adapter_context.execution.database_url)
        maintenance = MemoryIndexMaintenance(
            shared,
            owner_id=OWNER_ID,
            database_identity=adapter_context.execution.database_identity,
            fts_index=SqliteMemoryFtsIndex(shared),
            vector_index=SqliteMemoryVectorIndex(shared),
            repair_scheduler=lambda request: None,
            embedding_provider=FakeEmbeddingProvider(),
        )
        assert maintenance.reconcile(now=FROZEN_NOW, limit=100).checked == 30, (
            "setup failed: the sweep and the writes are not on the same database"
        )

        def act(index: int):
            if index % 2 == 0:
                return maintenance.reconcile(now=FROZEN_NOW, limit=10)
            return adapter.create(
                adapter_context,
                dataclass_replace(
                    structured(),
                    slot_key=f"goal:global:independent:{uuid4()}",
                    canonical_value=f"concurrent {index}",
                    display_text=f"concurrent {index}",
                ),
                idempotency_key=f"memory:manual:concurrent-{index}",
            )

        results = run_concurrently(act, 6)
        errors = [item for status, item in results if status == "error"]
        assert errors == [], f"concurrent reconcile and write raised: {errors}"

        final = maintenance.reconcile(now=FROZEN_NOW, limit=200)
        assert final.checked >= 30
        assert final.ghost_fts == 0, "a ghost appeared from a concurrent write"


class TestScaleAndColdStart:
    def test_a_cold_store_recalls_nothing_rather_than_guessing(
        self, recall_service, tmp_path
    ) -> None:
        """E2E-17 — the first-run experience.

        An empty store must return an empty result, not a low-scoring match.
        The failure this guards against is an assistant confidently answering
        from nothing on a brand-new profile, which is worse than saying it does
        not know.
        """

        result = recall_service.recall(_query(tmp_path, "what are my goals"))
        assert result.items == ()

    def test_fifty_facts_across_every_type_stay_within_budget(
        self, engine, recall_service, tmp_path
    ) -> None:
        """E2E-18 — a realistic store, not a synthetic one.

        Fifty facts spread across every memory type is roughly what a profile
        looks like after a few weeks of use. Recall must stay inside the prompt
        budget with that much to choose from — the budget is what protects every
        turn, and it is only tested when there is genuinely more to return than
        fits.
        """

        from uuid import uuid4

        types = list(MemoryType)
        for index in range(50):
            memory_type = types[index % len(types)]
            insert_record(
                engine,
                display_text=f"urban sketching fact {index}",
                memory_type=memory_type,
                slot_key=f"{memory_type.value}:global:independent:{uuid4()}",
            )

        result = recall_service.recall(_query(tmp_path, "sketching"))
        assert result.items, "nothing was recalled from fifty matching facts"
        total = sum(len(item.memory.display_text) for item in result.items)
        assert total <= MAX_RECALL_CONTEXT_CHARS

    def test_recall_prefers_the_query_over_arbitrary_records(
        self, engine, recall_service, tmp_path
    ) -> None:
        """E2E-18b — a budget met by returning nothing relevant is no use.

        E2E-18 proves recall stays inside the budget. On its own that is
        satisfiable by returning any five records. This adds the other half: the
        one record that actually matches the query has to be among them, in a
        store where forty-nine others do not match at all.
        """

        from uuid import uuid4

        for index in range(49):
            insert_record(
                engine,
                display_text=f"unrelated administrative note {index}",
                slot_key=f"goal:global:independent:{uuid4()}",
            )
        insert_record(
            engine,
            display_text="improve at urban sketching",
            slot_key=f"goal:global:independent:{uuid4()}",
        )

        found = recalled_texts(recall_service, tmp_path, "sketching")
        assert "improve at urban sketching" in found


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
