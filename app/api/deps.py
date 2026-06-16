from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.memory_store import MemoryStore


def get_store(db: Annotated[Session, Depends(get_db)]) -> Generator[MemoryStore, None, None]:
    yield MemoryStore(db)
