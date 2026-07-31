"""Fail-closed Phase 3 rollout policy for memory-v2 write adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings
from app.core.identifiers import canonical_uuid


class MemoryV2RolloutError(RuntimeError):
    pass


class MemoryV2WriteMode(StrEnum):
    LEGACY = "legacy"
    SCHEMA_ONLY = "schema_only"
    SHADOW = "shadow"
    CANONICAL = "canonical"


@dataclass(frozen=True)
class MemoryV2FeatureFlags:
    schema_enabled: bool = False
    shadow_mutations: bool = False
    canonical_writes: bool = False
    legacy_compatibility: bool = True
    enabled_owner_ids: frozenset[str] = frozenset()
    disposable_database_root: str = ""

    def __post_init__(self) -> None:
        owners = frozenset(canonical_uuid(owner) for owner in self.enabled_owner_ids)
        object.__setattr__(self, "enabled_owner_ids", owners)
        if (self.shadow_mutations or self.canonical_writes) and not self.schema_enabled:
            raise MemoryV2RolloutError("memory_v2_writes_require_schema")
        if self.shadow_mutations and self.canonical_writes:
            raise MemoryV2RolloutError("memory_v2_shadow_and_canonical_are_mutually_exclusive")
        if self.canonical_writes and not owners:
            raise MemoryV2RolloutError("memory_v2_canonical_writes_require_owner_allowlist")
        if (self.shadow_mutations or self.canonical_writes) and not (
            self.disposable_database_root.strip()
        ):
            raise MemoryV2RolloutError("memory_v2_mutations_require_disposable_database_root")

    @classmethod
    def from_settings(cls, settings: Settings) -> MemoryV2FeatureFlags:
        owners = frozenset(
            item.strip() for item in settings.memory_v2_enabled_owner_ids.split(",") if item.strip()
        )
        return cls(
            schema_enabled=settings.memory_v2_schema_enabled,
            shadow_mutations=settings.memory_v2_shadow_mutations,
            canonical_writes=settings.memory_v2_canonical_writes,
            legacy_compatibility=settings.memory_v2_legacy_compatibility,
            enabled_owner_ids=owners,
            disposable_database_root=settings.memory_v2_disposable_database_root,
        )

    def mode_for(self, owner_id: str) -> MemoryV2WriteMode:
        owner = canonical_uuid(owner_id)
        if not self.schema_enabled:
            return MemoryV2WriteMode.LEGACY
        if owner not in self.enabled_owner_ids:
            return MemoryV2WriteMode.SCHEMA_ONLY
        if self.canonical_writes:
            return MemoryV2WriteMode.CANONICAL
        if self.shadow_mutations:
            return MemoryV2WriteMode.SHADOW
        return MemoryV2WriteMode.SCHEMA_ONLY
