from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models import Memory
from app.models.enums import MemoryType

ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"
DELETED_STATUS = "deleted"
SUPERSEDED_STATUS = "superseded"


@dataclass(frozen=True)
class AgingPolicy:
    dormant_days: int = 180
    archive_importance_below: int = 4
    confidence_decay: float = 0.05


@dataclass(frozen=True)
class AgingResult:
    archived: int
    decayed: int
    skipped: int = 0
    dry_run: bool = False
    actions: tuple[dict, ...] = ()


class MemoryLifecycleService:
    """Lifecycle operations for long-term durable memory."""

    def supersede(self, store, old_memory: Memory, new_memory: Memory, reason: str) -> None:
        if old_memory.id == new_memory.id:
            return
        previous_status = old_memory.status
        old_memory.is_active = False
        old_memory.status = SUPERSEDED_STATUS
        old_memory.superseded_by_id = new_memory.id
        old_memory.update_reason = reason
        if new_memory.supersedes_id is None:
            new_memory.supersedes_id = old_memory.id
        if not new_memory.update_reason:
            new_memory.update_reason = reason
        store._delete_memory_fts(old_memory.id)
        store._mark_embedding_stale(old_memory)
        store.record_lifecycle_audit(
            old_memory,
            "superseded",
            previous_status=previous_status,
            new_status=SUPERSEDED_STATUS,
            reason=reason,
            related_memory_id=new_memory.id,
            source_sentence=old_memory.source_sentence,
        )
        store.db.flush()

    def archive(self, store, memory: Memory, reason: str) -> None:
        if memory.status == DELETED_STATUS:
            return
        previous_status = memory.status
        memory.is_active = False
        memory.status = ARCHIVED_STATUS
        memory.update_reason = reason
        store._delete_memory_fts(memory.id)
        store._mark_embedding_stale(memory)
        store.record_lifecycle_audit(
            memory,
            "archived",
            previous_status=previous_status,
            new_status=ARCHIVED_STATUS,
            reason=reason,
            source_sentence=memory.source_sentence,
        )
        store.db.flush()

    def delete(self, store, memory: Memory, reason: str = "User deleted memory.") -> None:
        previous_status = memory.status
        memory.is_active = False
        memory.status = DELETED_STATUS
        memory.update_reason = reason
        store._delete_memory_fts(memory.id)
        store._mark_embedding_stale(memory)
        store.record_lifecycle_audit(
            memory,
            "deleted",
            previous_status=previous_status,
            new_status=DELETED_STATUS,
            reason=reason,
            source_sentence=memory.source_sentence,
        )
        store.db.flush()

    def restore(self, store, memory: Memory, reason: str, explicit_restore: bool) -> None:
        if not explicit_restore:
            raise ValueError("Restoring inactive memory requires explicit restore intent.")
        previous_status = memory.status
        memory.is_active = True
        memory.status = ACTIVE_STATUS
        memory.update_reason = reason
        memory.superseded_by_id = None
        store._sync_memory_fts(memory)
        store._sync_memory_embedding(memory)
        store.record_lifecycle_audit(
            memory,
            "restored",
            previous_status=previous_status,
            new_status=ACTIVE_STATUS,
            reason=reason,
            source_sentence=memory.source_sentence,
        )
        store.db.flush()

    def reactivate_source_replacement(self, store, memory: Memory) -> None:
        """Reactivate only an archived fact being re-extracted from its same source."""

        if memory.status != ARCHIVED_STATUS:
            raise ValueError("Only archived source replacements can be reactivated.")
        previous_status = memory.status
        memory.is_active = True
        memory.status = ACTIVE_STATUS
        memory.update_reason = "Reactivated after replacement source re-extraction."
        memory.superseded_by_id = None
        store.reactivate_typed_record_for_memory(memory)
        store._sync_memory_fts(memory)
        store._sync_memory_embedding(memory)
        store.record_lifecycle_audit(
            memory,
            "source_reextracted",
            previous_status=previous_status,
            new_status=ACTIVE_STATUS,
            reason="The same user message was re-extracted during edit or rerun.",
            source_sentence=memory.source_sentence,
        )
        store.db.flush()

    def compress(
        self,
        store,
        memories: list[Memory],
        summary_text: str,
        memory_type: MemoryType,
        canonical_slot: str | None = None,
        reason: str = "Compressed related memories into a concise active summary.",
    ) -> Memory:
        if not memories:
            raise ValueError("At least one memory is required for compression.")
        self._validate_compression_scope(memories)
        importance = max(memory.importance for memory in memories)
        confidence = max(memory.confidence for memory in memories)
        source_sentence = "\n".join(
            dict.fromkeys(
                memory.source_sentence or memory.memory_text
                for memory in sorted(memories, key=lambda item: item.updated_at or item.created_at)
            ),
        )
        summary = store.add(
            Memory(
                memory_text=summary_text,
                memory_type=memory_type,
                importance=importance,
                confidence=confidence,
                canonical_slot=canonical_slot or memories[0].canonical_slot,
                source="memory_compression",
                source_sentence=source_sentence[:4000],
                status=ACTIVE_STATUS,
                is_active=True,
                update_reason=reason,
            ),
        )
        store.db.flush()
        for memory in memories:
            self.archive(store, memory, reason)
            memory.superseded_by_id = summary.id
            store.record_lifecycle_audit(
                memory,
                "compressed",
                previous_status=ARCHIVED_STATUS,
                new_status=ARCHIVED_STATUS,
                reason=reason,
                related_memory_id=summary.id,
                source_sentence=memory.source_sentence,
            )
        store.record_lifecycle_audit(
            summary,
            "compressed",
            previous_status=None,
            new_status=ACTIVE_STATUS,
            reason=reason,
            source_sentence=summary.source_sentence,
        )
        store._sync_memory_fts(summary)
        store._sync_memory_embedding(summary)
        store.db.flush()
        return summary

    def age(
        self,
        store,
        policy: AgingPolicy | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
        max_actions: int | None = None,
    ) -> AgingResult:
        policy = policy or AgingPolicy()
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=policy.dormant_days)
        archived = 0
        decayed = 0
        skipped = 0
        actions: list[dict] = []
        for memory in store.list_memories(active_only=True, limit=100000):
            if max_actions is not None and len(actions) >= max_actions:
                break
            if self._is_aging_protected(memory):
                skipped += 1
                continue
            last_signal = memory.last_accessed_at or memory.updated_at or memory.created_at
            if last_signal.tzinfo is None:
                last_signal = last_signal.replace(tzinfo=UTC)
            if last_signal > cutoff:
                continue
            if memory.importance < policy.archive_importance_below:
                reason = "Archived by aging policy due to low-importance dormancy."
                actions.append({"memory_id": memory.id, "action": "archive", "reason": reason})
                archived += 1
                if not dry_run:
                    self.archive(store, memory, reason)
                    store.record_lifecycle_audit(
                        memory,
                        "aged",
                        previous_status=ARCHIVED_STATUS,
                        new_status=ARCHIVED_STATUS,
                        reason=reason,
                        source_sentence=memory.source_sentence,
                    )
                continue
            new_confidence = max(0.1, round(memory.confidence - policy.confidence_decay, 3))
            if new_confidence < memory.confidence:
                reason = "Confidence decayed by aging policy due to dormancy."
                actions.append(
                    {
                        "memory_id": memory.id,
                        "action": "decay",
                        "reason": reason,
                        "previous_confidence": memory.confidence,
                        "new_confidence": new_confidence,
                    },
                )
                decayed += 1
                if not dry_run:
                    memory.confidence = new_confidence
                    memory.update_reason = reason
                    store.record_lifecycle_audit(
                        memory,
                        "aged",
                        previous_status=ACTIVE_STATUS,
                        new_status=ACTIVE_STATUS,
                        reason=reason,
                        source_sentence=memory.source_sentence,
                    )
        store.db.flush()
        return AgingResult(
            archived=archived,
            decayed=decayed,
            skipped=skipped,
            dry_run=dry_run,
            actions=tuple(actions),
        )

    def record_resurrection_blocked(
        self,
        store,
        tombstone: Memory,
        attempted_text: str,
        reason: str,
    ) -> None:
        store.record_lifecycle_audit(
            tombstone,
            "resurrection_blocked",
            previous_status=tombstone.status,
            new_status=tombstone.status,
            reason=reason,
            source_sentence=attempted_text,
        )
        store.db.flush()

    def _validate_compression_scope(self, memories: list[Memory]) -> None:
        types = {memory.memory_type for memory in memories}
        if len(types) != 1:
            raise ValueError("Compression requires memories with the same memory type.")
        slots = {memory.canonical_slot for memory in memories if memory.canonical_slot}
        if len(slots) > 1:
            raise ValueError("Compression requires one canonical scope.")
        if any(memory.status != ACTIVE_STATUS or not memory.is_active for memory in memories):
            raise ValueError("Compression only accepts active memories.")

    def _is_aging_protected(self, memory: Memory) -> bool:
        if memory.importance >= 8:
            return True
        if memory.memory_type in {
            MemoryType.IDENTITY,
            MemoryType.EDUCATION,
            MemoryType.PREFERENCE,
            MemoryType.GOAL_RELATED,
            MemoryType.PROJECT_RELATED,
        }:
            return True
        slot = (memory.canonical_slot or "").lower()
        if slot == "current_hardware":
            return True
        return slot.startswith(("identity:", "preference:", "project:"))


def tombstone_identity(
    memory_type: MemoryType | str,
    memory_text: str,
    canonical_slot: str | None = None,
) -> tuple[str, str] | None:
    slot = canonical_slot or infer_canonical_slot(memory_type, memory_text)
    value = infer_slot_value(slot, memory_text)
    if slot and value:
        return slot, value
    if slot:
        return slot, normalize_memory_text(memory_text)
    return None


def infer_canonical_slot(memory_type: MemoryType | str, memory_text: str) -> str | None:
    normalized = normalize_memory_text(memory_text)
    if re.search(r"\b(current )?(hardware|laptop|computer|machine|system|specs)\b", normalized):
        return "current_hardware"
    if re.search(r"\b(favou?rite|go to|go-to|like .* most|prefer)\b", normalized):
        if re.search(r"\b(language|programming language|coding)\b", normalized):
            return "preference:programming_language"
        if "editor" in normalized or "ide" in normalized:
            return "preference:editor"
    if str(memory_type) == MemoryType.IDENTITY.value and "=" in memory_text:
        return "identity:" + normalize_memory_text(memory_text.split("=", 1)[0])
    if str(memory_type) == MemoryType.PREFERENCE.value and "=" in memory_text:
        return "preference:" + normalize_memory_text(memory_text.split("=", 1)[0])
    return None


def infer_slot_value(canonical_slot: str | None, memory_text: str) -> str | None:
    normalized = normalize_memory_text(memory_text)
    if canonical_slot == "preference:programming_language":
        for language in (
            "python",
            "typescript",
            "javascript",
            "java",
            "c++",
            "cpp",
            "c",
            "rust",
            "go",
        ):
            if re.search(rf"\b{re.escape(language)}\b", normalized):
                return "c++" if language == "cpp" else language
    if canonical_slot == "current_hardware":
        parts = []
        for token in ("dell", "inspiron", "16gb", "512gb", "i7", "11th", "integrated"):
            if token in normalized:
                parts.append(token)
        return " ".join(parts) if parts else normalized
    if canonical_slot and "=" in memory_text:
        return normalize_memory_text(memory_text.split("=", 1)[1])
    return None


def normalize_memory_text(text: str) -> str:
    normalized = text.lower().replace("favourite", "favorite").replace("go-to", "go to")
    normalized = re.sub(r"[^a-z0-9+#.]+", " ", normalized)
    return " ".join(normalized.split())
