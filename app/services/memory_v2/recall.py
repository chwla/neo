"""Single canonical, lexical-only recall service for every Phase 5 consumer."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from app.models.memory_v2 import MemoryRecordV2
from app.repositories.memory_v2 import MemoryV2Repository
from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.queries import (
    CanonicalMemoryView,
    MemoryQueryContext,
    RecallCandidate,
    RecallDiagnostic,
    RecallItem,
    RecallMode,
    RecallQuery,
    RecallReasonCode,
    RecallResult,
    RecallScoreBreakdown,
)
from app.services.memory_v2.taxonomy import MemoryType

MAX_LEXICAL_CANDIDATES = 500
TOKENIZER_VERSION = "neo.memory.tokenizer.unicode-word.v1"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def lexical_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN_RE.findall(normalized))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _freshness(value: datetime, now: datetime, half_life_days: float) -> float:
    age_days = max(0.0, (_aware(now) - _aware(value)).total_seconds() / 86_400)
    return max(0.0, min(1.0, math.exp(-age_days / half_life_days)))


class CanonicalRecallService:
    """Owner-bound canonical recall with no vector or legacy dependency."""

    def __init__(
        self,
        repository: MemoryV2Repository,
        *,
        flags: MemoryV2FeatureFlags,
        minimum_scoped_score: float | None = None,
    ) -> None:
        self.repository = repository
        self.flags = flags
        self.minimum_scoped_score = (
            flags.recall_min_score if minimum_scoped_score is None else minimum_scoped_score
        )
        if not 0 <= self.minimum_scoped_score <= 1:
            raise ValueError("recall_minimum_score_out_of_range")

    def recall(self, query: RecallQuery) -> RecallResult:
        started = monotonic()
        context = query.context
        gate = self._gate(context)
        if gate is not None:
            return self._empty(context, gate, started)
        if context.mode is RecallMode.SCOPED_LEXICAL and (
            not self.flags.lexical_recall_enabled or not context.lexical_available
        ):
            return self._empty(context, RecallReasonCode.LEXICAL_UNAVAILABLE, started)

        inactive_filtered, expired_filtered = self.repository.recall_filter_counts(
            now=context.current_time,
            **self._filter_scope(query),
        )
        records = self._fetch(query)
        source_ids = self.repository.active_source_ids_for_records(
            [record.id for record in records]
        )
        views, sensitivity_filtered = self._views(records, context, source_ids)
        suppressed_ids, suppressed_slots = self._suppression(context)
        unsuppressed = [
            view
            for view in views
            if view.canonical_id not in suppressed_ids and view.slot_key not in suppressed_slots
        ]
        suppressed = tuple(
            sorted(
                {
                    view.canonical_id
                    for view in views
                    if view.canonical_id in suppressed_ids or view.slot_key in suppressed_slots
                },
                key=str,
            )
        )

        candidates, domain_filtered, below_threshold = self._score(
            query,
            unsuppressed,
        )
        selected, diversity_dropped, budget_dropped = self._select(
            candidates,
            context,
        )
        diagnostic = RecallDiagnostic(
            owner_database_binding="matched",
            recall_mode=context.mode,
            eligible_candidate_count=len(records),
            filtered_inactive_count=inactive_filtered,
            filtered_expired_count=expired_filtered,
            filtered_sensitivity_count=sensitivity_filtered,
            domain_filtered_count=domain_filtered,
            below_threshold_count=below_threshold,
            diversity_dropped_count=diversity_dropped,
            budget_dropped_count=budget_dropped,
            suppressed_ids=suppressed,
            degraded_lexical=False,
            latency_ms=int((monotonic() - started) * 1000),
            reason_codes=(),
        )
        return RecallResult(
            mode=context.mode,
            items=tuple(RecallItem(memory=item.memory, score=item.score) for item in selected),
            diagnostic=diagnostic,
        )

    def _gate(self, context: MemoryQueryContext) -> RecallReasonCode | None:
        if not context.memory_enabled:
            return RecallReasonCode.GATED_MEMORY_DISABLED
        if context.incognito:
            return RecallReasonCode.GATED_INCOGNITO
        owner = str(context.owner_id)
        if not self.flags.canonical_query_enabled or owner not in self.flags.enabled_owner_ids:
            return RecallReasonCode.OWNER_NOT_ENABLED
        if (
            self.repository.owner_id != owner
            or self.repository.database_identity != context.database_identity
        ):
            return RecallReasonCode.OWNER_DATABASE_MISMATCH
        return None

    def _empty(
        self,
        context: MemoryQueryContext,
        reason: RecallReasonCode,
        started: float,
    ) -> RecallResult:
        return RecallResult(
            mode=context.mode,
            items=(),
            diagnostic=RecallDiagnostic(
                owner_database_binding=(
                    "mismatch"
                    if reason is RecallReasonCode.OWNER_DATABASE_MISMATCH
                    else "not_queried"
                ),
                recall_mode=context.mode,
                eligible_candidate_count=0,
                degraded_lexical=reason is RecallReasonCode.LEXICAL_UNAVAILABLE,
                latency_ms=int((monotonic() - started) * 1000),
                reason_codes=(reason,),
            ),
        )

    def _fetch(self, query: RecallQuery) -> list[MemoryRecordV2]:
        context = query.context
        if context.mode is RecallMode.DETERMINISTIC:
            if query.canonical_id is not None:
                record = self.repository.get_recall_eligible_by_id(
                    str(query.canonical_id), now=context.current_time
                )
                return [record] if record is not None else []
            if query.trusted_slot_keys:
                return self.repository.list_recall_eligible_for_slots(
                    query.trusted_slot_keys,
                    now=context.current_time,
                    limit=min(100, context.maximum_records * 10),
                )
            if query.slot_key and query.memory_type and query.domain_key:
                record = self.repository.find_recall_eligible_slot(
                    now=context.current_time,
                    memory_type=query.memory_type,
                    domain_key=query.domain_key,
                    slot_key=query.slot_key,
                )
                return [record] if record is not None else []
        if context.mode is RecallMode.SCOPED_LEXICAL and not context.lexical_available:
            return []
        types: Collection[MemoryType] = context.allowed_memory_types
        domains: Collection[str] = context.allowed_domains
        if query.memory_type is not None:
            types = (query.memory_type,)
        if query.domain_key is not None:
            domains = (query.domain_key,)
        return self.repository.list_recall_eligible(
            now=context.current_time,
            memory_types=types,
            domain_keys=domains,
            limit=MAX_LEXICAL_CANDIDATES,
        )

    @staticmethod
    def _filter_scope(query: RecallQuery) -> dict[str, object]:
        context = query.context
        memory_types: Collection[MemoryType] = context.allowed_memory_types
        domains: Collection[str] = context.allowed_domains
        slots: Collection[str] = ()
        if query.memory_type is not None:
            memory_types = (query.memory_type,)
        if query.domain_key is not None:
            domains = (query.domain_key,)
        if query.trusted_slot_keys:
            slots = query.trusted_slot_keys
        elif query.slot_key:
            slots = (query.slot_key,)
        return {
            "memory_id": str(query.canonical_id) if query.canonical_id is not None else None,
            "memory_types": memory_types,
            "domain_keys": domains,
            "slot_keys": slots,
        }

    @staticmethod
    def _views(
        records: Sequence[MemoryRecordV2],
        context: MemoryQueryContext,
        source_ids: dict[str, tuple[str, ...]],
    ) -> tuple[list[CanonicalMemoryView], int]:
        views: list[CanonicalMemoryView] = []
        filtered = 0
        for record in records:
            sensitivity = Sensitivity(record.sensitivity)
            if sensitivity is Sensitivity.SENSITIVE:
                if not context.explicit_sensitive_lookup:
                    filtered += 1
                    continue
                display = "[sensitive memory]"
            else:
                display = (record.display_text or "").strip()
                if not display:
                    continue
            views.append(
                CanonicalMemoryView(
                    canonical_id=UUID(record.id),
                    owner_id=UUID(record.owner_id),
                    memory_type=MemoryType(record.memory_type),
                    domain_key=record.domain_key,
                    slot_key=record.slot_key,
                    display_text=display,
                    sensitivity=sensitivity,
                    confidence=record.confidence,
                    importance=record.importance,
                    pinned=record.pinned,
                    usage_count=record.usage_count,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    last_confirmed_at=record.last_confirmed_at,
                    last_used_at=record.last_used_at,
                    source_ids=tuple(UUID(item) for item in source_ids.get(record.id, ())),
                )
            )
        return views, filtered

    @staticmethod
    def _suppression(
        context: MemoryQueryContext,
    ) -> tuple[frozenset[UUID], frozenset[str]]:
        override = context.current_turn_override
        if override is None:
            return frozenset(), frozenset()
        return frozenset(override.suppressed_memory_ids), frozenset(override.suppressed_slot_keys)

    def _score(
        self,
        query: RecallQuery,
        views: Sequence[CanonicalMemoryView],
    ) -> tuple[list[RecallCandidate], int, int]:
        context = query.context
        if not views:
            return [], 0, 0
        lexical_enabled = self.flags.lexical_recall_enabled and context.lexical_available
        query_tokens = lexical_tokens(query.text) if lexical_enabled else ()
        documents = [lexical_tokens(view.display_text) for view in views]
        bm25 = self._bm25(query_tokens, documents)
        candidates: list[RecallCandidate] = []
        domain_filtered = 0
        below = 0
        for view, lexical in zip(views, bm25, strict=True):
            domain_fit = 1.0
            if context.allowed_domains:
                domain_fit = 1.0 if view.domain_key in context.allowed_domains else 0.0
            if context.mode is RecallMode.SCOPED_LEXICAL and domain_fit == 0:
                domain_filtered += 1
                continue
            # Freshness, confidence, importance and pinning may rank a relevant
            # record, but they may never manufacture relevance. A scoped query
            # with no lexical overlap therefore fails closed even when a record
            # is recent, important, or pinned.
            if context.mode is RecallMode.SCOPED_LEXICAL and lexical <= 0:
                below += 1
                continue
            score = self._breakdown(view, context.current_time, lexical, domain_fit)
            if (
                context.mode is RecallMode.SCOPED_LEXICAL
                and score.total < self.minimum_scoped_score
            ):
                below += 1
                continue
            candidates.append(RecallCandidate(memory=view, score=score))
        candidates.sort(
            key=lambda item: (
                -item.score.total,
                -_aware(item.memory.last_confirmed_at).timestamp(),
                -_aware(item.memory.updated_at).timestamp(),
                str(item.memory.canonical_id),
            )
        )
        return candidates, domain_filtered, below

    @staticmethod
    def _bm25(
        query_tokens: Sequence[str],
        documents: Sequence[Sequence[str]],
    ) -> list[float]:
        if not query_tokens or not documents:
            return [0.0] * len(documents)
        document_frequency = Counter(token for document in documents for token in set(document))
        average_length = sum(len(document) for document in documents) / len(documents) or 1
        raw: list[float] = []
        for document in documents:
            frequencies = Counter(document)
            score = 0.0
            for token in set(query_tokens):
                frequency = frequencies[token]
                if frequency == 0:
                    continue
                frequency_docs = document_frequency[token]
                inverse = math.log(
                    1 + (len(documents) - frequency_docs + 0.5) / (frequency_docs + 0.5)
                )
                denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(document) / average_length)
                score += inverse * frequency * 2.2 / denominator
            raw.append(score)
        return [value / (value + 1.0) for value in raw]

    @staticmethod
    def _breakdown(
        view: CanonicalMemoryView,
        now: datetime,
        lexical: float,
        domain_fit: float,
    ) -> RecallScoreBreakdown:
        importance = (view.importance - 1) / 9
        confidence = view.confidence
        confirmation = _freshness(view.last_confirmed_at, now, 180)
        recency = _freshness(view.updated_at, now, 365)
        usage = min(1.0, math.log1p(view.usage_count) / math.log(101))
        pin = 1.0 if view.pinned else 0.0
        total = min(
            1.0,
            0.25 * domain_fit
            + 0.45 * lexical
            + 0.08 * importance
            + 0.07 * confidence
            + 0.05 * confirmation
            + 0.04 * recency
            + 0.03 * usage
            + 0.03 * pin,
        )
        return RecallScoreBreakdown(
            domain_fit=domain_fit,
            lexical=lexical,
            importance=importance,
            confidence=confidence,
            confirmation_freshness=confirmation,
            recency=recency,
            usage=usage,
            pin=pin,
            total=total,
        )

    @staticmethod
    def _select(
        candidates: Sequence[RecallCandidate],
        context: MemoryQueryContext,
    ) -> tuple[list[RecallCandidate], int, int]:
        selected: list[RecallCandidate] = []
        seen_ids: set[UUID] = set()
        seen_slots: set[str] = set()
        type_counts: Counter[MemoryType] = Counter()
        domain_counts: Counter[str] = Counter()
        used_chars = 0
        diversity_dropped = 0
        budget_dropped = 0
        deferred: list[RecallCandidate] = []
        for candidate in candidates:
            view = candidate.memory
            if view.canonical_id in seen_ids or view.slot_key in seen_slots:
                diversity_dropped += 1
                continue
            if context.mode is RecallMode.BROAD and (
                type_counts[view.memory_type] >= 2 or domain_counts[view.domain_key] >= 3
            ):
                deferred.append(candidate)
                continue
            cost = len(view.display_text) + 120
            if (
                len(selected) >= context.maximum_records
                or used_chars + cost > context.maximum_characters
            ):
                budget_dropped += 1
                continue
            selected.append(candidate)
            seen_ids.add(view.canonical_id)
            seen_slots.add(view.slot_key)
            type_counts[view.memory_type] += 1
            domain_counts[view.domain_key] += 1
            used_chars += cost
        for candidate in deferred:
            view = candidate.memory
            cost = len(view.display_text) + 120
            if (
                len(selected) >= context.maximum_records
                or used_chars + cost > context.maximum_characters
            ):
                budget_dropped += 1
                continue
            if view.slot_key in seen_slots:
                diversity_dropped += 1
                continue
            selected.append(candidate)
            seen_ids.add(view.canonical_id)
            seen_slots.add(view.slot_key)
            used_chars += cost
        return selected, diversity_dropped, budget_dropped


RecallServiceFactory = Callable[[MemoryQueryContext], CanonicalRecallService]
