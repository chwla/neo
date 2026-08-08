from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session, sessionmaker

from app.repositories.memory import MemoryRepository
from app.services.memory.adapters import (
    ChatMemoryAdapter,
    StructuredMemoryInput,
)
from app.services.memory.contracts import Sensitivity
from app.services.memory.prompt import (
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory.queries import MemoryQueryContext, RecallMode
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.settings import MemorySettings
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.mutation_helpers import (
    OWNER_A,
    MemoryHarness,
    memory_harness,
    sqlite_engine,
)


@dataclass
class RecallServices:
    harness: MemoryHarness
    session: Session
    repository: MemoryRepository
    recall: CanonicalRecallService
    prompt: RecallPromptOrchestrator

    def close(self) -> None:
        self.session.rollback()
        bind = self.session.get_bind()
        self.session.close()
        bind.dispose()


def recall_harness(
    tmp_path: Path,
    *,
    owner_id: str = OWNER_A,
    profile_id: str = "disposable-one",
) -> tuple[MemoryHarness, ChatMemoryAdapter]:
    harness = memory_harness(tmp_path, owner_id=owner_id, profile_id=profile_id)
    return harness, ChatMemoryAdapter(harness.coordinator)


def add_memory(
    adapter: ChatMemoryAdapter,
    harness: MemoryHarness,
    *,
    key: str,
    memory_type: MemoryType,
    domain: str,
    slot: str,
    text: str,
    cardinality: Cardinality = Cardinality.ADDITIVE,
    importance: int = 7,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
):
    identifier = uuid5(NAMESPACE_URL, f"recall-fixture:{key}")
    if memory_type is MemoryType.GOAL:
        if slot.endswith(":current_primary_goal") or slot.endswith(":primary_output"):
            cardinality = Cardinality.EXCLUSIVE
        else:
            cardinality = Cardinality.ADDITIVE
            slot = f"goal:{domain}:independent:{identifier}"
    elif memory_type in {MemoryType.PREFERENCE, MemoryType.IDENTITY}:
        cardinality = Cardinality.EXCLUSIVE
    elif memory_type not in {MemoryType.EDUCATION, MemoryType.EMPLOYMENT}:
        cardinality = Cardinality.ADDITIVE
        slot = f"{memory_type.value}:{domain}:item:{identifier}"
    result = adapter.create(
        harness.context,
        StructuredMemoryInput(
            memory_type=memory_type,
            domain_key=domain,
            slot_key=slot,
            cardinality=cardinality,
            canonical_value=text,
            display_text=text,
            importance=importance,
            sensitivity=sensitivity,
            explicit_user_request=sensitivity is Sensitivity.SENSITIVE,
        ),
        idempotency_key=f"recall:{key}",
    )
    assert result.mutation is not None
    assert result.mutation.active_memory_ids
    return result.mutation.active_memory_ids[-1]


def recall_services(harness: MemoryHarness) -> RecallServices:
    engine = sqlite_engine(harness.context.execution.database_url)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    repository = MemoryRepository(
        session,
        owner_id=harness.context.execution.owner_id,
        database_identity=harness.context.execution.database_identity,
    )
    flags: MemorySettings = harness.coordinator.flags
    recall = CanonicalRecallService(repository, flags=flags)
    prompt = RecallPromptOrchestrator(
        recall,
        usage_recorder=repository_usage_recorder(repository),
    )
    return RecallServices(harness, session, repository, recall, prompt)


def query_context(
    services: RecallServices,
    *,
    mode: RecallMode = RecallMode.SCOPED_LEXICAL,
    domains: frozenset[str] = frozenset(),
    memory_types: frozenset[MemoryType] = frozenset(),
    memory_enabled: bool = True,
    incognito: bool = False,
    override=None,
    maximum_records: int = 5,
    maximum_characters: int = 2_400,
    lexical_available: bool = True,
) -> MemoryQueryContext:
    execution = services.harness.context.execution
    return MemoryQueryContext(
        owner_id=execution.owner_id,
        database_identity=execution.database_identity,
        profile_id=execution.profile_id,
        memory_enabled=memory_enabled,
        incognito=incognito,
        request_id="recall-request",
        session_id="recall-session",
        current_time=datetime.now(UTC),
        allowed_domains=domains,
        allowed_memory_types=memory_types,
        maximum_records=maximum_records,
        maximum_characters=maximum_characters,
        mode=mode,
        current_turn_override=override,
        lexical_available=lexical_available,
    )
