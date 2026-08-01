from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session, sessionmaker

from app.repositories.memory_v2 import MemoryV2Repository
from app.services.memory_v2.adapters import (
    ChatMemoryV2Adapter,
    StructuredMemoryInput,
)
from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.prompt import (
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory_v2.queries import MemoryQueryContext, RecallMode
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from tests.memory_v2.phase3_helpers import (
    OWNER_A,
    Phase3Harness,
    phase3_harness,
    sqlite_engine,
)


@dataclass
class Phase5Services:
    harness: Phase3Harness
    session: Session
    repository: MemoryV2Repository
    recall: CanonicalRecallService
    prompt: RecallPromptOrchestrator

    def close(self) -> None:
        self.session.rollback()
        bind = self.session.get_bind()
        self.session.close()
        bind.dispose()


def phase5_harness(
    tmp_path: Path,
    *,
    owner_id: str = OWNER_A,
    profile_id: str = "disposable-one",
) -> tuple[Phase3Harness, ChatMemoryV2Adapter]:
    harness = phase3_harness(tmp_path, owner_id=owner_id, profile_id=profile_id)
    harness.coordinator.flags = replace(
        harness.coordinator.flags,
        canonical_query_enabled=True,
        lexical_recall_enabled=True,
        secure_prompt_enabled=True,
        direct_answer_reads_enabled=True,
        research_recall_enabled=True,
    )
    return harness, ChatMemoryV2Adapter(harness.coordinator)


def add_memory(
    adapter: ChatMemoryV2Adapter,
    harness: Phase3Harness,
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
    identifier = uuid5(NAMESPACE_URL, f"phase5-fixture:{key}")
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
        idempotency_key=f"phase5:{key}",
    )
    assert result.mutation is not None
    assert result.mutation.active_memory_ids
    return result.mutation.active_memory_ids[-1]


def phase5_services(harness: Phase3Harness) -> Phase5Services:
    engine = sqlite_engine(harness.context.execution.database_url)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    repository = MemoryV2Repository(
        session,
        owner_id=harness.context.execution.owner_id,
        database_identity=harness.context.execution.database_identity,
    )
    flags: MemoryV2FeatureFlags = harness.coordinator.flags
    recall = CanonicalRecallService(repository, flags=flags)
    prompt = RecallPromptOrchestrator(
        recall,
        usage_recorder=repository_usage_recorder(repository),
    )
    return Phase5Services(harness, session, repository, recall, prompt)


def query_context(
    services: Phase5Services,
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
        request_id="phase5-request",
        session_id="phase5-session",
        current_time=datetime.now(UTC),
        allowed_domains=domains,
        allowed_memory_types=memory_types,
        maximum_records=maximum_records,
        maximum_characters=maximum_characters,
        mode=mode,
        current_turn_override=override,
        lexical_available=lexical_available,
    )
