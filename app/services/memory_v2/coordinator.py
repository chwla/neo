"""Single owner-bound execution boundary for every Phase 3 mutation adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.engine import Engine, make_url

from app.core.identifiers import canonical_uuid
from app.db.memory_v2_migrations import upgrade_memory_v2
from app.db.session import build_engine
from app.services.memory_v2.compatibility import (
    MemoryCompatibilityResult,
    map_compatibility_result,
)
from app.services.memory_v2.contracts import (
    DetachMemorySourceCommand,
    MemoryCommand,
    MemoryCommandResult,
    MemoryRejectionCode,
    SourceChangeOutcome,
    SourceChangeResult,
)
from app.services.memory_v2.crypto import (
    KeyedFingerprintProvider,
    KeyVersionResolver,
    SensitivePayloadProvider,
    TombstoneHMACProvider,
)
from app.services.memory_v2.feature_flags import (
    MemoryV2FeatureFlags,
    MemoryV2RolloutError,
    MemoryV2WriteMode,
)
from app.services.memory_v2.mutations import MemoryMutationService

EngineFactory = Callable[[str], Engine]
MutationServiceFactory = Callable[..., MemoryMutationService]


class MemoryV2CoordinationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryV2ExecutionContext:
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
class MemoryV2CoordinationResult:
    mode: MemoryV2WriteMode
    mutation: MemoryCommandResult | None
    compatibility: MemoryCompatibilityResult | None
    v2_called: bool
    legacy_write_allowed: bool
    reason: str | None = None


class MemoryV2MutationCoordinator:
    def __init__(
        self,
        *,
        flags: MemoryV2FeatureFlags,
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
        context: MemoryV2ExecutionContext,
        command: MemoryCommand,
    ) -> MemoryV2CoordinationResult:
        owner = self._validate_context(context)
        if command.owner_id != owner:
            raise MemoryV2CoordinationError("command_owner_context_mismatch")
        mode = self.flags.mode_for(owner)

        if context.is_incognito:
            result = MemoryCommandResult.disabled_for(
                command,
                rejection_code=MemoryRejectionCode.INCOGNITO_DISABLED,
                message="incognito_disabled",
            )
            return self._without_call(mode, result, "incognito_disabled")
        if not context.memory_enabled:
            result = MemoryCommandResult.disabled_for(
                command,
                rejection_code=MemoryRejectionCode.MEMORY_DISABLED,
                message="memory_disabled",
            )
            return self._without_call(mode, result, "memory_disabled")
        if mode is MemoryV2WriteMode.LEGACY:
            return MemoryV2CoordinationResult(
                mode=mode,
                mutation=None,
                compatibility=None,
                v2_called=False,
                legacy_write_allowed=True,
                reason="production_default_legacy",
            )
        if mode is MemoryV2WriteMode.SCHEMA_ONLY:
            return MemoryV2CoordinationResult(
                mode=mode,
                mutation=None,
                compatibility=None,
                v2_called=False,
                legacy_write_allowed=True,
                reason="owner_not_enabled_for_v2_mutations",
            )

        self._require_disposable_database(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory_v2(
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
            effective = command
            if mode is MemoryV2WriteMode.SHADOW and not command.dry_run:
                effective = command.model_copy(update={"dry_run": True})
            result = service.execute(effective)
        finally:
            engine.dispose()

        compatibility = (
            map_compatibility_result(result) if self.flags.legacy_compatibility else None
        )
        if compatibility is not None and mode is MemoryV2WriteMode.SHADOW:
            compatibility = replace(compatibility, committed=False)
        return MemoryV2CoordinationResult(
            mode=mode,
            mutation=result,
            compatibility=compatibility,
            v2_called=True,
            legacy_write_allowed=False,
            reason="shadow_dry_run" if mode is MemoryV2WriteMode.SHADOW else None,
        )

    def detach_source(
        self,
        context: MemoryV2ExecutionContext,
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
            raise MemoryV2CoordinationError("incognito_source_change_disabled")
        if not context.memory_enabled:
            raise MemoryV2CoordinationError("memory_disabled_source_change")
        mode = self.flags.mode_for(owner)
        if mode is not MemoryV2WriteMode.CANONICAL:
            raise MemoryV2CoordinationError("source_change_requires_canonical_v2_mode")

        self._require_disposable_database(context)
        engine = self._engine_factory(context.database_url)
        try:
            upgrade_memory_v2(
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

    def _without_call(
        self,
        mode: MemoryV2WriteMode,
        result: MemoryCommandResult,
        reason: str,
    ) -> MemoryV2CoordinationResult:
        return MemoryV2CoordinationResult(
            mode=mode,
            mutation=result,
            compatibility=(
                map_compatibility_result(result) if self.flags.legacy_compatibility else None
            ),
            v2_called=False,
            legacy_write_allowed=False,
            reason=reason,
        )

    @staticmethod
    def _validate_context(context: MemoryV2ExecutionContext) -> str:
        owner = context.validated_owner()
        if not context.profile_id.strip():
            raise MemoryV2CoordinationError("profile_context_required")
        if not context.database_identity.strip():
            raise MemoryV2CoordinationError("database_identity_required")
        if not context.database_url.strip():
            raise MemoryV2CoordinationError("explicit_profile_database_required")
        expected_prefix = "guest-profile:" if context.is_guest else "account-profile:"
        if not context.database_identity.startswith(expected_prefix):
            raise MemoryV2CoordinationError("guest_permanent_database_binding_mismatch")
        if context.database_identity != f"{expected_prefix}{context.profile_id}":
            raise MemoryV2CoordinationError("profile_database_identity_mismatch")
        return owner

    def _require_disposable_database(self, context: MemoryV2ExecutionContext) -> None:
        if not context.disposable:
            raise MemoryV2RolloutError("v2_mutations_require_disposable_profile")
        url = make_url(context.database_url)
        if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
            raise MemoryV2RolloutError("v2_mutations_require_file_backed_sqlite")
        configured = Path(self.flags.disposable_database_root).expanduser().resolve()
        database_path = Path(url.database).expanduser().resolve()
        if database_path == configured or not database_path.is_relative_to(configured):
            raise MemoryV2RolloutError("v2_database_outside_disposable_root")
