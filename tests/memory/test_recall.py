from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update

from app.models.memory import MemoryCandidate, MemoryRecord
from app.services.memory.contracts import (
    CandidateIntent,
    CandidateLifecycleState,
    MemoryLifecycleState,
    Sensitivity,
)
from app.services.memory.queries import RecallMode, RecallQuery
from app.services.memory.taxonomy import Cardinality, MemoryType
from app.services.memory.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION
from tests.memory.recall_helpers import (
    add_memory,
    query_context,
    recall_harness,
    recall_services,
)


def test_owner_status_expiry_predicates_are_in_canonical_sql(tmp_path) -> None:
    harness, adapter = recall_harness(tmp_path)
    active = add_memory(
        adapter,
        harness,
        key="active",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:active",
        text="edit cinematic video footage",
    )
    expired = add_memory(
        adapter,
        harness,
        key="expired",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:expired",
        text="edit expired video footage",
    )
    archived = add_memory(
        adapter,
        harness,
        key="archived",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:archived",
        text="edit archived video footage",
    )
    services = recall_services(harness)
    try:
        services.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(expired))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(archived))
            .values(status=MemoryLifecycleState.ARCHIVED.value)
        )
        services.session.commit()
        statement = services.repository.eligible_records_statement(now=datetime.now(UTC))
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "memory_records.owner_id =" in sql
        assert "memory_records.status = 'active'" in sql
        assert "memory_records.expires_at IS NULL" in sql
        assert "memory_records.expires_at >" in sql

        result = services.recall.recall(
            RecallQuery(
                context=query_context(
                    services,
                    domains=frozenset({"video_creation"}),
                ),
                text="edit video footage",
            )
        )
        assert result.canonical_ids == (active,)
        assert result.items[0].memory.source_ids
        assert result.diagnostic.filtered_inactive_count == 1
        assert result.diagnostic.filtered_expired_count == 1
    finally:
        services.close()


def test_scoped_relevance_domain_and_pin_cannot_bypass_gates(tmp_path) -> None:
    harness, adapter = recall_harness(tmp_path)
    relevant = add_memory(
        adapter,
        harness,
        key="relevant",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:reels",
        text="edit short video reels with clean cuts",
    )
    pinned = add_memory(
        adapter,
        harness,
        key="pinned",
        memory_type=MemoryType.KNOWLEDGE,
        domain="software_development",
        slot="knowledge:software_development:editor",
        text="edit source code in a video themed editor",
    )
    services = recall_services(harness)
    try:
        services.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(pinned))
            .values(pinned=True, importance=10)
        )
        services.session.commit()
        context = query_context(
            services,
            domains=frozenset({"video_creation"}),
        )
        first = services.recall.recall(RecallQuery(context=context, text="edit video reels"))
        second = services.recall.recall(RecallQuery(context=context, text="edit video reels"))
        assert first.canonical_ids == second.canonical_ids == (relevant,)

        unrelated = services.recall.recall(
            RecallQuery(context=context, text="quantum garden weather")
        )
        assert unrelated.items == ()
        assert unrelated.diagnostic.below_threshold_count >= 1
    finally:
        services.close()


def test_broad_recall_is_diverse_bounded_and_deterministic(tmp_path) -> None:
    harness, adapter = recall_harness(tmp_path)
    types = [
        MemoryType.GOAL,
        MemoryType.PREFERENCE,
        MemoryType.IDENTITY,
        MemoryType.PROJECT,
        MemoryType.KNOWLEDGE,
        MemoryType.GOAL,
        MemoryType.PREFERENCE,
        MemoryType.PROJECT,
        MemoryType.KNOWLEDGE,
        MemoryType.IDENTITY,
        MemoryType.ACTIVITY,
    ]
    for index, memory_type in enumerate(types):
        add_memory(
            adapter,
            harness,
            key=f"broad-{index}",
            memory_type=memory_type,
            domain="global",
            slot=f"{memory_type.value}:global:fixture_{index}",
            text=f"saved fixture {index} for broad canonical recall",
            cardinality=Cardinality.ADDITIVE,
            importance=10 - index % 5,
        )
    services = recall_services(harness)
    try:
        context = query_context(services, mode=RecallMode.BROAD)
        query = RecallQuery(context=context, text="what do you remember about me")
        first = services.recall.recall(query)
        second = services.recall.recall(query)
        assert 1 <= len(first.items) <= 5
        assert first.canonical_ids == second.canonical_ids
        assert len({item.memory.slot_key for item in first.items}) == len(first.items)
        assert len({item.memory.memory_type for item in first.items}) >= 3
    finally:
        services.close()


def test_sensitive_records_are_excluded_normally_and_redacted_for_explicit_lookup(
    tmp_path,
) -> None:
    harness, adapter = recall_harness(tmp_path)
    sensitive_id = add_memory(
        adapter,
        harness,
        key="sensitive",
        memory_type=MemoryType.KNOWLEDGE,
        domain="health_fitness",
        slot="knowledge:health_fitness:sensitive",
        text="synthetic sensitive fixture value",
        sensitivity=Sensitivity.SENSITIVE,
    )
    services = recall_services(harness)
    try:
        broad = services.recall.recall(
            RecallQuery(
                context=query_context(services, mode=RecallMode.BROAD),
                text="show my saved memories",
            )
        )
        assert sensitive_id not in broad.canonical_ids
        assert broad.diagnostic.filtered_sensitivity_count == 1

        context = query_context(services, mode=RecallMode.DETERMINISTIC).model_copy(
            update={"explicit_sensitive_lookup": True}
        )
        direct = services.recall.recall(RecallQuery(context=context, canonical_id=sensitive_id))
        assert direct.canonical_ids == (sensitive_id,)
        assert direct.items[0].memory.display_text == "[sensitive memory]"
        assert "synthetic sensitive fixture value" not in direct.model_dump_json()
    finally:
        services.close()


def test_exact_slot_lookup_and_broad_lexical_unavailable_fallback(tmp_path) -> None:
    harness, adapter = recall_harness(tmp_path)
    goal_id = add_memory(
        adapter,
        harness,
        key="exact-slot",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short video reels",
    )
    services = recall_services(harness)
    try:
        exact = services.recall.recall(
            RecallQuery(
                context=query_context(services, mode=RecallMode.DETERMINISTIC),
                memory_type=MemoryType.GOAL,
                domain_key="video_creation",
                slot_key="goal:video_creation:current_primary_goal",
            )
        )
        assert exact.canonical_ids == (goal_id,)

        broad = services.recall.recall(
            RecallQuery(
                context=query_context(
                    services,
                    mode=RecallMode.BROAD,
                    lexical_available=False,
                ),
                text="text that must not be lexically scored",
            )
        )
        assert broad.canonical_ids == (goal_id,)
        assert broad.items[0].score.lexical == 0
    finally:
        services.close()


def test_every_nonserving_lifecycle_shape_is_excluded_from_canonical_recall(tmp_path) -> None:
    harness, adapter = recall_harness(tmp_path)
    active = add_memory(
        adapter,
        harness,
        key="lifecycle-active",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:active",
        text="canonical lifecycle active fixture",
    )
    record_states = {
        state: add_memory(
            adapter,
            harness,
            key=f"lifecycle-{state.value}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:{state.value}",
            text=f"canonical lifecycle {state.value} fixture",
        )
        for state in (
            MemoryLifecycleState.SUPERSEDED,
            MemoryLifecycleState.ARCHIVED,
            MemoryLifecycleState.FORGOTTEN,
        )
    }
    expired = add_memory(
        adapter,
        harness,
        key="lifecycle-expired",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:expired",
        text="canonical lifecycle expired fixture",
    )
    services = recall_services(harness)
    try:
        for state, memory_id in record_states.items():
            services.session.execute(
                update(MemoryRecord)
                .where(MemoryRecord.id == str(memory_id))
                .values(status=state.value)
            )
        services.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(expired))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        for state in (
            CandidateLifecycleState.NEEDS_REVIEW,
            CandidateLifecycleState.REJECTED,
        ):
            candidate_id = str(uuid4())
            services.repository.add_candidate(
                MemoryCandidate(
                    id=candidate_id,
                    owner_id=services.repository.owner_id,
                    subject_key="user",
                    memory_type=MemoryType.KNOWLEDGE.value,
                    domain_key="learning",
                    slot_key=f"knowledge:learning:candidate:{candidate_id}",
                    cardinality=Cardinality.ADDITIVE.value,
                    sensitivity=Sensitivity.NORMAL.value,
                    canonical_payload=f"canonical candidate {state.value} fixture",
                    display_text=f"canonical candidate {state.value} fixture",
                    intent=CandidateIntent.ASSERT.value,
                    target_hints_json={},
                    trusted_target_ids=[],
                    predecessor_evidence_json={},
                    source_spans_json=[],
                    grounding_evidence_json={},
                    confidence=1,
                    importance=7,
                    explicit_user_request=False,
                    extractor_name="phase5-lifecycle-fixture",
                    extractor_version="1",
                    state=state.value,
                    revision=1,
                    contract_version=CONTRACT_VERSION,
                    policy_version=POLICY_VERSION,
                    taxonomy_version=TAXONOMY_VERSION,
                    value_schema_version=1,
                    candidate_schema_version=1,
                )
            )
        services.session.commit()

        result = services.recall.recall(
            RecallQuery(
                context=query_context(
                    services,
                    mode=RecallMode.BROAD,
                    domains=frozenset({"learning"}),
                ),
                text="show my saved memories",
            )
        )
        assert result.canonical_ids == (active,)
        assert result.diagnostic.filtered_inactive_count == 3
        assert result.diagnostic.filtered_expired_count == 1
        serving_sql = str(
            services.repository.eligible_records_statement(now=datetime.now(UTC)).compile()
        )
        assert "memory_candidates" not in serving_sql
    finally:
        services.close()
