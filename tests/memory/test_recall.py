"""Tier 5 — canonical recall (plan section RCL).

Recall is the part of the memory layer the user actually experiences.  Storage
being correct is invisible; recall being wrong is the assistant forgetting your
name or bringing up something you asked it to forget.

Two design rules run through these tests.  First, relevance cannot be
manufactured: freshness, importance and pinning may reorder a record that
already matched, but they may never promote one that did not.  Second, the one
enumerated exception is the core identity slots — "who am i" shares no word with
"Soham", so a purely lexical gate would hide exactly the facts a personal
assistant most needs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.engine import Engine

from app.services.memory.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory.policy import USAGE_AFFECTS_RANKING
from app.services.memory.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallQuery,
    RecallReasonCode,
)
from app.services.memory.recall import (
    CORE_IDENTITY_SLOT_KEYS,
    CanonicalRecallService,
    lexical_tokens,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory import factories
from tests.memory.conftest import FROZEN_NOW, OWNER_ID

SKETCHING = "improve at urban sketching"


def _context(tmp_path, **overrides) -> MemoryQueryContext:
    base = {
        "owner_id": OWNER_ID,
        "database_identity": str(tmp_path / "memory.db"),
        "profile_id": "profile-1",
        "request_id": "request-1",
        "current_time": FROZEN_NOW,
        "mode": RecallMode.SCOPED_LEXICAL,
    }
    base.update(overrides)
    return MemoryQueryContext(**base)


def _query(tmp_path, text: str = "", **overrides) -> RecallQuery:
    context_overrides = overrides.pop("context", {})
    return RecallQuery(context=_context(tmp_path, **context_overrides), text=text, **overrides)


class TestGating:
    def test_incognito_returns_nothing(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-01 — nothing is read in incognito, not merely nothing shown."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "sketching", context={"incognito": True}))
        assert result.items == ()
        assert RecallReasonCode.GATED_INCOGNITO in result.diagnostic.reason_codes

    def test_disabled_memory_returns_nothing(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-02"""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"memory_enabled": False})
        )
        assert result.items == ()
        assert RecallReasonCode.GATED_MEMORY_DISABLED in result.diagnostic.reason_codes

    def test_a_gated_query_reports_zero_eligible_candidates(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-04b — the gate short-circuits before any query is issued."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "sketching", context={"incognito": True}))
        assert result.diagnostic.eligible_candidate_count == 0


class TestEligibilityFiltering:
    def test_an_inactive_record_is_excluded_and_counted(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-05"""

        factories.insert_record(
            engine, display_text=SKETCHING, status=MemoryLifecycleState.ARCHIVED
        )
        result = recall_service.recall(_query(tmp_path, "sketching"))
        assert result.items == ()
        assert result.diagnostic.filtered_inactive_count == 1

    def test_an_expired_record_is_excluded_and_counted(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-06"""

        factories.insert_record(
            engine, display_text=SKETCHING, expires_at=FROZEN_NOW - timedelta(days=1)
        )
        result = recall_service.recall(_query(tmp_path, "sketching"))
        assert result.items == ()
        assert result.diagnostic.filtered_expired_count == 1

    def test_a_forgotten_record_never_comes_back(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-05b / PRV-08 — the promise "forget" makes to the user."""

        factories.insert_record(
            engine, display_text=SKETCHING, status=MemoryLifecycleState.FORGOTTEN
        )
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert result.items == ()

    def test_a_sensitive_record_is_excluded_from_ordinary_recall(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-07 — sensitive facts surface when asked for, not as ambient context."""

        factories.insert_record(
            engine,
            display_text=None,
            sensitivity=Sensitivity.SENSITIVE,
            canonical_payload=None,
            encrypted_canonical_payload=b"c",
            encrypted_display_payload=b"c",
            encryption_algorithm="aes-256-gcm",
            encryption_key_version="v1",
            canonical_nonce=b"n",
            display_nonce=b"n",
            encryption_aad=b"a",
        )
        result = recall_service.recall(_query(tmp_path, "asthma"))
        assert result.items == ()

    def test_a_domain_filter_excludes_and_counts(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-10"""

        factories.insert_record(engine, display_text=SKETCHING, domain_key="global")
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"allowed_domains": frozenset({"learning"})})
        )
        assert result.items == ()
        # The domain filter is applied in SQL, so the excluded record never
        # reaches the scoring stage where ``domain_filtered_count`` is
        # incremented.  The record is correctly excluded; the counter simply
        # describes a different, later filter. Pinned so the distinction is
        # explicit rather than looking like a miscount.
        assert result.diagnostic.eligible_candidate_count == 0
        assert result.diagnostic.domain_filtered_count == 0

    def test_a_matching_domain_is_kept(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-10b"""

        factories.insert_record(engine, display_text=SKETCHING, domain_key="global")
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"allowed_domains": frozenset({"global"})})
        )
        assert len(result.items) == 1


class TestProjectScope:
    def test_a_project_memory_is_invisible_outside_its_project(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-12 — the guarantee that project notes stay in their project."""

        factories.insert_record(
            engine,
            display_text=SKETCHING,
            scope_type="project",
            scope_project_id="alpha",
        )
        assert recall_service.recall(_query(tmp_path, "sketching")).items == ()

    def test_a_project_memory_is_visible_inside_its_project(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-12b"""

        factories.insert_record(
            engine,
            display_text=SKETCHING,
            scope_type="project",
            scope_project_id="alpha",
        )
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"active_project_id": "alpha"})
        )
        assert len(result.items) == 1

    def test_a_project_memory_is_invisible_from_another_project(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-12c"""

        factories.insert_record(
            engine,
            display_text=SKETCHING,
            scope_type="project",
            scope_project_id="alpha",
        )
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"active_project_id": "beta"})
        )
        assert result.items == ()

    def test_a_global_memory_is_visible_inside_a_project(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-13 — personal facts stay readable from anywhere."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(
            _query(tmp_path, "sketching", context={"active_project_id": "alpha"})
        )
        assert len(result.items) == 1


class TestRelevanceCannotBeManufactured:
    """The central rule: ranking signals reorder matches, they do not create them."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("importance", 10),
            ("confidence", 1.0),
            ("pinned", True),
            ("usage_count", 99),
        ],
    )
    def test_no_ranking_signal_promotes_an_unrelated_memory(
        self,
        recall_service: CanonicalRecallService,
        engine: Engine,
        tmp_path,
        field: str,
        value: object,
    ) -> None:
        """RCL-28 / RCL-30 — a scoped query with no overlap fails closed."""

        factories.insert_record(
            engine, display_text="my favourite pasta shape is rigatoni", **{field: value}
        )
        result = recall_service.recall(_query(tmp_path, "tell me about quantum computing"))
        assert result.items == ()

    def test_a_pinned_record_still_loses_to_a_better_match(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-29 — pinning is a nudge, not a guarantee of first place."""

        factories.insert_record(
            engine,
            display_text="sketching supplies",
            pinned=True,
            slot_key="knowledge:global:item:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )
        factories.insert_record(
            engine,
            display_text="improve at urban sketching with watercolour and fineliner",
            slot_key="goal:global:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        result = recall_service.recall(
            _query(tmp_path, "urban sketching watercolour fineliner improve")
        )
        assert result.items
        assert "watercolour" in result.items[0].memory.display_text

    def test_a_pinned_record_is_still_filtered_by_status(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-30b — the pin policy declares it bypasses nothing; prove it."""

        factories.insert_record(
            engine,
            display_text=SKETCHING,
            pinned=True,
            status=MemoryLifecycleState.ARCHIVED,
        )
        assert recall_service.recall(_query(tmp_path, "urban sketching")).items == ()

    def test_a_pinned_record_is_still_filtered_by_expiry(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-30c"""

        factories.insert_record(
            engine,
            display_text=SKETCHING,
            pinned=True,
            expires_at=FROZEN_NOW - timedelta(days=1),
        )
        assert recall_service.recall(_query(tmp_path, "urban sketching")).items == ()


class TestCoreIdentity:
    @pytest.mark.parametrize("slot_key", sorted(CORE_IDENTITY_SLOT_KEYS))
    def test_a_core_identity_slot_is_reachable_without_word_overlap(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path, slot_key: str
    ) -> None:
        """RCL-19 — "who am i" shares no token with "Soham"."""

        factories.insert_record(
            engine,
            display_text="Soham",
            memory_type=MemoryType.IDENTITY,
            domain_key="global",
            slot_key=slot_key,
            cardinality=Cardinality.EXCLUSIVE,
        )
        result = recall_service.recall(_query(tmp_path, "who am i"))
        assert len(result.items) == 1

    def test_an_ordinary_memory_cannot_enter_through_the_identity_exception(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-19b — the set is enumerated, not derived, exactly so this holds."""

        factories.insert_record(
            engine,
            display_text="Soham",
            memory_type=MemoryType.IDENTITY,
            domain_key="global",
            slot_key="identity:global:favourite_colour",
            cardinality=Cardinality.EXCLUSIVE,
        )
        assert recall_service.recall(_query(tmp_path, "who am i")).items == ()

    def test_the_identity_set_stays_small(self) -> None:
        """RCL-19c — every addition here widens what bypasses the lexical gate."""

        assert len(CORE_IDENTITY_SLOT_KEYS) <= 8
        assert all(key.startswith("identity:global:") for key in CORE_IDENTITY_SLOT_KEYS)


class TestTokenisation:
    def test_tokens_are_lowercased_and_split_on_non_alphanumerics(self) -> None:
        """RCL-20"""

        assert lexical_tokens("Urban-Sketching, 2026!") == ("urban", "sketch", "2026")

    def test_unicode_is_normalised(self) -> None:
        """RCL-20b"""

        assert lexical_tokens("ｓｋｅｔｃｈｉｎｇ") == lexical_tokens("sketching")

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("podcasting", "podcasts"),
            ("stories", "story"),
            ("sketched", "sketching"),
            ("walked", "walking"),
        ],
    )
    def test_inflections_of_one_word_share_a_stem(self, first: str, second: str) -> None:
        """RCL-21 — the regression: "podcasting" did not match "podcasts"."""

        assert lexical_tokens(first) == lexical_tokens(second)

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("sketching", "sketches"),
            ("boxing", "boxes"),
            ("wishing", "wishes"),
            ("running", "runs"),
            ("swimming", "swims"),
        ],
    )
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known gap: the stemmer strips a bare 's' but not the 'es' plural, and "
            "does not undouble a final consonant. 'sketches' becomes 'sketche' and "
            "'running' becomes 'runn', neither of which matches 'sketch' or 'run'. "
            "Remove this xfail when -es and doubled-consonant forms are handled."
        ),
    )
    def test_es_plurals_and_doubled_consonants_currently_miss(
        self, first: str, second: str
    ) -> None:
        """RCL-21d — a gap found while writing RCL-21, recorded not patched.

        ``_stem`` documents itself as making "the singular, plural and participle
        forms of the same word agree", and promises that "any word it does not
        recognise is left exactly as written".  Neither quite holds:

        - ``sketches`` loses only the trailing ``s``, giving ``sketche`` — which
          matches neither ``sketch`` nor ``sketching``.  Same for boxes, wishes,
          classes: every ``-es`` plural.
        - ``running`` loses ``ing`` without undoubling, giving ``runn`` — which
          does not match ``runs`` or ``run``.

        These words are not left as written; they are transformed into a form
        that matches nothing at all, which is the same failure mode as the
        original "podcasting did not match podcasts" bug this stemmer was added
        to fix. It matters here specifically: this app's own example domain is
        urban sketching, and "show me my sketches" does not reach a memory that
        says "sketching".
        """

        assert lexical_tokens(first) == lexical_tokens(second)

    @pytest.mark.parametrize("token", ["cat", "runs", "2026", "id"])
    def test_short_tokens_and_digits_are_left_alone(self, token: str) -> None:
        """RCL-21b — the stemmer is deliberately small and fails safe."""

        assert lexical_tokens(token) == (token.casefold(),)

    def test_stemming_is_idempotent(self) -> None:
        """RCL-21c — stemming a stem must not keep eroding the word."""

        once = lexical_tokens("sketching")
        assert lexical_tokens(" ".join(once)) == once

    def test_an_empty_string_produces_no_tokens(self) -> None:
        """RCL-23b"""

        assert lexical_tokens("") == ()


class TestScoring:
    def test_a_term_match_outranks_a_non_match(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-22"""

        factories.insert_record(engine, display_text="improve at urban sketching")
        factories.insert_record(
            engine,
            display_text="learn to bake sourdough bread",
            slot_key="goal:global:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert len(result.items) == 1
        assert "sketching" in result.items[0].memory.display_text

    def test_bm25_handles_an_empty_corpus(self) -> None:
        """RCL-23 — no dividing by zero on a cold store."""

        assert CanonicalRecallService._bm25(("sketching",), []) == []

    def test_bm25_handles_an_empty_query(self) -> None:
        """RCL-23c"""

        assert CanonicalRecallService._bm25((), [("sketching",)]) == [0.0]

    def test_bm25_saturates(self) -> None:
        """RCL-24 — a term repeated fifty times must not dominate the ranking."""

        once = CanonicalRecallService._bm25(("sketch",), [("sketch",)])[0]
        many = CanonicalRecallService._bm25(("sketch",), [("sketch",) * 50])[0]
        assert many < 1.0
        assert many / max(once, 1e-9) < 5

    @pytest.mark.parametrize("scores_field", ["lexical", "importance", "confidence", "total"])
    def test_every_score_component_stays_within_zero_and_one(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path, scores_field: str
    ) -> None:
        """RCL-32"""

        factories.insert_record(engine, display_text=SKETCHING, importance=10, confidence=1.0)
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert result.items
        value = getattr(result.items[0].score, scores_field)
        assert 0.0 <= value <= 1.0

    def test_scoring_is_deterministic(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-33 — two identical queries must score identically."""

        factories.insert_record(engine, display_text=SKETCHING)
        first = recall_service.recall(_query(tmp_path, "urban sketching"))
        second = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert [item.score.total for item in first.items] == [
            item.score.total for item in second.items
        ]

    def test_ties_break_deterministically(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-34 — never by row order, or results shuffle between runs."""

        for index in range(4):
            factories.insert_record(
                engine,
                display_text="improve at urban sketching",
                canonical_fingerprint=f"sha256:{index}" + "0" * 63,
                slot_key=f"goal:global:independent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}",
            )
        first = recall_service.recall(_query(tmp_path, "urban sketching"))
        second = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert first.canonical_ids == second.canonical_ids


class TestUsageRanking:
    def test_the_policy_constant_says_usage_does_not_affect_ranking(self) -> None:
        """RCL-31a — the declared intent."""

        assert USAGE_AFFECTS_RANKING is False

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Contradiction: policy declares USAGE_AFFECTS_RANKING = False, but "
            "_breakdown gives usage a 0.03 weight in the lexical total, so a "
            "frequently-recalled memory does outrank an identical unused one. "
            "The constant is referenced nowhere else in the codebase. Remove "
            "this xfail once the two are reconciled."
        ),
    )
    def test_usage_does_not_change_the_score(self) -> None:
        """RCL-31b — a contradiction found while writing RCL-31.

        ``policy.USAGE_AFFECTS_RANKING = False`` reads as a deliberate product
        decision, and a good one: ranking by usage creates a feedback loop where
        memories that were recalled get recalled more, regardless of whether they
        were relevant.  But the constant is declared and never read, and
        ``_breakdown`` includes ``0.03 * usage`` in the lexical total.

        Measured, the gap between an unused record and one used a hundred times
        is about 0.03 of total score — small, but enough to reorder near-ties,
        which is exactly where ranking decisions actually get made.

        Either the scorer should drop the usage term or the constant should say
        ``True``; right now the code and its stated policy disagree.
        """

        from datetime import UTC, datetime
        from uuid import uuid4

        from app.services.memory.queries import CanonicalMemoryView

        now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

        def _view(usage: int) -> CanonicalMemoryView:
            return CanonicalMemoryView(
                canonical_id=uuid4(),
                owner_id=uuid4(),
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key="goal:global:independent:x",
                display_text=SKETCHING,
                sensitivity=Sensitivity.NORMAL,
                confidence=0.9,
                importance=5,
                pinned=False,
                usage_count=usage,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )

        unused = CanonicalRecallService._breakdown(_view(0), now, 0.5, 1.0)
        heavily_used = CanonicalRecallService._breakdown(_view(100), now, 0.5, 1.0)
        assert unused.total == heavily_used.total

    def test_the_size_of_the_usage_effect_is_bounded(self) -> None:
        """RCL-31c — pinning how large the contradiction actually is.

        Whatever the resolution, it is worth knowing the effect is small: usage
        can move a total score by at most the 0.03 weight it carries.
        """

        from datetime import UTC, datetime
        from uuid import uuid4

        from app.services.memory.queries import CanonicalMemoryView

        now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

        def _view(usage: int) -> CanonicalMemoryView:
            return CanonicalMemoryView(
                canonical_id=uuid4(),
                owner_id=uuid4(),
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key="goal:global:independent:x",
                display_text=SKETCHING,
                sensitivity=Sensitivity.NORMAL,
                confidence=0.9,
                importance=5,
                pinned=False,
                usage_count=usage,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )

        delta = (
            CanonicalRecallService._breakdown(_view(100), now, 0.5, 1.0).total
            - CanonicalRecallService._breakdown(_view(0), now, 0.5, 1.0).total
        )
        assert 0 < delta <= 0.031


class TestSelectionLimits:
    def test_the_record_limit_is_honoured(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-36"""

        for index in range(8):
            factories.insert_record(
                engine,
                display_text=f"improve at urban sketching technique {index}",
                canonical_fingerprint=f"sha256:{index}" + "0" * 63,
                slot_key=f"goal:global:independent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}",
            )
        result = recall_service.recall(
            _query(tmp_path, "urban sketching", context={"maximum_records": 3})
        )
        assert len(result.items) <= 3

    def test_the_character_budget_is_honoured(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-37 / PRF-05"""

        for index in range(6):
            factories.insert_record(
                engine,
                display_text="urban sketching " + ("detail " * 60) + str(index),
                canonical_fingerprint=f"sha256:{index}" + "0" * 63,
                slot_key=f"goal:global:independent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{index}",
            )
        result = recall_service.recall(
            _query(tmp_path, "urban sketching", context={"maximum_characters": 400})
        )
        total = sum(len(item.memory.display_text) for item in result.items)
        assert total <= 400

    def test_an_empty_store_returns_an_empty_result(
        self, recall_service: CanonicalRecallService, tmp_path
    ) -> None:
        """RCL-41 — a cold start must answer cleanly rather than error."""

        result = recall_service.recall(_query(tmp_path, "anything at all"))
        assert result.items == ()
        assert result.diagnostic.eligible_candidate_count == 0


class TestDiagnostics:
    def test_the_diagnostic_counts_are_internally_consistent(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-42 — every eligible record is either selected or in a drop bucket."""

        factories.insert_record(engine, display_text=SKETCHING)
        factories.insert_record(
            engine,
            display_text="learn to bake sourdough",
            slot_key="goal:global:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        diagnostic = result.diagnostic
        accounted = (
            len(result.items)
            + diagnostic.below_threshold_count
            + diagnostic.domain_filtered_count
            + diagnostic.diversity_dropped_count
            + diagnostic.budget_dropped_count
            + len(diagnostic.suppressed_ids)
        )
        assert accounted == diagnostic.eligible_candidate_count

    def test_the_latency_is_reported(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-43"""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert result.diagnostic.latency_ms >= 0

    def test_the_selected_ids_match_the_returned_items(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-44 — the diagnostic must describe what was actually injected."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        # ``final_injected_ids`` is filled in by the prompt orchestrator when it
        # records usage, not by recall itself: recall selects, the orchestrator
        # decides what actually reaches the prompt. Pinned so an empty tuple
        # here is not mistaken for a lost selection.
        assert result.diagnostic.final_injected_ids == ()
        assert len(result.canonical_ids) == 1

    def test_the_owner_binding_is_reported(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-04c — which database answered is part of the audit trail."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert result.diagnostic.owner_database_binding


class TestDeterministicMode:
    def test_deterministic_mode_requires_a_selector(self, tmp_path) -> None:
        """RCL-15b / QRY-04 — an unscoped deterministic query is a caller bug."""

        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="deterministic_recall_selector_required"):
            RecallQuery(context=_context(tmp_path, mode=RecallMode.DETERMINISTIC), text="")

    def test_a_slot_selector_returns_the_slot_occupant(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-16"""

        factories.insert_record(
            engine,
            display_text="Soham",
            memory_type=MemoryType.IDENTITY,
            slot_key="identity:global:name",
            cardinality=Cardinality.EXCLUSIVE,
        )
        result = recall_service.recall(
            RecallQuery(
                context=_context(tmp_path, mode=RecallMode.DETERMINISTIC),
                slot_key="identity:global:name",
            )
        )
        assert len(result.items) == 1
        assert result.items[0].memory.display_text == "Soham"

    def test_a_memory_type_selector_returns_that_type_only(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-18"""

        factories.insert_record(engine, display_text=SKETCHING, memory_type=MemoryType.GOAL)
        factories.insert_record(
            engine,
            display_text="a stored fact",
            memory_type=MemoryType.KNOWLEDGE,
            slot_key="knowledge:global:item:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        result = recall_service.recall(
            RecallQuery(
                context=_context(tmp_path, mode=RecallMode.DETERMINISTIC),
                memory_type=MemoryType.GOAL,
            )
        )
        assert [item.memory.memory_type for item in result.items] == [MemoryType.GOAL]

    def test_a_deterministic_query_still_respects_the_owner_gate(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-15c — a selector is not an override."""

        factories.insert_record(
            engine,
            display_text="Soham",
            memory_type=MemoryType.IDENTITY,
            slot_key="identity:global:name",
            cardinality=Cardinality.EXCLUSIVE,
        )
        result = recall_service.recall(
            RecallQuery(
                context=_context(tmp_path, mode=RecallMode.DETERMINISTIC, incognito=True),
                slot_key="identity:global:name",
            )
        )
        assert result.items == ()


class TestBroadMode:
    def test_broad_mode_returns_records_without_word_overlap(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-14b — "what do you remember about me" is not a lexical question."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(
            _query(tmp_path, "what do you remember", context={"mode": RecallMode.BROAD})
        )
        assert len(result.items) == 1

    def test_broad_mode_still_excludes_inactive_records(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-14c — a broader question is not a weaker filter."""

        factories.insert_record(
            engine, display_text=SKETCHING, status=MemoryLifecycleState.FORGOTTEN
        )
        result = recall_service.recall(
            _query(tmp_path, "what do you remember", context={"mode": RecallMode.BROAD})
        )
        assert result.items == ()


class TestServiceConfiguration:
    @pytest.mark.parametrize("score", [-0.1, 1.1])
    def test_an_out_of_range_minimum_score_is_refused(
        self, session, tmp_path, memory_settings, score: float
    ) -> None:
        """RCL-35b"""

        from app.repositories.memory import MemoryRepository

        repository = MemoryRepository(
            session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )
        with pytest.raises(ValueError, match="recall_minimum_score_out_of_range"):
            CanonicalRecallService(repository, flags=memory_settings, minimum_scoped_score=score)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("semantic_weight", 1.1),
            ("semantic_cap", -0.1),
            ("semantic_threshold", 2.0),
        ],
    )
    def test_out_of_range_semantic_configuration_is_refused(
        self, session, tmp_path, memory_settings, field: str, value: float
    ) -> None:
        """RCL-52b"""

        from app.repositories.memory import MemoryRepository

        repository = MemoryRepository(
            session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )
        with pytest.raises(ValueError, match="semantic_score_configuration_out_of_range"):
            CanonicalRecallService(repository, flags=memory_settings, **{field: value})

    @pytest.mark.parametrize("limit", [0, 501])
    def test_an_out_of_range_candidate_limit_is_refused(
        self, session, tmp_path, memory_settings, limit: int
    ) -> None:
        """RCL-14d"""

        from app.repositories.memory import MemoryRepository

        repository = MemoryRepository(
            session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )
        with pytest.raises(ValueError, match="derived_candidate_limit_out_of_range"):
            CanonicalRecallService(repository, flags=memory_settings, vector_candidate_limit=limit)


class TestSemanticUnavailable:
    def test_recall_works_with_no_semantic_provider(
        self, recall_service: CanonicalRecallService, engine: Engine, tmp_path
    ) -> None:
        """RCL-45 / RCL-46 — the lexical path must stand alone."""

        factories.insert_record(engine, display_text=SKETCHING)
        result = recall_service.recall(_query(tmp_path, "urban sketching"))
        assert len(result.items) == 1
        assert result.diagnostic.semantic_candidate_count == 0
