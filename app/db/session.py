from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base


def build_engine(database_url: str | None = None):
    """Create a SQLAlchemy engine for SQLite-first local storage."""

    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    created_engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        configure_sqlite(created_engine)
    return created_engine


def configure_sqlite(created_engine) -> None:
    """Tune SQLite for a local app with Streamlit/API readers and short writes."""

    @event.listens_for(created_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def initialize_database() -> None:
    """Create the local schema when running without migrations."""

    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_chat_message_metadata_columns()


def ensure_chat_message_metadata_columns() -> None:
    """Add nullable chat metadata columns for existing SQLite databases."""

    inspector = inspect(engine)
    if "chat_messages" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("chat_messages")}
    columns = {
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "duration_ms": "INTEGER",
        "thinking": "TEXT",
    }
    with engine.begin() as connection:
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE chat_messages ADD COLUMN {name} {column_type}"))


initialize_database()


def get_db() -> Generator[Session, None, None]:
    """Yield a database session for request-scoped usage."""

    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
