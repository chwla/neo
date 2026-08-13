"""Tier 5 — the recall contracts (plan section QRY).

`queries.py` is the boundary every recall consumer crosses.  It carries almost no
logic, which is the point: the invariants are declared as field bounds and two
model validators, so a caller cannot construct a nonsensical request in the first
place.

Two of those validators are security-relevant rather than merely tidy.  An
override belonging to a different owner would let one profile's turn suppress
another's memories, and `explicit_sensitive_lookup` is the flag that unlocks
SENSITIVE records — allowing it outside deterministic mode would mean a fuzzy
lexical match could surface a diagnosis.  Both are tested as refusals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import CurrentTurnOverride
from app.services.memory.queries import (
    CanonicalMemoryView,
    MemoryQueryContext,
    RecallDiagnostic,
    RecallItem,
    RecallMode,
    RecallQuery,
    RecallResult,
    RecallScoreBreakdown,
    SerializedMemoryContext,
    UsageSelection,
)
from app.services.memory.taxonomy import MemoryType

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER = uuid4()


def _context(**overrides) -> MemoryQueryContext:
    base = {
        "owner_id": OWNER,
        "database_identity": "/tmp/memory.db",
        "profile_id": "profile-1",
        "request_id": "request-1",
        "current_time": NOW,
        "mode": RecallMode.SCOPED_LEXICAL,
    }
    base.update(overrides)
    return MemoryQueryContext(**base)


def _view(**overrides) -> CanonicalMemoryView:
    base = {
        "canonical_id": uuid4(),
        "owner_id": OWNER,
        "memory_type": MemoryType.IDENTITY,
        "domain_key": "global",
        "slot_key": "identity:global:name",
        "display_text": "Soham",
        "sensitivity": Sensitivity.NORMAL,
        "confidence": 0.9,
        "importance": 5,
        "pinned": False,
        "usage_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "last_confirmed_at": NOW,
    }
    base.update(overrides)
    return CanonicalMemoryView(**base)


def _score(**overrides) -> RecallScoreBreakdown:
    base = {
        "domain_fit": 1.0,
        "lexical": 1.0,
        "importance": 0.5,
        "confidence": 0.9,
        "confirmation_freshness": 1.0,
        "recency": 1.0,
        "usage": 0.0,
        "pin": 0.0,
        "total": 0.9,
    }
    base.update(overrides)
    return RecallScoreBreakdown(**base)


class TestQueryContextValidators:
    def test_an_override_from_another_owner_is_rejected(self) -> None:
        """QRY-01 — one profile's turn must not suppress another's memories.

        The override carries suppression lists.  Accepting one bound to a
        different owner would let a turn in profile A hide records in profile B,
        which is a cross-owner leak in the one direction nobody checks for —
        *hiding* data rather than exposing it.
        """

        override = CurrentTurnOverride(owner_id=str(uuid4()), source_message_id="m1")

        with pytest.raises(ValidationError, match="current_turn_override_owner_mismatch"):
            _context(current_turn_override=override)

    def test_a_matching_override_is_accepted(self) -> None:
        """The validator distinguishes on owner, rather than refusing all overrides."""

        override = CurrentTurnOverride(owner_id=str(OWNER), source_message_id="m1")

        context = _context(current_turn_override=override)

        assert context.current_turn_override is override

    @pytest.mark.parametrize(
        "mode",
        [RecallMode.SCOPED_LEXICAL, RecallMode.BROAD],
    )
    def test_sensitive_lookup_outside_deterministic_mode_is_rejected(
        self, mode: RecallMode
    ) -> None:
        """QRY-02 — the flag that unlocks SENSITIVE records is mode-bound.

        Deterministic mode selects by id or slot: the caller already knows
        exactly which record it wants.  In a scoring mode, a fuzzy lexical match
        decides, and a diagnosis could surface because it happened to share a
        word with the question.
        """

        with pytest.raises(ValidationError, match="sensitive_lookup_requires_deterministic_mode"):
            _context(mode=mode, explicit_sensitive_lookup=True)

    def test_sensitive_lookup_is_allowed_in_deterministic_mode(self) -> None:
        context = _context(
            mode=RecallMode.DETERMINISTIC,
            explicit_sensitive_lookup=True,
        )

        assert context.explicit_sensitive_lookup is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("maximum_records", 0),
            ("maximum_records", 21),
            ("maximum_characters", 199),
            ("maximum_characters", 12_001),
        ],
    )
    def test_budget_bounds_are_enforced(self, field: str, value: int) -> None:
        """QRY-03 — a budget outside these bounds is a prompt that breaks something."""

        with pytest.raises(ValidationError):
            _context(**{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("maximum_records", 1),
            ("maximum_records", 20),
            ("maximum_characters", 200),
            ("maximum_characters", 12_000),
        ],
    )
    def test_budget_extremes_are_accepted(self, field: str, value: int) -> None:
        """Both ends are inclusive — pinned so a `gt` typo for `ge` is caught."""

        assert getattr(_context(**{field: value}), field) == value


class TestRecallQuerySelectors:
    def test_deterministic_mode_requires_a_selector(self) -> None:
        """QRY-04 (already covered in test_recall.py; asserted here at the contract)."""

        with pytest.raises(ValidationError, match="deterministic_recall_selector_required"):
            RecallQuery(context=_context(mode=RecallMode.DETERMINISTIC))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("canonical_id", uuid4()),
            ("slot_key", "identity:global:name"),
            ("trusted_slot_keys", ("identity:global:name",)),
            ("memory_type", MemoryType.IDENTITY),
        ],
    )
    def test_each_selector_kind_satisfies_the_requirement_alone(
        self, field: str, value: object
    ) -> None:
        """QRY-05 — four selectors, each sufficient on its own.

        Parametrised rather than combined: a test passing all four at once would
        still pass if three of them stopped counting.
        """

        query = RecallQuery(context=_context(mode=RecallMode.DETERMINISTIC), **{field: value})

        assert getattr(query, field) == value

    def test_a_scoring_mode_needs_no_selector(self) -> None:
        """The requirement is specific to deterministic mode."""

        assert RecallQuery(context=_context(mode=RecallMode.SCOPED_LEXICAL)).canonical_id is None

    def test_trusted_slot_keys_are_capped(self) -> None:
        """QRY-06 — the cap bounds the deterministic fan-out."""

        with pytest.raises(ValidationError):
            RecallQuery(
                context=_context(mode=RecallMode.DETERMINISTIC),
                trusted_slot_keys=tuple(f"identity:global:s{index}" for index in range(51)),
            )

    def test_exactly_fifty_trusted_slot_keys_are_accepted(self) -> None:
        query = RecallQuery(
            context=_context(mode=RecallMode.DETERMINISTIC),
            trusted_slot_keys=tuple(f"identity:global:s{index}" for index in range(50)),
        )

        assert len(query.trusted_slot_keys) == 50


class TestScoreBreakdown:
    @pytest.mark.parametrize(
        "component",
        [
            "domain_fit",
            "lexical",
            "semantic",
            "importance",
            "confidence",
            "confirmation_freshness",
            "recency",
            "usage",
            "pin",
            "total",
        ],
    )
    @pytest.mark.parametrize("value", [-0.01, 1.01])
    def test_every_component_is_bounded_to_zero_one(
        self, component: str, value: float
    ) -> None:
        """QRY-07 — enumerated over the fields, so a new component is covered too.

        The bound is what makes the components comparable and the total
        interpretable; an unbounded component would silently dominate ranking.
        """

        with pytest.raises(ValidationError):
            _score(**{component: value})

    def test_the_bounds_are_inclusive(self) -> None:
        assert _score(lexical=0.0, total=1.0).total == 1.0


class TestRecallResult:
    def test_canonical_ids_match_the_items_in_order(self) -> None:
        """QRY-08 — usage accounting reads this property, so order is load-bearing."""

        views = [_view(display_text=str(index)) for index in range(3)]
        result = RecallResult(
            mode=RecallMode.SCOPED_LEXICAL,
            items=tuple(RecallItem(memory=view, score=_score()) for view in views),
            diagnostic=RecallDiagnostic(
                owner_database_binding="db",
                recall_mode=RecallMode.SCOPED_LEXICAL,
                eligible_candidate_count=3,
            ),
        )

        assert result.canonical_ids == tuple(view.canonical_id for view in views)

    def test_an_empty_result_has_no_canonical_ids(self) -> None:
        result = RecallResult(
            mode=RecallMode.SCOPED_LEXICAL,
            items=(),
            diagnostic=RecallDiagnostic(
                owner_database_binding="db",
                recall_mode=RecallMode.SCOPED_LEXICAL,
                eligible_candidate_count=0,
            ),
        )

        assert result.canonical_ids == ()


class TestContractModelBehaviour:
    """QRY-09 — every model forbids extras and is frozen.

    Enumerated over the models rather than asserted on one representative: these
    cross a process boundary, and a model that silently accepted an unknown field
    would drop it without telling anyone.
    """

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            (MemoryQueryContext, {}),
            (RecallScoreBreakdown, {}),
            (UsageSelection, {}),
            (SerializedMemoryContext, {}),
        ],
        ids=["MemoryQueryContext", "RecallScoreBreakdown", "UsageSelection", "SerializedMemoryContext"],
    )
    def test_unknown_fields_are_rejected(self, model: type, kwargs: dict) -> None:
        builders = {
            MemoryQueryContext: lambda: _context(unexpected_field="x"),
            RecallScoreBreakdown: lambda: _score(unexpected_field="x"),
            UsageSelection: lambda: UsageSelection(
                owner_id=OWNER,
                request_id="r1",
                purpose="chat",
                canonical_ids=(),
                selected_at=NOW,
                unexpected_field="x",
            ),
            SerializedMemoryContext: lambda: SerializedMemoryContext(
                content="x",
                canonical_ids=(),
                character_count=1,
                unexpected_field="x",
            ),
        }

        with pytest.raises(ValidationError):
            builders[model]()

    def test_models_are_frozen(self) -> None:
        context = _context()

        with pytest.raises(ValidationError):
            context.maximum_records = 10

    def test_the_serialized_message_identity_is_fixed(self) -> None:
        """The role and name are `Literal`s, so a caller cannot relabel the block.

        The whole containment story depends on this message arriving as untrusted
        user context under a known name; letting a caller change either would
        undo it silently.
        """

        with pytest.raises(ValidationError):
            SerializedMemoryContext(
                content="x",
                canonical_ids=(),
                character_count=1,
                name="something_else",
            )
