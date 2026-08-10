from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

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
from app.services.memory.extraction import (
    OllamaRequestMode,
    build_extraction_model_provider,
    probe_ollama_provider,
)
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


_verified_memory_schemas: set[tuple[str, str, str]] = set()
_verified_memory_schemas_lock = Lock()

_EXTRACTION_LOG = logging.getLogger("neo.memory.extraction")
_negotiated_ollama_modes: dict[tuple[str, str], tuple[OllamaRequestMode, object | None]] = {}
_negotiated_ollama_lock = Lock()


def _resolve_ollama_request_mode(
    settings: MemorySettings,
) -> tuple[OllamaRequestMode, object | None]:
    """Choose the request format the configured Ollama build actually accepts.

    ``auto`` previously collapsed to ``ollama_schema`` and the capability probe
    was never run, so a server that rejects JSON-schema ``format`` (returning
    HTTP 400 ``failed to parse grammar``) failed *every* model-backed extraction
    silently.  Probe once per endpoint/model and reuse the answer; an explicitly
    configured mode is still honoured exactly as written.
    """

    configured = settings.ollama_request_mode
    if configured == "ollama_json":
        return OllamaRequestMode.JSON, None
    if configured == "ollama_schema":
        return OllamaRequestMode.SCHEMA, None
    if settings.extraction_provider != "ollama":
        return OllamaRequestMode.SCHEMA, None

    key = (settings.extraction_endpoint, settings.extraction_model)
    cached = _negotiated_ollama_modes.get(key)
    if cached is not None:
        return cached
    with _negotiated_ollama_lock:
        existing = _negotiated_ollama_modes.get(key)
        if existing is not None:
            return existing
        resolved: tuple[OllamaRequestMode, object | None]
        try:
            probe = probe_ollama_provider(
                settings.extraction_endpoint,
                model=settings.extraction_model,
                requested_mode=OllamaRequestMode.AUTO,
                connect_timeout_seconds=settings.extraction_connect_timeout_seconds,
                response_timeout_seconds=settings.extraction_response_timeout_seconds,
                warmup_timeout_seconds=settings.extraction_warmup_timeout_seconds,
            )
        except Exception:
            # An unreachable provider must not permanently pin a mode, so this
            # result is deliberately not cached.
            _EXTRACTION_LOG.exception("memory_extraction_capability_probe_failed")
            return OllamaRequestMode.JSON, None
        if probe.selected_request_mode is None:
            _EXTRACTION_LOG.warning(
                "memory_extraction_capability_probe_inconclusive code=%s",
                probe.sanitized_failure_code,
            )
            resolved = (OllamaRequestMode.JSON, None)
        else:
            resolved = (probe.selected_request_mode, probe.capabilities)
            _EXTRACTION_LOG.info(
                "memory_extraction_mode_negotiated mode=%s schema=%s json=%s",
                probe.selected_request_mode.value,
                probe.capabilities.schema_format_supported,
                probe.capabilities.json_format_supported,
            )
        _negotiated_ollama_modes[key] = resolved
    return resolved


def _ensure_memory_schema(database_url: str, owner_id: str, database_identity: str) -> None:
    """Run the memory migration check once per process for each profile database.

    A runtime is built several times per chat turn, and each build otherwise
    created an engine and opened a write-capable connection to the very database
    the chat worker is writing to.  The schema cannot change underneath a running
    process, so verifying it once removes both the latency and that contention.
    The key includes the owner binding, so a database reached with a different
    identity is still validated rather than silently accepted.
    """

    key = (database_url, owner_id, database_identity)
    if key in _verified_memory_schemas:
        return
    with _verified_memory_schemas_lock:
        if key in _verified_memory_schemas:
            return
        engine = build_engine(database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner_id,
                database_identity=database_identity,
            )
        finally:
            engine.dispose()
        _verified_memory_schemas.add(key)


def build_memory_runtime(profile: dict) -> MemoryRuntime:
    profile_id = str(profile["id"])
    guest = bool(profile.get("is_guest"))
    settings = MemorySettings.from_settings(get_settings())
    database_url = database_url_for(profile_id, guest=guest)
    owner_id = str(profile["owner_id"])
    database_identity = database_identity_for_profile(profile_id, guest=guest)
    _ensure_memory_schema(database_url, owner_id, database_identity)
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
        request_mode, capabilities = _resolve_ollama_request_mode(settings)
        model = build_extraction_model_provider(
            settings.extraction_provider,
            settings.extraction_endpoint,
            model=settings.extraction_model,
            connect_timeout_seconds=settings.extraction_connect_timeout_seconds,
            response_timeout_seconds=settings.extraction_response_timeout_seconds,
            ollama_request_mode=request_mode,
            ollama_capabilities=capabilities,
        )
    extraction = MemoryExtractionCoordinator(chat, model=model)
    return MemoryRuntime(profile, settings, coordinator, generic, chat, extraction)
