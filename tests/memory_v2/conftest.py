from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.memory_v2_migrations import upgrade_memory_v2
from app.db.session import build_engine
from app.services.memory_v2.contracts import (
    ActorKind,
    CandidateIntent,
    MemoryActor,
    MemorySource,
    Sensitivity,
    SourceKind,
    ValidatedCandidateProposal,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from tests.memory_v2.factories import DATABASE_IDENTITY, OWNER_A


@pytest.fixture
def normal_goal_candidate() -> ValidatedCandidateProposal:
    return ValidatedCandidateProposal(
        proposal_id=UUID("00000000-0000-4000-8000-000000000101"),
        intent=CandidateIntent.ASSERT,
        memory_type=MemoryType.GOAL,
        domain_key="video_creation",
        slot_key="goal:video_creation:primary_output",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value="create short Instagram reels clearly",
        display_text="create short Instagram reels clearly",
        sensitivity=Sensitivity.NORMAL,
        confidence=0.98,
        importance=7,
    )


@pytest.fixture
def user_actor() -> MemoryActor:
    return MemoryActor(kind=ActorKind.USER, actor_id="user-1")


@pytest.fixture
def direct_source() -> MemorySource:
    return MemorySource(kind=SourceKind.DIRECT_COMMAND, message_id="message-1")


@pytest.fixture
def memory_v2_engine(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'memory-v2.db'}")
    upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def memory_v2_session(memory_v2_engine):
    factory = sessionmaker(bind=memory_v2_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
