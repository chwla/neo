from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event

from app.services.memory_v2.adapters import (
    MemoryV2AdapterContext,
    StructuredMemoryInput,
)
from app.services.memory_v2.contracts import ActorKind, SourceKind
from app.services.memory_v2.coordinator import (
    MemoryV2ExecutionContext,
    MemoryV2MutationCoordinator,
)
from app.services.memory_v2.disposable_crypto import DisposableMemoryCrypto
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.taxonomy import Cardinality, MemoryType

OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"


def sqlite_engine(database_url: str):
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


@dataclass(frozen=True)
class Phase3Harness:
    root: Path
    database_path: Path
    coordinator: MemoryV2MutationCoordinator
    context: MemoryV2AdapterContext


def phase3_harness(
    tmp_path: Path,
    *,
    owner_id: str = OWNER_A,
    profile_id: str = "disposable-one",
    canonical: bool = True,
    shadow: bool = False,
    enabled_owners: frozenset[str] | None = None,
    guest: bool = False,
    incognito: bool = False,
    memory_enabled: bool = True,
    source_kind: SourceKind = SourceKind.MANUAL_UI,
    message_id: str | None = None,
) -> Phase3Harness:
    root = tmp_path / "phase3-disposable"
    database_path = root / profile_id / "neo.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    owners = enabled_owners if enabled_owners is not None else frozenset({owner_id})
    flags = MemoryV2FeatureFlags(
        schema_enabled=canonical or shadow,
        shadow_mutations=shadow,
        canonical_writes=canonical,
        enabled_owner_ids=owners,
        disposable_database_root=str(root),
    )
    crypto = DisposableMemoryCrypto(seed=b"phase3-disposable-test-seed-material")
    coordinator = MemoryV2MutationCoordinator(
        flags=flags,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
        engine_factory=sqlite_engine,
    )
    prefix = "guest-profile" if guest else "account-profile"
    execution = MemoryV2ExecutionContext(
        owner_id=owner_id,
        database_identity=f"{prefix}:{profile_id}",
        database_url=f"sqlite:///{database_path}",
        profile_id=profile_id,
        is_guest=guest,
        is_incognito=incognito,
        memory_enabled=memory_enabled,
        disposable=True,
    )
    context = MemoryV2AdapterContext(
        execution=execution,
        actor_kind=ActorKind.USER,
        actor_id="phase3-user",
        source_kind=source_kind,
        source_id="phase3-source",
        request_id="phase3-request",
        session_id="phase3-session",
        conversation_id="phase3-chat",
        message_id=message_id,
    )
    return Phase3Harness(root, database_path, coordinator, context)


def video_goal(value: str, *, proposal_id=None) -> StructuredMemoryInput:
    return StructuredMemoryInput(
        memory_type=MemoryType.GOAL,
        domain_key="video_creation",
        slot_key="goal:video_creation:current_primary_goal",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=value,
        display_text=value,
        proposal_id=proposal_id,
    )
