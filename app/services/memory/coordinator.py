"""Single owner-bound execution boundary for every Phase 3 mutation adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.engine import Engine

from app.core.identifiers import canonical_uuid
from app.db.memory_migrations import upgrade_memory
from app.db.session import build_engine
from app.services.memory.contracts import (
    CandidatePersistenceOutcome,
    CandidatePersistenceResult,
    CandidateStatusSnapshot,
    CanonicalMemorySnapshot,
    DetachMemorySourceCommand,
    MemoryCommand,
    MemoryCommandResult,
    MemoryRejectionCode,
    PersistExtractionCandidateCommand,
    SourceChangeOutcome,
    SourceChangeResult,
)
from app.services.memory.crypto import (
    KeyedFingerprintProvider,
    KeyVersionResolver,
    SensitivePayloadProvider,
    TombstoneHMACProvider,
)
from app.services.memory.mutations import MemoryMutationService
from app.services.memory.settings import MemorySettings

EngineFactory = Callable[[str], Engine]
MutationServiceFactory = Callable[..., MemoryMutationService]


class MemoryCoordinationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryExecutionContext:
    owner_id: str
    database_identity: str
    database_url: str
    profile_id: str
    is_guest: bool = False
    is_incognito: bool = False
    memory_enabled: bool = True
    disposable: bool = False

    def validated_owner(self) -> str:
        return canonical_uuid(self.owner_id)


@dataclass(frozen=True)
class MemoryCoordinationResult:
    mutation: MemoryCommandResult | None
    called: bool
    reason: str | None = None


class MemoryMutationCoordinator:
    def __init__(
        self,
        *,
        flags: MemorySettings,
        payload_provider: SensitivePayloadProvider,
        fingerprint_provider: KeyedFingerprintProvider,
        tombstone_provider: TombstoneHMACProvider,
        key_versions: KeyVersionResolver,
        engine_factory: EngineFactory = build_engine,
        service_factory: MutationServiceFactory = MemoryMutationService,
    ) -> None:
        self.flags = flags
        self._payload_provider = payload_provider
        self._fingerprint_provider = fingerprint_provider
        self._tombstone_provider = tombstone_provider
        self._key_versions = key_versions
        self._engine_factory = engine_factory
        self._service_factory = service_factory

    def execute(
        self,
        context: MemoryExecutionContext,
        command: MemoryCommand,
    ) -> MemoryCoordinationResult:
        owner = self._validate_context(context)
        if command.owner_id != owner:
            raise MemoryCoordinationError("command_owner_context_mismatch")
        if context.is_incognito or not self.flags.enabled:
            result = MemoryCommandResult.disabled_for(
                command,
                rejection_code=(
                    MemoryRejectionCode.INCOGNITO_DISABLED
                    if context.is_incognito
                    else MemoryRejectionCode.MEMORY_DISABLED
                ),
                message="incognito_disabled" if context.is_incognito else "memory_disabled",
            )
            return self._without_call(
                result, "incognito_disabled" if context.is_incognito else "memory_disabled"
            )
        if not context.memory_enabled:
            result = MemoryCommandResult.disabled_for(
                command,
                rejection_code=MemoryRejectionCode.MEMORY_DISABLED,
                message="memory_disabled",
            )
            return self._without_call(result, "memory_disabled")
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            service = self._service_factory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
                payload_provider=self._payload_provider,
                fingerprint_provider=self._fingerprint_provider,
                tombstone_provider=self._tombstone_provider,
                key_versions=self._key_versions,
            )
            result = service.execute(command)
        finally:
            engine.dispose()

        return MemoryCoordinationResult(
            mutation=result,
            called=True,
        )

    def detach_source(
        self,
        context: MemoryExecutionContext,
        command: DetachMemorySourceCommand,
    ) -> SourceChangeResult:
        """Execute an exact source-only change in canonical disposable mode."""

        owner = self._validate_context(context)
        if command.owner_id != owner:
            return SourceChangeResult(
                outcome=SourceChangeOutcome.OWNER_MISMATCH,
                owner_id=command.owner_id,
                memory_id=command.target.memory_id,
                requested_source_id=command.source_id,
                review_required=False,
                idempotency_key=command.idempotency_key,
                reason="command_owner_context_mismatch",
            )
        if context.is_incognito:
            raise MemoryCoordinationError("incognito_source_change_disabled")
        if not context.memory_enabled:
            raise MemoryCoordinationError("memory_disabled_source_change")
        if not self.flags.enabled:
            raise MemoryCoordinationError("memory_disabled_source_change")
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            service = self._service_factory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
                payload_provider=self._payload_provider,
                fingerprint_provider=self._fingerprint_provider,
                tombstone_provider=self._tombstone_provider,
                key_versions=self._key_versions,
            )
            return service.detach_source(command)
        finally:
            engine.dispose()

    def list_active_memories(
        self,
        context: MemoryExecutionContext,
        *,
        limit: int = 200,
        include_archived: bool = False,
    ) -> tuple[CanonicalMemorySnapshot, ...]:
        owner = self._require_canonical_query_context(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            return self._build_service(engine, owner, context).list_active_records(
                limit=limit,
                include_archived=include_archived,
            )
        finally:
            engine.dispose()

    def candidate_status(
        self,
        context: MemoryExecutionContext,
        candidate_id: UUID,
    ) -> CandidateStatusSnapshot | None:
        owner = self._require_canonical_query_context(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            return self._build_service(engine, owner, context).candidate_status(candidate_id)
        finally:
            engine.dispose()

    def reject_candidate(
        self,
        context: MemoryExecutionContext,
        candidate_id: UUID,
        *,
        expected_revision: int,
    ) -> CandidateStatusSnapshot:
        owner = self._require_canonical_query_context(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            return self._build_service(engine, owner, context).reject_candidate(
                candidate_id,
                expected_revision=expected_revision,
            )
        finally:
            engine.dispose()

    def persist_extraction_candidate(
        self,
        context: MemoryExecutionContext,
        command: PersistExtractionCandidateCommand,
    ) -> CandidatePersistenceResult:
        owner = self._validate_context(context)
        if command.owner_id != owner:
            return CandidatePersistenceResult(
                outcome=CandidatePersistenceOutcome.REJECTED,
                owner_id=command.owner_id,
                candidate_id=command.candidate.proposal_id,
                reason="command_owner_context_mismatch",
            )
        self._require_canonical_query_context(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory(
                engine,
                owner_id=owner,
                database_identity=context.database_identity,
            )
            return self._build_service(engine, owner, context).persist_extraction_candidate(command)
        finally:
            engine.dispose()

    def _require_canonical_query_context(self, context: MemoryExecutionContext) -> str:
        owner = self._validate_context(context)
        if context.is_incognito:
            raise MemoryCoordinationError("incognito_extraction_disabled")
        if not context.memory_enabled:
            raise MemoryCoordinationError("memory_disabled_extraction")
        if not self.flags.enabled:
            raise MemoryCoordinationError("memory_disabled_extraction")
        return owner

    def _build_service(
        self,
        engine: Engine,
        owner: str,
        context: MemoryExecutionContext,
    ) -> MemoryMutationService:
        return self._service_factory(
            engine,
            owner_id=owner,
            database_identity=context.database_identity,
            payload_provider=self._payload_provider,
            fingerprint_provider=self._fingerprint_provider,
            tombstone_provider=self._tombstone_provider,
            key_versions=self._key_versions,
        )

    def _without_call(
        self,
        result: MemoryCommandResult,
        reason: str,
    ) -> MemoryCoordinationResult:
        return MemoryCoordinationResult(
            mutation=result,
            called=False,
            reason=reason,
        )

    @staticmethod
    def _validate_context(context: MemoryExecutionContext) -> str:
        owner = context.validated_owner()
        if not context.profile_id.strip():
            raise MemoryCoordinationError("profile_context_required")
        if not context.database_identity.strip():
            raise MemoryCoordinationError("database_identity_required")
        if not context.database_url.strip():
            raise MemoryCoordinationError("explicit_profile_database_required")
        expected_prefix = "guest-profile:" if context.is_guest else "account-profile:"
        if not context.database_identity.startswith(expected_prefix):
            raise MemoryCoordinationError("guest_permanent_database_binding_mismatch")
        if context.database_identity != f"{expected_prefix}{context.profile_id}":
            raise MemoryCoordinationError("profile_database_identity_mismatch")
        return owner
