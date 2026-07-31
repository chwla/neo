from __future__ import annotations

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import build_engine
from app.repositories.memory_store import MemoryStore
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import ExtractionRequest, MemoryExtractionService


def test_fitness_refinements_update_canonical_rows_without_duplicates(
    tmp_path,
) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'test22.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    store = MemoryStore(session)
    try:
        turns = (
            "Remember this for future chats: I want to build strength and improve my "
            "stamina. I prefer fitness advice in simple weekly plans with sets, reps, "
            "rest days, and progression steps.",
            "Correction: For workout advice, I prefer weekly workout plans with exact "
            "exercises, sets, reps, rest days, and progression steps.",
            "Actually, my main fitness goal is to build strength while improving cardio "
            "stamina.",
        )
        extractor = MemoryExtractionService()
        for turn in turns:
            extraction = extractor.extract(ExtractionRequest(text=turn, persist=True))
            extractor.persist_and_accept(store, extraction)
            session.commit()

        assert len(store.list_memories(limit=100)) == 2
        assert len(store.list_preferences()) == 1
        assert len(store.list_goals()) == 1

        answer = DirectMemoryAnswerService().answer(
            store,
            "What do you remember about my current fitness goals and workout advice "
            "preferences? Only use saved memory.",
        )
        assert answer is not None
        assert "build strength while improving cardio stamina" in answer
        assert (
            "weekly workout plans with exact exercises, sets, reps, rest days, "
            "and progression steps"
        ) in answer
        assert "build strength and improve my stamina" not in answer
        assert "fitness advice in simple weekly plans" not in answer
    finally:
        session.close()
        engine.dispose()
