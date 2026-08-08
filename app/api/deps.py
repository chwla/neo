from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories.app_store import AppStore


def get_store(db: Annotated[Session, Depends(get_db)]) -> Generator[AppStore, None, None]:
    yield AppStore(db)
