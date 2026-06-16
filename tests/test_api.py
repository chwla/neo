from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_store
from app.main import app
from app.models import Base
from app.repositories.memory_store import MemoryStore


def test_api_extract_auto_stores_and_list_memory() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_store():
        session = session_factory()
        try:
            yield MemoryStore(session)
        finally:
            session.close()

    app.dependency_overrides[get_store] = override_store
    client = TestClient(app)
    try:
        extraction = client.post(
            "/extract-memory",
            json={"text": "My name is Soham.", "persist": True},
        )
        assert extraction.status_code == 200
        assert extraction.json()["candidate_ids"]

        profile = client.get("/profile")
        assert profile.status_code == 200
        assert profile.json()[0]["key"] == "name"
    finally:
        app.dependency_overrides.clear()

