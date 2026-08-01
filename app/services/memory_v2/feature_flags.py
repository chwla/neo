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
    extraction_enabled: bool = False
    foreground_commands_enabled: bool = False
    post_turn_extraction_enabled: bool = False
    live_extraction_model_enabled: bool = False
    extraction_provider: str = ""
    extraction_endpoint: str = ""
    extraction_model: str = ""
    extraction_timeout_seconds: int = 120
    extraction_connect_timeout_seconds: int = 5
    extraction_response_timeout_seconds: int = 120
    extraction_warmup_timeout_seconds: int = 300
    ollama_request_mode: str = "auto"
    extraction_max_input_chars: int = 12_000

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
        phase4_subfeature = (
            self.foreground_commands_enabled
            or self.post_turn_extraction_enabled
            or self.live_extraction_model_enabled
        )
        if phase4_subfeature and not self.extraction_enabled:
            raise MemoryV2RolloutError("memory_v2_extraction_subfeatures_require_extraction")
        if self.extraction_enabled and not (self.schema_enabled and self.canonical_writes):
            raise MemoryV2RolloutError("memory_v2_extraction_requires_canonical_schema_writes")
        if self.live_extraction_model_enabled and not self.extraction_endpoint.strip():
            raise MemoryV2RolloutError("memory_v2_live_extraction_requires_endpoint")
        if self.live_extraction_model_enabled and self.extraction_provider not in {
            "direct_json",
            "ollama",
        }:
            raise MemoryV2RolloutError("memory_v2_live_extraction_requires_explicit_provider")
        if not 1 <= self.extraction_timeout_seconds <= 600:
            raise MemoryV2RolloutError("memory_v2_extraction_timeout_out_of_range")
        if not 1 <= self.extraction_connect_timeout_seconds <= 60:
            raise MemoryV2RolloutError("memory_v2_extraction_connect_timeout_out_of_range")
        if not 1 <= self.extraction_response_timeout_seconds <= 600:
            raise MemoryV2RolloutError("memory_v2_extraction_response_timeout_out_of_range")
        if not 1 <= self.extraction_warmup_timeout_seconds <= 900:
            raise MemoryV2RolloutError("memory_v2_extraction_warmup_timeout_out_of_range")
        if self.ollama_request_mode not in {"auto", "ollama_schema", "ollama_json"}:
            raise MemoryV2RolloutError("memory_v2_ollama_request_mode_invalid")
        if not 500 <= self.extraction_max_input_chars <= 50_000:
            raise MemoryV2RolloutError("memory_v2_extraction_input_limit_out_of_range")

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
            extraction_enabled=settings.memory_v2_extraction_enabled,
            foreground_commands_enabled=settings.memory_v2_foreground_commands_enabled,
            post_turn_extraction_enabled=settings.memory_v2_post_turn_extraction_enabled,
            live_extraction_model_enabled=settings.memory_v2_live_extraction_model_enabled,
            extraction_provider=settings.memory_v2_extraction_provider,
            extraction_endpoint=settings.memory_v2_extraction_endpoint,
            extraction_model=settings.memory_v2_extraction_model,
            extraction_timeout_seconds=settings.memory_v2_extraction_timeout_seconds,
            extraction_connect_timeout_seconds=(
                settings.memory_v2_extraction_connect_timeout_seconds
            ),
            extraction_response_timeout_seconds=(
                settings.memory_v2_extraction_response_timeout_seconds
            ),
            extraction_warmup_timeout_seconds=(
                settings.memory_v2_extraction_warmup_timeout_seconds
            ),
            ollama_request_mode=settings.memory_v2_ollama_request_mode,
            extraction_max_input_chars=settings.memory_v2_extraction_max_input_chars,
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
