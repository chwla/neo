from collections.abc import Generator
from threading import Lock

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import active_profile_database_url, get_settings
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
_sessionmakers: dict[str, sessionmaker] = {}
# Chat generation workers, memory extraction threads, and API requests all
# resolve session factories concurrently.  Without this lock two threads can
# each build an engine for the same URL; the loser is dropped without
# ``dispose()``, leaking its SQLite connection pool for the process lifetime.
_sessionmakers_lock = Lock()


def _sessionmaker_for_current_database() -> sessionmaker:
    url = active_profile_database_url.get() or get_settings().database_url
    cached = _sessionmakers.get(url)
    if cached is not None:
        return cached
    with _sessionmakers_lock:
        existing = _sessionmakers.get(url)
        if existing is not None:
            return existing
        bind = engine if url == str(engine.url) else build_engine(url)
        _sessionmakers[url] = sessionmaker(
            bind=bind, autoflush=False, autocommit=False, future=True
        )
    return _sessionmakers[url]


class _ProfileAwareSessionFactory:
    """Keep legacy ``SessionLocal()`` callers isolated to the active profile."""

    def __call__(self, *args, **kwargs):
        return _sessionmaker_for_current_database()(*args, **kwargs)


SessionLocal = _ProfileAwareSessionFactory()


def initialize_database(database_url: str | None = None) -> None:
    """Create the local schema when running without migrations."""

    import app.models  # noqa: F401

    target_engine = engine if database_url is None else build_engine(database_url)
    Base.metadata.create_all(bind=target_engine)
    ensure_chat_message_metadata_columns(target_engine)
    ensure_chat_generation_columns(target_engine)
    ensure_chat_columns(target_engine)


def ensure_chat_columns(target_engine=engine) -> None:
    """Add chat columns introduced after the initial schema.

    A database created before pinning existed has no `pinned` column, and the
    sidebar orders by it on every read, so the column is added with a default
    rather than left to fail the first query.
    """

    inspector = inspect(target_engine)
    if "chats" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("chats")}
    if "pinned" in existing:
        return
    with target_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE chats ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0")
        )


def ensure_chat_message_metadata_columns(target_engine=engine) -> None:
    """Add nullable chat metadata columns for existing SQLite databases."""

    inspector = inspect(target_engine)
    if "chat_messages" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("chat_messages")}
    columns = {
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "duration_ms": "INTEGER",
        "thinking": "TEXT",
        "response_kind": "VARCHAR(40)",
        "provider_name": "VARCHAR(120)",
        "model_name": "VARCHAR(240)",
        "route_name": "VARCHAR(120)",
        "finish_reason": "VARCHAR(40)",
        "trace_id": "VARCHAR(80)",
        "metadata_json": "TEXT",
        "generation_id": "VARCHAR(36)",
    }
    with target_engine.begin() as connection:
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(
                    text(f"ALTER TABLE chat_messages ADD COLUMN {name} {column_type}")
                )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_messages_generation "
                "ON chat_messages (generation_id)"
            )
        )


def ensure_chat_generation_columns(target_engine=engine) -> None:
    """Add streaming fields introduced after the initial chat-generation schema."""

    inspector = inspect(target_engine)
    if "chat_generations" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("chat_generations")}
    columns = {
        "thinking": "TEXT",
        "status_detail": "VARCHAR(120)",
        "timezone": "VARCHAR(80)",
        "locale": "VARCHAR(40)",
        "response_kind": "VARCHAR(40)",
        "provider_name": "VARCHAR(120)",
        "model_name": "VARCHAR(240)",
        "route_name": "VARCHAR(120)",
        "finish_reason": "VARCHAR(40)",
        "trace_id": "VARCHAR(80)",
        "metadata_json": "TEXT",
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "total_tokens": "INTEGER",
        "duration_ms": "INTEGER",
        "worker_id": "VARCHAR(36)",
        "heartbeat_at": "DATETIME",
        "lease_token": "VARCHAR(36)",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    }
    with target_engine.begin() as connection:
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(
                    text(f"ALTER TABLE chat_generations ADD COLUMN {name} {column_type}")
                )


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
