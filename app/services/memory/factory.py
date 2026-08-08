from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.config import get_settings
from app.db.memory_migrations import upgrade_memory
from app.db.session import build_engine
from app.services.memory.adapters import (
    ChatMemoryAdapter,
    GenericMemoryAdapter,
    MemoryAdapterContext,
)
from app.services.memory.contracts import ActorKind, EvidenceSpan, SourceKind
from app.services.memory.coordinator import MemoryExecutionContext, MemoryMutationCoordinator
from app.services.memory.extraction import OllamaRequestMode, build_extraction_model_provider
from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
from app.services.memory.local_crypto import LocalMemoryCrypto
from app.services.memory.settings import MemorySettings
from app.services.profile_accounts import (
    database_identity_for_profile,
    database_url_for,
    memory_key_material_for_profile,
)


@dataclass(frozen=True)
class MemoryRuntime:
    profile: dict
    settings: MemorySettings
    coordinator: MemoryMutationCoordinator
    adapter: GenericMemoryAdapter
    chat_adapter: ChatMemoryAdapter
    extraction: MemoryExtractionCoordinator

    @property
    def execution(self) -> MemoryExecutionContext:
        profile_id = str(self.profile["id"])
        guest = bool(self.profile.get("is_guest"))
        return MemoryExecutionContext(
            owner_id=str(self.profile["owner_id"]),
            database_identity=database_identity_for_profile(profile_id, guest=guest),
            database_url=database_url_for(profile_id, guest=guest),
            profile_id=profile_id,
            is_guest=guest,
            is_incognito=bool(self.profile.get("incognito")),
            memory_enabled=self.settings.enabled,
        )

    def context(
        self,
        *,
        source_kind: SourceKind,
        source_id: str | None,
        request_id: str,
        conversation_id: str | None = None,
        message_id: str | None = None,
        session_id: str | None = None,
        observed_at: datetime | None = None,
        evidence: tuple[EvidenceSpan, ...] = (),
    ) -> MemoryAdapterContext:
        return MemoryAdapterContext(
            execution=self.execution,
            actor_kind=ActorKind.USER,
            actor_id=str(self.profile["id"]),
            source_kind=source_kind,
            source_id=source_id,
            request_id=request_id,
            session_id=session_id,
            conversation_id=conversation_id,
            message_id=message_id,
            observed_at=observed_at,
            evidence=evidence,
        )


def build_memory_runtime(profile: dict) -> MemoryRuntime:
    profile_id = str(profile["id"])
    guest = bool(profile.get("is_guest"))
    settings = MemorySettings.from_settings(get_settings())
    database_url = database_url_for(profile_id, guest=guest)
    engine = build_engine(database_url)
    try:
        upgrade_memory(
            engine,
            owner_id=str(profile["owner_id"]),
            database_identity=database_identity_for_profile(profile_id, guest=guest),
        )
    finally:
        engine.dispose()
    crypto = LocalMemoryCrypto(seed=memory_key_material_for_profile(profile_id, guest=guest))
    coordinator = MemoryMutationCoordinator(
        flags=settings,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )
    generic = GenericMemoryAdapter(coordinator)
    chat = ChatMemoryAdapter(coordinator)
    model = None
    if settings.extraction_enabled and settings.live_extraction_model_enabled:
        request_mode = (
            OllamaRequestMode.JSON
            if settings.ollama_request_mode == "ollama_json"
            else OllamaRequestMode.SCHEMA
        )
        model = build_extraction_model_provider(
            settings.extraction_provider,
            settings.extraction_endpoint,
            model=settings.extraction_model,
            connect_timeout_seconds=settings.extraction_connect_timeout_seconds,
            response_timeout_seconds=settings.extraction_response_timeout_seconds,
            ollama_request_mode=request_mode,
        )
    extraction = MemoryExtractionCoordinator(chat, model=model)
    return MemoryRuntime(profile, settings, coordinator, generic, chat, extraction)
