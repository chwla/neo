from __future__ import annotations

from app.models import Memory, Preference, ProfileFact
from app.models.enums import MemoryType
from app.repositories.memory_store import MemoryStore
from app.services.lifecycle import MemoryLifecycleService


class ConflictResolutionService:
    """Resolve contradictions without deleting historical records."""

    def __init__(self, lifecycle: MemoryLifecycleService | None = None) -> None:
        self.lifecycle = lifecycle or MemoryLifecycleService()

    def supersede_profile_key(self, store: MemoryStore, new_fact: ProfileFact) -> None:
        for old_fact in store.active_profile_by_key(new_fact.key):
            if old_fact.id != new_fact.id and old_fact.value != new_fact.value:
                old_fact.is_active = False

    def supersede_preference_category(self, store: MemoryStore, new_preference: Preference) -> None:
        """Backward-compatible resolver for records without a canonical preference slot."""
        for old_preference in store.active_preferences_by_category(new_preference.category):
            is_different_record = old_preference.id != new_preference.id
            has_different_value = old_preference.value != new_preference.value
            if is_different_record and has_different_value:
                old_preference.is_active = False

    def supersede_preference_slot(self, store: MemoryStore, new_preference: Preference) -> None:
        """Supersede only a conflicting stance on the same preference subject."""
        canonical_slot = (new_preference.canonical_slot or "").strip()
        if not canonical_slot:
            self.supersede_preference_category(store, new_preference)
            return
        for old_preference in store.list_preferences():
            if (
                old_preference.is_active
                and old_preference.id != new_preference.id
                and old_preference.canonical_slot == canonical_slot
                and old_preference.value != new_preference.value
            ):
                old_preference.is_active = False

    def supersede_similar_memory(self, store: MemoryStore, new_memory: Memory) -> None:
        for old_memory in store.active_memories_by_type(new_memory.memory_type):
            if old_memory.id == new_memory.id:
                continue
            if self._conflicts(old_memory, new_memory):
                self.lifecycle.supersede(
                    store,
                    old_memory,
                    new_memory,
                    "Superseded by conflicting accepted memory.",
                )

    def _conflicts(self, old_memory: Memory, new_memory: Memory) -> bool:
        if old_memory.memory_type == MemoryType.PREFERENCE:
            if old_memory.canonical_slot and new_memory.canonical_slot:
                return old_memory.canonical_slot == new_memory.canonical_slot
            return self._prefix(old_memory.memory_text) == self._prefix(new_memory.memory_text)
        if old_memory.memory_type == MemoryType.IDENTITY:
            return self._prefix(old_memory.memory_text) == self._prefix(new_memory.memory_text)
        return old_memory.memory_text.strip().lower() == new_memory.memory_text.strip().lower()

    def _prefix(self, text: str) -> str:
        return text.split("=", maxsplit=1)[0].strip().lower()
