"""Authenticated owner-scoped controls for reconstructible memory derived state."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.accounts import session_for
from app.core.config import Settings, get_settings
from app.db.memory_migrations import memory_migration_state
from app.db.session import build_engine
from app.services.embeddings import OllamaEmbeddingProvider, ValidatedMemoryEmbeddingProvider
from app.services.memory.index_contracts import RetryPolicy
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.maintenance import MemoryIndexMaintenance
from app.services.memory.metrics import MemoryDerivedMetrics
from app.services.memory.outbox import MemoryOutboxProcessor
from app.services.memory.settings import MemoryConfigurationError, MemorySettings
from app.services.profile_accounts import database_identity_for_profile, database_url_for

router = APIRouter(prefix="/memory", tags=["memory"])

_UUID_TOKEN_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RECONCILIATION_CHECKPOINT_PATTERN = (
    rf"^(?:{_UUID_TOKEN_PATTERN}|v1:"
    rf"(?:-|!|{_UUID_TOKEN_PATTERN}):"
    rf"(?:-|!|{_UUID_TOKEN_PATTERN}):"
    rf"(?:-|!|{_UUID_TOKEN_PATTERN}))$"
)


class ReconciliationControl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dry_run: bool = False
    limit: int | None = Field(default=None, ge=1, le=1_000)
    checkpoint: str | None = Field(
        default=None,
        max_length=128,
        pattern=_RECONCILIATION_CHECKPOINT_PATTERN,
    )


def _authorized_profile(request: Request) -> tuple[dict, Settings, MemorySettings]:
    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="authenticated_profile_required")
    try:
        settings = get_settings()
        flags = MemorySettings.from_settings(settings)
    except (MemoryConfigurationError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="memory_configuration_invalid") from exc
    owner_id = str(profile.get("owner_id") or "")
    if not flags.health_routes_enabled or not flags.owner_is_enabled(owner_id):
        raise HTTPException(status_code=404, detail="memory_health_routes_disabled")
    return profile, settings, flags


def _maintenance_for_profile(profile: dict, settings: Settings):
    profile_id = str(profile["id"])
    owner_id = str(profile["owner_id"])
    is_guest = bool(profile.get("is_guest"))
    identity = database_identity_for_profile(profile_id, guest=is_guest)
    engine = build_engine(database_url_for(profile_id, guest=is_guest))
    try:
        state = memory_migration_state(engine)
        if state.owner_id != owner_id or state.database_identity != identity:
            raise RuntimeError("memory_owner_binding_mismatch")
        fts = SqliteMemoryFtsIndex(engine)
        vector = SqliteMemoryVectorIndex(engine)
        if settings.memory_embedding_provider != "ollama":
            raise RuntimeError("unsupported_memory_embedding_provider")
        provider = ValidatedMemoryEmbeddingProvider(
            OllamaEmbeddingProvider(
                model_name=settings.memory_embedding_model,
                base_url=settings.ollama_url,
                timeout=settings.memory_index_embedding_timeout_seconds,
            ),
            dimension=settings.memory_embedding_dimension,
            provider_version=settings.memory_embedding_version,
            cooldown_seconds=settings.memory_provider_cooldown_seconds,
        )
        processor = MemoryOutboxProcessor(
            engine,
            owner_id=owner_id,
            database_identity=identity,
            fts_index=fts,
            vector_index=vector,
            embedding_provider=provider,
            retry_policy=RetryPolicy(
                maximum_attempts=settings.memory_retry_max_attempts,
                dead_letter_threshold=settings.memory_dead_letter_threshold,
                base_delay_seconds=settings.memory_retry_base_seconds,
                maximum_delay_seconds=settings.memory_retry_max_seconds,
                jitter_seconds=settings.memory_retry_jitter_seconds,
                lease_seconds=settings.memory_worker_lease_seconds,
                batch_size=settings.memory_worker_batch_size,
            ),
        )
        metrics = MemoryDerivedMetrics(
            engine,
            owner_id=owner_id,
            database_identity=identity,
        )
        maintenance = MemoryIndexMaintenance.from_settings(
            engine,
            settings=settings,
            owner_id=owner_id,
            database_identity=identity,
            fts_index=fts,
            vector_index=vector,
            repair_scheduler=processor.schedule_repair,
            embedding_provider=provider,
            provider_health=provider.health,
            metric_reader=metrics.snapshot,
        )
        return maintenance, engine
    except Exception:
        engine.dispose()
        raise


@router.get("/health")
def derived_health(request: Request) -> dict[str, object]:
    profile, settings, _flags = _authorized_profile(request)
    try:
        maintenance, engine = _maintenance_for_profile(profile, settings)
        try:
            return maintenance.coverage().model_dump(mode="json")
        finally:
            engine.dispose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory_health_health_unavailable") from exc


@router.post("/derived/reconcile")
def reconcile_derived(
    request: Request,
    control: ReconciliationControl,
) -> dict[str, object]:
    profile, settings, flags = _authorized_profile(request)
    if not flags.reconciliation_enabled:
        raise HTTPException(status_code=404, detail="memory_reconciliation_disabled")
    try:
        maintenance, engine = _maintenance_for_profile(profile, settings)
        try:
            return maintenance.reconcile(
                dry_run=control.dry_run,
                limit=control.limit,
                checkpoint=control.checkpoint,
            ).model_dump(mode="json")
        finally:
            engine.dispose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory_reconciliation_unavailable") from exc


@router.post("/derived/rebuild")
def rebuild_derived(request: Request) -> dict[str, object]:
    profile, settings, flags = _authorized_profile(request)
    if not flags.reconciliation_enabled:
        raise HTTPException(status_code=404, detail="memory_reconciliation_disabled")
    try:
        maintenance, engine = _maintenance_for_profile(profile, settings)
        try:
            return maintenance.rebuild_owner().model_dump(mode="json")
        finally:
            engine.dispose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory_rebuild_unavailable") from exc
