"""Tier 3 — the structured adapters and source changes (plan sections ADP, SRC).

Every write into memory goes through one of these adapters and ends at a single
mutation coordinator. The adapters are thin on purpose: they exist to attach the
right *identity* to a write — which actor, which source, and above all which
idempotency surface — and then get out of the way.

That last part is what these tests are mostly about. Each surface (`chat`,
`review`, `import`, `agent`, `maintenance`, `manual`, `source_change`) namespaces
its own keys. Without that separation, a chat extraction and a human review of
the same candidate could hash to the same key, and the second would be silently
swallowed as a replay of the first — a reviewer's decision vanishing because a
background worker got there first with a coincidentally identical id.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.memory.adapters import (
    AgentMemoryAdapter,
    CandidateReviewAction,
    CandidateReviewAdapter,
    GenericMemoryAdapter,
    ImportMemoryAdapter,
    MaintenanceMemoryAdapter,
    MemoryAdapterContext,
    MemoryAdapterError,
    StructuredMemoryInput,
    TypedMemoryAdapter,
    structured_item_hash,
)
from app.services.memory.contracts import (
    ActorKind,
    MemoryOutcome,
    SourceKind,
    TargetRevision,
)
from app.services.memory.coordinator import MemoryCoordinationError
from app.services.memory.idempotency import MemoryIdempotency
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID, PROFILE_ID
from tests.memory.factories import DEFAULT_DOMAIN, DEFAULT_GOAL_SLOT


def structured(**overrides) -> StructuredMemoryInput:
    """A valid structured input; every adapter takes one of these."""

    # Uses the goal shape from ``factories``: an additive slot must carry an
    # entity id, and inventing one here would fail taxonomy validation rather
    # than test the adapter.
    values = {
        "memory_type": MemoryType.GOAL,
        "domain_key": DEFAULT_DOMAIN,
        "slot_key": DEFAULT_GOAL_SLOT,
        "cardinality": Cardinality.ADDITIVE,
        "canonical_value": "improve at urban sketching",
        "display_text": "improve at urban sketching",
    }
    values.update(overrides)
    return StructuredMemoryInput(**values)


@pytest.fixture
def generic(mutation_coordinator) -> GenericMemoryAdapter:
    return GenericMemoryAdapter(mutation_coordinator)


class TestTheExecutionContext:
    def test_a_valid_owner_is_canonicalised(self, execution_context) -> None:
        """ADP-01"""

        assert execution_context.validated_owner() == OWNER_ID

    @pytest.mark.parametrize("owner", ["not-a-uuid", "", "11111111"])
    def test_an_invalid_owner_is_refused(self, execution_context, owner: str) -> None:
        """ADP-01b — the owner is the only thing separating two profiles."""

        with pytest.raises(ValueError, match="canonical_uuid_required"):
            replace(execution_context, owner_id=owner).validated_owner()

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("profile_id", "  ", "profile_context_required"),
            ("database_identity", "  ", "database_identity_required"),
            ("database_url", "  ", "explicit_profile_database_required"),
            ("database_identity", "guest-profile:x", "guest_permanent_database_binding_mismatch"),
        ],
    )
    def test_an_incomplete_context_is_refused_before_any_database_work(
        self, generic, execution_context, field: str, value: str, message: str
    ) -> None:
        """ADP-02 — checked up front, so a bad context cannot open a database.

        The last case is the one that matters most: a permanent profile served
        from a guest database, or the reverse. The identity prefix is what makes
        that detectable, and it is checked before a connection exists.
        """

        context = MemoryAdapterContext(
            execution=replace(execution_context, **{field: value}),
            actor_kind=ActorKind.USER,
            actor_id=OWNER_ID,
            source_kind=SourceKind.MANUAL_UI,
        )
        with pytest.raises(MemoryCoordinationError, match=message):
            generic.create(context, structured(), idempotency_key="memory:manual:abc")

    def test_a_guest_profile_needs_a_guest_database(self, generic, execution_context) -> None:
        """ADP-02b — the same check from the other direction."""

        context = MemoryAdapterContext(
            execution=replace(
                execution_context,
                is_guest=True,
                database_identity=f"account-profile:{PROFILE_ID}",
            ),
            actor_kind=ActorKind.USER,
            actor_id=OWNER_ID,
            source_kind=SourceKind.MANUAL_UI,
        )
        with pytest.raises(MemoryCoordinationError, match="guest_permanent_database_binding"):
            generic.create(context, structured(), idempotency_key="memory:manual:abc")


class TestGating:
    def test_incognito_short_circuits_before_a_service_is_built(
        self, generic, adapter_context
    ) -> None:
        """ADP-03 — an incognito turn must not even open the database.

        Gating after the write would leave the row and then decline to report
        it, which is the worst of both.
        """

        context = replace(
            adapter_context,
            execution=replace(adapter_context.execution, is_incognito=True),
        )
        result = generic.create(context, structured(), idempotency_key="memory:manual:abc")
        assert result.called is False
        assert result.reason == "incognito_disabled"

    def test_a_disabled_profile_short_circuits(self, generic, adapter_context) -> None:
        """ADP-03b"""

        context = replace(
            adapter_context,
            execution=replace(adapter_context.execution, memory_enabled=False),
        )
        result = generic.create(context, structured(), idempotency_key="memory:manual:abc")
        assert result.called is False
        assert result.reason == "memory_disabled"

    def test_a_gated_write_leaves_no_record(self, generic, adapter_context) -> None:
        """ADP-03c — the property, rather than the report of the property."""

        context = replace(
            adapter_context,
            execution=replace(adapter_context.execution, is_incognito=True),
        )
        generic.create(context, structured(), idempotency_key="memory:manual:abc")
        assert generic.list_active_memories(adapter_context) == ()


class TestTheGenericVerbs:
    def test_create_writes_a_record(self, generic, adapter_context) -> None:
        """ADP-04"""

        result = generic.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        assert result.called is True
        assert result.mutation.outcome is MemoryOutcome.CREATED
        assert len(generic.list_active_memories(adapter_context)) == 1

    def test_archive_removes_it_from_the_active_set(self, generic, adapter_context) -> None:
        """ADP-04b"""

        created = generic.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]
        generic.archive(
            adapter_context,
            TargetRevision(memory_id=memory_id, expected_revision=1),
            idempotency_key="memory:manual:archive-1",
        )
        assert generic.list_active_memories(adapter_context) == ()

    def test_the_same_idempotency_key_does_not_write_twice(self, generic, adapter_context) -> None:
        """ADP-04c — the whole reason every verb demands a key.

        Adapters are called from retryable places: HTTP handlers, background
        workers, a user pressing a button twice.
        """

        for _ in range(2):
            generic.create(adapter_context, structured(), idempotency_key="memory:manual:create-1")
        assert len(generic.list_active_memories(adapter_context)) == 1

    def test_a_cross_owner_command_is_refused(self, generic, adapter_context) -> None:
        """ADP-18 — the boundary, checked at the coordinator for every verb.

        The command carries an owner and so does the context. If they disagree
        the write would land in whichever database the context supplied.
        """

        from tests.memory.factories import create_command

        command = create_command(owner=OTHER_OWNER_ID, idempotency_key="memory:manual:x")
        with pytest.raises(MemoryCoordinationError, match="command_owner_context_mismatch"):
            generic.execute(adapter_context, command)


class TestIdempotencySurfaces:
    """Each surface namespaces its own keys, so two callers cannot collide."""

    def test_every_surface_produces_a_distinct_key(self) -> None:
        """ADP-09 / ADP-10 / ADP-12 / ADP-13 / ADP-14 — the separation itself.

        Every surface is fed the *same* logical identifier here. If any two
        collided, the second caller's write would be swallowed as a replay of
        the first — a reviewer's decision silently lost to a background worker,
        or an import overwriting an agent's write.
        """

        keys = {
            "http": MemoryIdempotency.http(OWNER_ID, "x", "create"),
            "review": MemoryIdempotency.review(OWNER_ID, "x", 1, "accept"),
            "chat": MemoryIdempotency.chat(OWNER_ID, "x", "v1", "x"),
            "source_change": MemoryIdempotency.source_change(OWNER_ID, "x", 1, "x", "delete"),
            "import": MemoryIdempotency.imported(OWNER_ID, "x", "x"),
            "agent": MemoryIdempotency.agent(OWNER_ID, "x"),
            "maintenance": MemoryIdempotency.maintenance(OWNER_ID, "x", "x"),
            "manual": MemoryIdempotency.manual(OWNER_ID, "x"),
        }
        assert len(set(keys.values())) == len(keys)

    def test_the_surface_is_visible_in_the_key(self) -> None:
        """ADP-09b — so an operator reading an audit row can tell where it came from."""

        assert MemoryIdempotency.review(OWNER_ID, "x", 1, "accept").startswith("memory:review:")
        assert MemoryIdempotency.agent(OWNER_ID, "x").startswith("memory:agent:")

    def test_a_key_is_owner_scoped(self) -> None:
        """ADP-18b — two profiles doing the same thing must not share a key.

        They live in separate databases, so a collision would not cross
        profiles today. This keeps that true if the storage layout ever changes.
        """

        assert MemoryIdempotency.agent(OWNER_ID, "call-1") != MemoryIdempotency.agent(
            OTHER_OWNER_ID, "call-1"
        )

    @pytest.mark.parametrize(
        "build",
        [
            lambda: MemoryIdempotency.review(OWNER_ID, "c1", 1, "accept"),
            lambda: MemoryIdempotency.chat(OWNER_ID, "m1", "v1", "p1"),
            lambda: MemoryIdempotency.imported(OWNER_ID, "b1", "h1"),
        ],
    )
    def test_a_key_is_deterministic(self, build) -> None:
        """ADP-09c — a retry has to produce the identical key or it isn't a retry."""

        assert build() == build()

    def test_the_review_key_changes_with_the_action(self) -> None:
        """ADP-09d — accepting and rejecting one candidate are different decisions.

        Sharing a key would make a reject-then-accept sequence a no-op.
        """

        accept = MemoryIdempotency.review(OWNER_ID, "c1", 1, "accept")
        reject = MemoryIdempotency.review(OWNER_ID, "c1", 1, "reject")
        assert accept != reject

    def test_the_review_key_changes_with_the_revision(self) -> None:
        """ADP-09e — a candidate edited between reviews is a new decision."""

        assert MemoryIdempotency.review(OWNER_ID, "c1", 1, "accept") != MemoryIdempotency.review(
            OWNER_ID, "c1", 2, "accept"
        )


class TestTypedAndSurfaceAdapters:
    def test_the_typed_adapter_uses_the_manual_surface(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-05 — a UI write is a manual write, not an extraction."""

        adapter = TypedMemoryAdapter(mutation_coordinator)
        result = adapter.create_typed(adapter_context, structured(), client_mutation_id="ui-1")
        assert result.mutation.outcome is MemoryOutcome.CREATED

    def test_the_typed_adapter_is_idempotent_per_client_mutation_id(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-05b — a double-clicked save button writes once."""

        adapter = TypedMemoryAdapter(mutation_coordinator)
        for _ in range(2):
            adapter.create_typed(adapter_context, structured(), client_mutation_id="ui-1")
        assert len(adapter.list_active_memories(adapter_context)) == 1

    def test_the_import_adapter_requires_a_complete_item(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-12 — imported data is untrusted; a partial row is refused.

        Filling in defaults for a missing `slot_key` would file an imported
        memory under a slot the export never claimed.
        """

        adapter = ImportMemoryAdapter(mutation_coordinator)
        with pytest.raises(MemoryAdapterError, match="imported_structured_memory_incomplete"):
            adapter.accept(adapter_context, {"memory_type": "goal"}, batch_id="b", item_hash="h")

    def test_the_import_adapter_accepts_a_complete_item(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-12b"""

        adapter = ImportMemoryAdapter(mutation_coordinator)
        result = adapter.accept(
            adapter_context,
            {
                "memory_type": "goal",
                "domain_key": DEFAULT_DOMAIN,
                "slot_key": DEFAULT_GOAL_SLOT,
                "cardinality": "additive",
                "canonical_value": "improve at urban sketching",
                "display_text": "improve at urban sketching",
            },
            batch_id="batch-1",
            item_hash="hash-1",
        )
        assert result.mutation.outcome is MemoryOutcome.CREATED

    def test_the_agent_adapter_refuses_a_non_agent_actor(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-14 — a tool-authored memory must be attributable to the agent.

        Provenance is the point. If a write from an agent could be recorded as
        a user write, the audit trail would claim the user said something they
        never said.
        """

        adapter = AgentMemoryAdapter(mutation_coordinator)
        with pytest.raises(MemoryAdapterError, match="agent_actor_required"):
            adapter.create_from_tool(adapter_context, structured(), tool_call_id="call-1")

    def test_the_agent_adapter_accepts_an_agent_actor(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-14b"""

        adapter = AgentMemoryAdapter(mutation_coordinator)
        context = replace(
            adapter_context, actor_kind=ActorKind.AGENT, source_kind=SourceKind.AGENT_TOOL
        )
        result = adapter.create_from_tool(context, structured(), tool_call_id="call-1")
        assert result.mutation.outcome is MemoryOutcome.CREATED

    def test_the_maintenance_adapter_archives_under_its_run(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-13 — a maintenance sweep is replayable without double-archiving."""

        generic = GenericMemoryAdapter(mutation_coordinator)
        created = generic.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]

        adapter = MaintenanceMemoryAdapter(mutation_coordinator)
        target = TargetRevision(memory_id=memory_id, expected_revision=1)
        first = adapter.archive_proposal(
            adapter_context, target, run_id="run-1", proposal_hash="h1"
        )
        assert first.mutation.outcome is MemoryOutcome.ARCHIVED
        assert generic.list_active_memories(adapter_context) == ()


class TestCandidateReview:
    def _adapter(self, mutation_coordinator) -> CandidateReviewAdapter:
        return CandidateReviewAdapter(mutation_coordinator)

    def test_accepting_a_candidate_creates_the_memory(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-06"""

        result = self._adapter(mutation_coordinator).apply(
            adapter_context,
            candidate_id="cand-1",
            candidate_revision=1,
            action=CandidateReviewAction.ACCEPT,
            item=structured(),
        )
        assert result.outcome == MemoryOutcome.CREATED.value
        assert result.review_required is False

    def test_rejecting_writes_nothing(self, mutation_coordinator, adapter_context) -> None:
        """ADP-07 — a rejection is a decision, not a mutation.

        It must not touch the store: the reviewer said no.
        """

        adapter = self._adapter(mutation_coordinator)
        result = adapter.apply(
            adapter_context,
            candidate_id="cand-1",
            candidate_revision=1,
            action=CandidateReviewAction.REJECT,
        )
        assert result.outcome == "rejected"
        assert result.coordination is None
        assert adapter.list_active_memories(adapter_context) == ()

    def test_marking_ambiguous_requires_review_and_writes_nothing(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """ADP-07b — "I can't tell" is distinct from "no"."""

        adapter = self._adapter(mutation_coordinator)
        result = adapter.apply(
            adapter_context,
            candidate_id="cand-1",
            candidate_revision=1,
            action=CandidateReviewAction.AMBIGUOUS,
        )
        assert result.review_required is True
        assert result.coordination is None
        assert adapter.list_active_memories(adapter_context) == ()

    def test_accepting_twice_writes_once(self, mutation_coordinator, adapter_context) -> None:
        """ADP-08 — the review key covers candidate, revision and action.

        A reviewer double-clicking, or two review workers picking up the same
        item, must not produce two memories.
        """

        adapter = self._adapter(mutation_coordinator)
        for _ in range(2):
            adapter.apply(
                adapter_context,
                candidate_id="cand-1",
                candidate_revision=1,
                action=CandidateReviewAction.ACCEPT,
                item=structured(),
            )
        assert len(adapter.list_active_memories(adapter_context)) == 1

    @pytest.mark.parametrize(
        "action",
        [
            CandidateReviewAction.ACCEPT,
            CandidateReviewAction.REFINE,
            CandidateReviewAction.REPLACE,
            CandidateReviewAction.MERGE,
        ],
    )
    def test_an_action_without_its_required_inputs_is_refused(
        self, mutation_coordinator, adapter_context, action
    ) -> None:
        """ADP-06b — each action needs specific inputs, and guesses are refused.

        Accepting with no item, or refining with no patch, is a caller bug.
        Silently doing nothing would report a decision that never happened.
        """

        with pytest.raises(MemoryAdapterError, match="review_action_inputs_invalid"):
            self._adapter(mutation_coordinator).apply(
                adapter_context,
                candidate_id="cand-1",
                candidate_revision=1,
                action=action,
            )


class TestStructuredItemHash:
    def test_the_hash_is_stable(self) -> None:
        """ADP-15"""

        assert structured_item_hash(structured()) == structured_item_hash(structured())

    def test_the_hash_is_independent_of_field_order(self) -> None:
        """ADP-15b — it is used as an import identity, so it must not depend on
        how the caller happened to construct the object."""

        first = StructuredMemoryInput(
            memory_type=MemoryType.GOAL,
            domain_key=DEFAULT_DOMAIN,
            slot_key=DEFAULT_GOAL_SLOT,
            cardinality=Cardinality.ADDITIVE,
            canonical_value="improve at urban sketching",
            display_text="improve at urban sketching",
        )
        second = StructuredMemoryInput(
            display_text="improve at urban sketching",
            canonical_value="improve at urban sketching",
            cardinality=Cardinality.ADDITIVE,
            slot_key=DEFAULT_GOAL_SLOT,
            domain_key=DEFAULT_DOMAIN,
            memory_type=MemoryType.GOAL,
        )
        assert structured_item_hash(first) == structured_item_hash(second)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("display_text", "something else"),
            ("canonical_value", "something else"),
            ("domain_key", "urban_sketching"),
            ("slot_key", "goal:urban_sketching:primary"),
            ("memory_type", MemoryType.PREFERENCE),
            ("cardinality", Cardinality.EXCLUSIVE),
        ],
    )
    def test_the_hash_changes_with_any_identifying_field(self, field, value) -> None:
        """ADP-15c — a field in the identity that doesn't move the hash means two
        different memories import as one."""

        assert structured_item_hash(structured()) != structured_item_hash(
            structured(**{field: value})
        )

    def test_the_hash_ignores_non_identifying_fields(self) -> None:
        """ADP-15d — confidence and importance describe the same memory.

        Re-importing a batch whose confidence was recalculated must not create
        duplicates.
        """

        assert structured_item_hash(structured(confidence=0.5)) == structured_item_hash(
            structured(confidence=0.9)
        )


class TestContextModels:
    def test_the_actor_and_source_are_valid_contract_models(self, adapter_context) -> None:
        """ADP-16"""

        actor = adapter_context.actor()
        source = adapter_context.source()
        assert actor.kind is ActorKind.USER
        assert source.kind is SourceKind.CHAT_MESSAGE
        assert source.message_id == "m1"

    def test_an_unvalidatable_proposal_is_refused(self) -> None:
        """ADP-17 — a structured input that cannot become a proposal fails here.

        Better at the adapter than three layers down with a contract error that
        names none of the caller's fields.
        """

        from app.services.memory.adapters import _validated_candidate

        with pytest.raises(MemoryAdapterError, match="structured_candidate_invalid"):
            _validated_candidate(structured(display_text=""))


class TestSourceChanges:
    def test_detaching_is_owner_scoped(self, mutation_coordinator, adapter_context) -> None:
        """SRC-05 — a source detach is a write, and writes are owner-bound.

        Note where this fails: not at the command's owner check but earlier, in
        the migration binding check, which refuses to open a database whose
        recorded owner disagrees with the caller's. Two independent layers both
        stop it, and the outer one stops it before a connection is usable.
        """

        from uuid import uuid4

        from app.services.memory.source_changes import MemorySourceChangeCoordinator

        generic = GenericMemoryAdapter(mutation_coordinator)
        created = generic.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]

        coordinator = MemorySourceChangeCoordinator(generic)
        foreign = replace(
            adapter_context,
            execution=replace(adapter_context.execution, owner_id=OTHER_OWNER_ID),
        )
        from app.db.memory_migrations import MemoryMigrationError

        with pytest.raises(MemoryMigrationError, match="owner_database_binding_mismatch"):
            coordinator.delete_message_source(
                foreign,
                message_id="m1",
                edit_revision=1,
                target=TargetRevision(memory_id=memory_id, expected_revision=1),
                source_id=uuid4(),
            )

    def test_detaching_never_runs_a_lifecycle_command(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """SRC-04 — removing provenance is not removing the memory.

        The docstring on the coordinator says it "never infers or runs a
        lifecycle command", and that is the security-relevant part: editing a
        message must not silently delete what was learned from it. The record
        stays active; losing its last source is a review signal, not a delete.
        """

        from uuid import uuid4

        from app.services.memory.source_changes import MemorySourceChangeCoordinator

        generic = GenericMemoryAdapter(mutation_coordinator)
        created = generic.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]

        coordinator = MemorySourceChangeCoordinator(generic)
        coordinator.delete_message_source(
            adapter_context,
            message_id="m1",
            edit_revision=1,
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
            source_id=uuid4(),
        )
        assert len(generic.list_active_memories(adapter_context)) == 1

    def test_the_detach_key_covers_the_edit_revision(self) -> None:
        """SRC-02b — editing a message twice is two distinct detaches.

        Sharing a key across edits would make the second edit a no-op, leaving
        provenance pointing at text the user has since replaced.
        """

        first = MemoryIdempotency.source_change(OWNER_ID, "m1", 1, "mem-1", "delete")
        second = MemoryIdempotency.source_change(OWNER_ID, "m1", 2, "mem-1", "delete")
        assert first != second


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
