from __future__ import annotations

from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import build_engine
from app.repositories.memory_store import MemoryStore
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import ExtractionRequest, MemoryExtractionService


def test_canonical_memory_only_query_keeps_all_committed_goals_and_preferences(
    tmp_path,
) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'test21.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    store = MemoryStore(session)
    try:
        prompt = (
            "I mainly want to master Python, C, and C++. I prefer technical explanations "
            "that are concise for simple questions, but detailed for difficult technical "
            "topics. My current major goal is to finish testing Neo before its first launch."
        )
        extractor = MemoryExtractionService()
        extraction = extractor.extract(ExtractionRequest(text=prompt, persist=True))
        candidates = extractor.persist_and_accept(store, extraction)
        session.commit()

        assert len(candidates) == 3
        assert len(store.list_memories(limit=100)) == 3

        answer = DirectMemoryAnswerService().answer(
            store,
            "What do you remember about my current goals and preferences? "
            "Only use saved memory.",
        )
        assert answer is not None
        assert "master Python, C, and C++" in answer
        assert "finish testing Neo before its first launch" in answer
        assert "concise for simple questions" in answer
        assert "detailed for difficult technical topics" in answer
    finally:
        session.close()
        engine.dispose()
