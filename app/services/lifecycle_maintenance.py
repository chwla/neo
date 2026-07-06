from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.models import Memory, MemoryCandidate, MemoryLifecycleAudit
from app.models.enums import CandidateStatus
from app.repositories.memory_store import MemoryStore
from app.services.lifecycle import (
    ACTIVE_STATUS,
    ARCHIVED_STATUS,
    DELETED_STATUS,
    SUPERSEDED_STATUS,
    AgingPolicy,
    MemoryLifecycleService,
    normalize_memory_text,
    tombstone_identity,
)


@dataclass(frozen=True)
class CompressionCandidate:
    group_id: str
    memory_ids: tuple[int, ...]
    reason: str
    recommended_action: str
    safe_to_auto_compress: bool
    requires_summary: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "memory_ids": list(self.memory_ids),
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "safe_to_auto_compress": self.safe_to_auto_compress,
            "requires_summary": self.requires_summary,
        }


@dataclass(frozen=True)
class AuditCheckIssue:
    check: str
    passed: bool
    memory_ids: tuple[int, ...] = ()
    recommended_fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "passed": self.passed,
            "memory_ids": list(self.memory_ids),
            "recommended_fix": self.recommended_fix,
        }


@dataclass(frozen=True)
class TombstoneReview:
    deleted_without_tombstones: tuple[int, ...] = ()
    duplicate_tombstone_groups: tuple[dict[str, Any], ...] = ()
    old_tombstones: tuple[int, ...] = ()
    broad_tombstones: tuple[int, ...] = ()
    narrow_tombstones: tuple[int, ...] = ()
    orphan_tombstones: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "deleted_without_tombstones": list(self.deleted_without_tombstones),
            "duplicate_tombstone_groups": list(self.duplicate_tombstone_groups),
            "old_tombstones": list(self.old_tombstones),
            "broad_tombstones": list(self.broad_tombstones),
            "narrow_tombstones": list(self.narrow_tombstones),
            "orphan_tombstones": list(self.orphan_tombstones),
        }


@dataclass
class MaintenanceReport:
    dry_run: bool
    max_actions: int
    aging: dict[str, Any]
    compression_candidates: list[dict[str, Any]]
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    applied_actions: list[dict[str, Any]] = field(default_factory=list)
    skipped_actions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_repair: dict[str, Any] = field(default_factory=dict)
    tombstone_review: dict[str, Any] = field(default_factory=dict)
    audit_consistency: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "max_actions": self.max_actions,
            "aging": self.aging,
            "compression_candidates": self.compression_candidates,
            "planned_actions": self.planned_actions,
            "applied_actions": self.applied_actions,
            "skipped_actions": self.skipped_actions,
            "warnings": self.warnings,
            "audit_repair": self.audit_repair,
            "tombstone_review": self.tombstone_review,
            "audit_consistency": self.audit_consistency,
        }


class MemoryLifecycleMaintenance:
    """Manual lifecycle maintenance runner with dry-run as the safe default."""

    def __init__(self, lifecycle: MemoryLifecycleService | None = None) -> None:
        self.lifecycle = lifecycle or MemoryLifecycleService()

    def run(
        self,
        store: MemoryStore,
        apply: bool = False,
        max_actions: int = 20,
        aging_policy: AgingPolicy | None = None,
        include_aging: bool = True,
        include_compression_candidates: bool = True,
        include_audit_check: bool = True,
        include_tombstone_review: bool = True,
        include_audit_repair: bool = False,
    ) -> MaintenanceReport:
        dry_run = not apply
        max_actions = max(max_actions, 0)
        aging = (
            store.age_memories(aging_policy or AgingPolicy(), dry_run=True, max_actions=max_actions)
            if include_aging
            else None
        )
        candidates = (
            self.discover_compression_candidates(store) if include_compression_candidates else []
        )
        audit_before = self.check_audit_consistency(store) if include_audit_check else []
        repair_plan = self.plan_audit_repair(store, audit_before) if include_audit_repair else []
        planned_actions = self._planned_actions(aging, candidates, repair_plan, max_actions)
        skipped_actions = self._skipped_actions(
            include_aging,
            include_compression_candidates,
            include_audit_check,
            include_tombstone_review,
            include_audit_repair,
            candidates,
            planned_actions,
        )
        warnings = self._warnings(apply, max_actions, include_audit_repair, repair_plan)
        applied_actions: list[dict[str, Any]] = []

        if apply:
            if include_aging and max_actions > 0:
                aging = store.age_memories(
                    aging_policy or AgingPolicy(),
                    dry_run=False,
                    max_actions=max_actions,
                )
                applied_actions.extend(
                    {
                        "action": action.get("action"),
                        "memory_id": action.get("memory_id"),
                        "reason": action.get("reason"),
                    }
                    for action in list(aging.actions)[:max_actions]
                )
            remaining = max(max_actions - len(applied_actions), 0)
            if include_compression_candidates and remaining:
                applied_actions.extend(
                    self.apply_safe_compression(store, candidates, max_actions=remaining),
                )
            remaining = max(max_actions - len(applied_actions), 0)
            if include_audit_repair and remaining:
                applied_actions.extend(
                    self.apply_audit_repair(store, repair_plan, max_actions=remaining),
                )

        tombstones = (
            self.review_tombstones(store) if include_tombstone_review else TombstoneReview()
        )
        audit = self.check_audit_consistency(store) if include_audit_check else []
        final_repair_plan = self.plan_audit_repair(store, audit) if include_audit_repair else []
        return MaintenanceReport(
            dry_run=dry_run,
            max_actions=max_actions,
            aging={
                "included": include_aging,
                "dry_run": True if aging is None else aging.dry_run,
                "archived": 0 if aging is None else aging.archived,
                "decayed": 0 if aging is None else aging.decayed,
                "skipped": 0 if aging is None else aging.skipped,
                "actions": [] if aging is None else list(aging.actions)[:max_actions],
            },
            compression_candidates=[candidate.to_dict() for candidate in candidates],
            planned_actions=planned_actions,
            applied_actions=applied_actions,
            skipped_actions=skipped_actions,
            warnings=warnings,
            audit_repair={
                "included": include_audit_repair,
                "planned": repair_plan,
                "remaining": final_repair_plan,
            },
            tombstone_review=tombstones.to_dict(),
            audit_consistency=[issue.to_dict() for issue in audit],
        )

    def plan_audit_repair(
        self,
        store: MemoryStore,
        issues: list[AuditCheckIssue] | None = None,
    ) -> list[dict[str, Any]]:
        issues = issues if issues is not None else self.check_audit_consistency(store)
        plans: list[dict[str, Any]] = []
        for issue in issues:
            if issue.passed:
                continue
            if issue.check == "deleted memories have delete audit":
                plans.extend(self._audit_backfill_plans(store, issue.memory_ids, "deleted"))
            elif issue.check == "archived memories have archive or compression audit":
                plans.extend(self._audit_backfill_plans(store, issue.memory_ids, "archived"))
            elif issue.check == "superseded memories have supersede audit":
                plans.extend(self._audit_backfill_plans(store, issue.memory_ids, "superseded"))
            elif issue.check == "restored memories have restore audit":
                plans.extend(self._audit_backfill_plans(store, issue.memory_ids, "restored"))
            elif issue.check == "compressed memories have compression audit":
                plans.extend(self._audit_backfill_plans(store, issue.memory_ids, "compressed"))
            elif issue.check == "resurrection blocks have audit events":
                plans.extend(self._resurrection_backfill_plans(store, issue.memory_ids))
        unique: dict[tuple[int, str, int | None], dict[str, Any]] = {}
        for plan in plans:
            key = (plan["memory_id"], plan["action"], plan.get("related_memory_id"))
            unique.setdefault(key, plan)
        return list(unique.values())

    def apply_audit_repair(
        self,
        store: MemoryStore,
        repair_plan: list[dict[str, Any]],
        max_actions: int,
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for plan in repair_plan:
            if len(applied) >= max_actions:
                break
            if self._audit_exists(
                store, plan["memory_id"], plan["action"], plan.get("related_memory_id")
            ):
                continue
            memory = store.get_memory(plan["memory_id"])
            if memory is None:
                continue
            store.record_lifecycle_audit(
                memory,
                plan["action"],
                previous_status=None,
                new_status=plan.get("new_status"),
                reason="audit_backfill",
                related_memory_id=plan.get("related_memory_id"),
                source_sentence=None,
            )
            applied.append({**plan, "reason": "audit_backfill"})
        store.db.flush()
        return applied

    def discover_compression_candidates(self, store: MemoryStore) -> list[CompressionCandidate]:
        active = store.list_memories(active_only=True, limit=100000)
        inactive = [
            memory
            for memory in store.list_memories(active_only=False, limit=100000)
            if not memory.is_active or memory.status != ACTIVE_STATUS
        ]
        candidates: list[CompressionCandidate] = []
        seen_groups: set[str] = set()

        exact_groups: dict[tuple[str, str, str], list[Memory]] = {}
        identity_groups: dict[tuple[str, str, str], list[Memory]] = {}
        slot_groups: dict[tuple[str, str], list[Memory]] = {}
        for memory in active:
            normalized = normalize_memory_text(memory.memory_text)
            exact_key = (memory.memory_type.value, memory.canonical_slot or "", normalized)
            exact_groups.setdefault(exact_key, []).append(memory)
            identity = tombstone_identity(
                memory.memory_type, memory.memory_text, memory.canonical_slot
            )
            if identity:
                identity_groups.setdefault(
                    (memory.memory_type.value, identity[0], identity[1]),
                    [],
                ).append(memory)
            if memory.canonical_slot:
                slot_groups.setdefault(
                    (memory.memory_type.value, memory.canonical_slot), []
                ).append(memory)

        for key, group in exact_groups.items():
            if len(group) < 2:
                continue
            group_id = "exact:" + ":".join(key)
            seen_groups.add(group_id)
            candidates.append(
                CompressionCandidate(
                    group_id=group_id,
                    memory_ids=tuple(memory.id for memory in group),
                    reason="Exact duplicate active memories.",
                    recommended_action="Archive duplicate copies and keep the latest active memory.",
                    safe_to_auto_compress=True,
                    requires_summary=False,
                ),
            )

        for key, group in identity_groups.items():
            if len(group) < 2:
                continue
            group_id = "slot_identity:" + ":".join(key)
            if group_id in seen_groups:
                continue
            normalized_texts = {normalize_memory_text(memory.memory_text) for memory in group}
            candidates.append(
                CompressionCandidate(
                    group_id=group_id,
                    memory_ids=tuple(memory.id for memory in group),
                    reason="Same canonical slot and normalized lifecycle identity.",
                    recommended_action="Archive older near-duplicates and keep the latest active fact.",
                    safe_to_auto_compress=len(normalized_texts) <= 3,
                    requires_summary=False,
                ),
            )

        for key, group in slot_groups.items():
            if len(group) < 3:
                continue
            group_id = "slot_cluster:" + ":".join(key)
            if any(group_id == candidate.group_id for candidate in candidates):
                continue
            candidates.append(
                CompressionCandidate(
                    group_id=group_id,
                    memory_ids=tuple(memory.id for memory in group),
                    reason="Multiple active memories share one canonical scope.",
                    recommended_action="Review for manual or LLM-written summary before compression.",
                    safe_to_auto_compress=False,
                    requires_summary=True,
                ),
            )

        replacement_groups: dict[int, list[Memory]] = {}
        for memory in inactive:
            if memory.status == SUPERSEDED_STATUS and memory.superseded_by_id:
                replacement_groups.setdefault(memory.superseded_by_id, []).append(memory)
        for replacement_id, group in replacement_groups.items():
            if len(group) < 2:
                continue
            replacement = store.get_memory(replacement_id)
            if (
                replacement is None
                or replacement.status != ACTIVE_STATUS
                or not replacement.is_active
            ):
                continue
            candidates.append(
                CompressionCandidate(
                    group_id=f"superseded_chain:{replacement_id}",
                    memory_ids=tuple(memory.id for memory in [*group, replacement]),
                    reason="Superseded chain has one active replacement.",
                    recommended_action="Keep active replacement; retain inactive history as archived audit trail.",
                    safe_to_auto_compress=True,
                    requires_summary=False,
                ),
            )

        return candidates

    def apply_safe_compression(
        self,
        store: MemoryStore,
        candidates: list[CompressionCandidate],
        max_actions: int,
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for candidate in candidates:
            if len(actions) >= max_actions:
                break
            if not candidate.safe_to_auto_compress or candidate.requires_summary:
                continue
            memories = [store.get_memory(memory_id) for memory_id in candidate.memory_ids]
            active_memories = [
                memory
                for memory in memories
                if memory is not None and memory.status == ACTIVE_STATUS and memory.is_active
            ]
            if len(active_memories) < 2:
                continue
            keeper = self._latest_memory(active_memories)
            duplicates = [memory for memory in active_memories if memory.id != keeper.id]
            for duplicate in duplicates:
                if len(actions) >= max_actions:
                    break
                previous_status = duplicate.status
                duplicate.is_active = False
                duplicate.status = ARCHIVED_STATUS
                duplicate.superseded_by_id = keeper.id
                duplicate.update_reason = "Auto-compressed safe duplicate memory."
                store._delete_memory_fts(duplicate.id)
                store._mark_embedding_stale(duplicate)
                store.record_lifecycle_audit(
                    duplicate,
                    "compressed",
                    previous_status=previous_status,
                    new_status=ARCHIVED_STATUS,
                    reason=duplicate.update_reason,
                    related_memory_id=keeper.id,
                    source_sentence=duplicate.source_sentence,
                )
                actions.append(
                    {
                        "action": "auto_compress",
                        "group_id": candidate.group_id,
                        "archived_memory_id": duplicate.id,
                        "kept_memory_id": keeper.id,
                    },
                )
        store.db.flush()
        return actions

    def check_audit_consistency(self, store: MemoryStore) -> list[AuditCheckIssue]:
        memories = store.list_memories(active_only=False, limit=100000)
        audits = list(store.db.scalars(select(MemoryLifecycleAudit)))
        actions_by_memory: dict[int, set[str]] = {}
        for audit in audits:
            actions_by_memory.setdefault(audit.memory_id, set()).add(audit.action)

        issues: list[AuditCheckIssue] = []
        issues.append(
            self._status_audit_issue(
                "deleted memories have delete audit",
                memories,
                actions_by_memory,
                DELETED_STATUS,
                "deleted",
                "Backfill or create a delete lifecycle audit record.",
            ),
        )
        issues.append(
            self._status_audit_issue(
                "archived memories have archive or compression audit",
                memories,
                actions_by_memory,
                ARCHIVED_STATUS,
                ("archived", "compressed"),
                "Backfill archive/compression lifecycle audit record.",
            ),
        )
        issues.append(
            self._status_audit_issue(
                "superseded memories have supersede audit",
                memories,
                actions_by_memory,
                SUPERSEDED_STATUS,
                "superseded",
                "Backfill supersede lifecycle audit record.",
            ),
        )
        restored_without_audit = [
            memory.id
            for memory in memories
            if memory.status == ACTIVE_STATUS
            and memory.update_reason
            and "restore" in memory.update_reason.lower()
            and "restored" not in actions_by_memory.get(memory.id, set())
        ]
        issues.append(
            AuditCheckIssue(
                "restored memories have restore audit",
                passed=not restored_without_audit,
                memory_ids=tuple(restored_without_audit),
                recommended_fix="Backfill restored lifecycle audit record.",
            ),
        )

        compressed_without_audit = [
            memory.id
            for memory in memories
            if (
                memory.source == "memory_compression"
                or (memory.update_reason and "compress" in memory.update_reason.lower())
                or memory.superseded_by_id is not None
            )
            and "compressed" not in actions_by_memory.get(memory.id, set())
        ]
        issues.append(
            AuditCheckIssue(
                "compressed memories have compression audit",
                passed=not compressed_without_audit,
                memory_ids=tuple(compressed_without_audit),
                recommended_fix="Backfill compressed lifecycle audit record.",
            ),
        )

        deleted_replacements = []
        for memory in memories:
            if memory.status == ACTIVE_STATUS and memory.superseded_by_id:
                replacement = store.get_memory(memory.superseded_by_id)
                if replacement is not None and replacement.status == DELETED_STATUS:
                    deleted_replacements.append(memory.id)
        issues.append(
            AuditCheckIssue(
                "no active memory points to deleted replacement",
                passed=not deleted_replacements,
                memory_ids=tuple(deleted_replacements),
                recommended_fix="Clear invalid replacement link or restore replacement explicitly.",
            ),
        )

        inactive_leaks = []
        for memory in memories:
            if memory.status in {DELETED_STATUS, SUPERSEDED_STATUS} and not memory.is_active:
                if any(
                    result.id == memory.id
                    for result in store.search_memories(memory.memory_text, limit=20)
                ):
                    inactive_leaks.append(memory.id)
        issues.append(
            AuditCheckIssue(
                "inactive memories are excluded from active retrieval",
                passed=not inactive_leaks,
                memory_ids=tuple(inactive_leaks),
                recommended_fix="Remove inactive memory from active indexes and check retrieval filters.",
            ),
        )

        malformed_tombstones = [
            memory.id for memory in memories if memory.status == DELETED_STATUS and memory.is_active
        ]
        issues.append(
            AuditCheckIssue(
                "tombstones exist for deleted memories",
                passed=not malformed_tombstones,
                memory_ids=tuple(malformed_tombstones),
                recommended_fix="Set deleted memory is_active=false so it acts as a tombstone.",
            ),
        )

        blocked_candidates = list(
            store.db.scalars(
                select(MemoryCandidate).where(MemoryCandidate.status == CandidateStatus.REJECTED)
            ),
        )
        missing_blocked_audit = []
        for candidate in blocked_candidates:
            reasoning = candidate.reasoning or ""
            if "resurrection" not in reasoning or "tombstone_memory_id" not in reasoning:
                continue
            memory_id = self._reasoning_memory_id(reasoning)
            if memory_id is None or "resurrection_blocked" not in actions_by_memory.get(
                memory_id, set()
            ):
                missing_blocked_audit.append(candidate.id)
        issues.append(
            AuditCheckIssue(
                "resurrection blocks have audit events",
                passed=not missing_blocked_audit,
                memory_ids=tuple(missing_blocked_audit),
                recommended_fix="Record resurrection_blocked audit on the tombstone memory.",
            ),
        )

        return issues

    def review_tombstones(
        self,
        store: MemoryStore,
        old_threshold_days: int = 365,
    ) -> TombstoneReview:
        now = datetime.now(UTC)
        deleted = [
            memory
            for memory in store.list_memories(active_only=False, limit=100000)
            if memory.status == DELETED_STATUS
        ]
        duplicate_groups: dict[tuple[str, str], list[int]] = {}
        old_tombstones = []
        broad_tombstones = []
        narrow_tombstones = []
        malformed = []

        for memory in deleted:
            if memory.is_active:
                malformed.append(memory.id)
            identity = tombstone_identity(
                memory.memory_type, memory.memory_text, memory.canonical_slot
            )
            if identity:
                duplicate_groups.setdefault(identity, []).append(memory.id)
            elif (
                memory.canonical_slot
                and len(normalize_memory_text(memory.memory_text).split()) <= 2
            ):
                broad_tombstones.append(memory.id)
            else:
                narrow_tombstones.append(memory.id)
            timestamp = memory.updated_at or memory.created_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if now - timestamp > timedelta(days=old_threshold_days):
                old_tombstones.append(memory.id)

        duplicate_reports = tuple(
            {"identity": f"{identity[0]}:{identity[1]}", "memory_ids": ids}
            for identity, ids in duplicate_groups.items()
            if len(ids) > 1
        )
        return TombstoneReview(
            deleted_without_tombstones=tuple(malformed),
            duplicate_tombstone_groups=duplicate_reports,
            old_tombstones=tuple(old_tombstones),
            broad_tombstones=tuple(broad_tombstones),
            narrow_tombstones=tuple(narrow_tombstones),
            orphan_tombstones=(),
        )

    def _planned_actions(
        self,
        aging,
        candidates: list[CompressionCandidate],
        repair_plan: list[dict[str, Any]],
        max_actions: int,
    ) -> list[dict[str, Any]]:
        planned: list[dict[str, Any]] = []
        if aging is not None:
            planned.extend(
                {
                    "action": action.get("action"),
                    "memory_id": action.get("memory_id"),
                    "reason": action.get("reason"),
                }
                for action in list(aging.actions)
            )
        for candidate in candidates:
            if candidate.safe_to_auto_compress and not candidate.requires_summary:
                planned.append(
                    {
                        "action": "safe_compression_candidate",
                        "group_id": candidate.group_id,
                        "memory_ids": list(candidate.memory_ids),
                        "reason": candidate.reason,
                    },
                )
        planned.extend(
            {
                "action": "audit_repair",
                "memory_id": plan["memory_id"],
                "audit_action": plan["action"],
                "related_memory_id": plan.get("related_memory_id"),
                "reason": "audit_backfill",
            }
            for plan in repair_plan
        )
        return planned[:max_actions]

    def _skipped_actions(
        self,
        include_aging: bool,
        include_compression_candidates: bool,
        include_audit_check: bool,
        include_tombstone_review: bool,
        include_audit_repair: bool,
        candidates: list[CompressionCandidate],
        planned_actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        skipped: list[dict[str, Any]] = []
        if not include_aging:
            skipped.append({"action": "aging", "reason": "include_aging=false"})
        if not include_compression_candidates:
            skipped.append(
                {
                    "action": "compression_candidates",
                    "reason": "include_compression_candidates=false",
                },
            )
        if not include_audit_check:
            skipped.append({"action": "audit_check", "reason": "include_audit_check=false"})
        if not include_tombstone_review:
            skipped.append(
                {"action": "tombstone_review", "reason": "include_tombstone_review=false"}
            )
        if not include_audit_repair:
            skipped.append({"action": "audit_repair", "reason": "include_audit_repair=false"})
        for candidate in candidates:
            if not candidate.safe_to_auto_compress or candidate.requires_summary:
                skipped.append(
                    {
                        "action": "compression",
                        "group_id": candidate.group_id,
                        "reason": "manual_or_llm_summary_required",
                    },
                )
        if len(planned_actions) == 0:
            skipped.append({"action": "apply", "reason": "no eligible planned actions"})
        return skipped

    def _warnings(
        self,
        apply: bool,
        max_actions: int,
        include_audit_repair: bool,
        repair_plan: list[dict[str, Any]],
    ) -> list[str]:
        warnings = []
        if not apply:
            warnings.append("dry_run_only_no_mutations")
        if max_actions <= 0:
            warnings.append("max_actions_zero_no_apply_actions_allowed")
        if include_audit_repair and repair_plan:
            warnings.append("audit_repair_uses_synthetic_audit_backfill_records_only")
        return warnings

    def _audit_backfill_plans(
        self,
        store: MemoryStore,
        memory_ids: tuple[int, ...],
        action: str,
    ) -> list[dict[str, Any]]:
        plans = []
        for memory_id in memory_ids:
            memory = store.get_memory(memory_id)
            if memory is None:
                continue
            related_memory_id = (
                memory.superseded_by_id if action in {"compressed", "superseded"} else None
            )
            if self._audit_exists(store, memory.id, action, related_memory_id):
                continue
            plans.append(
                {
                    "memory_id": memory.id,
                    "action": action,
                    "new_status": memory.status,
                    "related_memory_id": related_memory_id,
                },
            )
        return plans

    def _resurrection_backfill_plans(
        self,
        store: MemoryStore,
        candidate_ids: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        plans = []
        for candidate_id in candidate_ids:
            candidate = store.get_candidate(candidate_id)
            if candidate is None:
                continue
            memory_id = self._reasoning_memory_id(candidate.reasoning or "")
            if memory_id is None:
                continue
            memory = store.get_memory(memory_id)
            if memory is None:
                continue
            if self._audit_exists(store, memory.id, "resurrection_blocked", None):
                continue
            plans.append(
                {
                    "memory_id": memory.id,
                    "action": "resurrection_blocked",
                    "new_status": memory.status,
                    "related_memory_id": None,
                },
            )
        return plans

    def _audit_exists(
        self,
        store: MemoryStore,
        memory_id: int,
        action: str,
        related_memory_id: int | None,
    ) -> bool:
        stmt = select(MemoryLifecycleAudit).where(
            MemoryLifecycleAudit.memory_id == memory_id,
            MemoryLifecycleAudit.action == action,
        )
        if related_memory_id is not None:
            stmt = stmt.where(MemoryLifecycleAudit.related_memory_id == related_memory_id)
        return store.db.scalars(stmt).first() is not None

    def _latest_memory(self, memories: list[Memory]) -> Memory:
        return sorted(
            memories,
            key=lambda memory: (
                memory.updated_at or memory.created_at,
                memory.importance,
                memory.id,
            ),
            reverse=True,
        )[0]

    def _status_audit_issue(
        self,
        check: str,
        memories: list[Memory],
        actions_by_memory: dict[int, set[str]],
        status: str,
        expected_action: str | tuple[str, ...],
        recommended_fix: str,
    ) -> AuditCheckIssue:
        expected_actions = (
            {expected_action} if isinstance(expected_action, str) else set(expected_action)
        )
        missing = [
            memory.id
            for memory in memories
            if memory.status == status
            and actions_by_memory.get(memory.id, set()).isdisjoint(expected_actions)
        ]
        return AuditCheckIssue(
            check=check,
            passed=not missing,
            memory_ids=tuple(missing),
            recommended_fix=recommended_fix,
        )

    def _reasoning_memory_id(self, reasoning: str) -> int | None:
        marker = '"tombstone_memory_id"'
        if marker not in reasoning:
            return None
        try:
            import json

            payload = json.loads(reasoning)
            value = payload.get("tombstone_memory_id")
            if isinstance(value, int):
                return value
        except Exception:
            return None
        return None
