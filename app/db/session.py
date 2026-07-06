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
    ensure_memory_metadata_columns()
    ensure_memory_embedding_table()


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
                connection.execute(
                    text(f"ALTER TABLE chat_messages ADD COLUMN {name} {column_type}")
                )


def ensure_memory_metadata_columns() -> None:
    """Add traceability columns for existing SQLite memory databases."""

    inspector = inspect(engine)
    if "memories" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("memories")}
    columns = {
        "source_sentence": "TEXT",
        "source_conversation_id": "INTEGER",
        "canonical_slot": "VARCHAR(120)",
        "status": "VARCHAR(32) NOT NULL DEFAULT 'active'",
        "supersedes_id": "INTEGER",
        "update_reason": "TEXT",
    }
    with engine.begin() as connection:
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE memories ADD COLUMN {name} {column_type}"))
        connection.execute(
            text(
                """
                UPDATE memories
                SET source_sentence = memory_text
                WHERE source_sentence IS NULL
                """,
            ),
        )


def ensure_memory_embedding_table() -> None:
    """Create embedding metadata table for existing SQLite databases."""

    inspector = inspect(engine)
    if "memory_embeddings" in inspector.get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE memory_embeddings (
                    memory_id INTEGER NOT NULL PRIMARY KEY,
                    model VARCHAR(120) NOT NULL,
                    provider VARCHAR(80) NOT NULL DEFAULT 'ollama',
                    dimensions INTEGER,
                    vector_json TEXT,
                    content_hash VARCHAR(64),
                    status VARCHAR(32) NOT NULL DEFAULT 'missing',
                    error TEXT,
                    embedded_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id)
                )
                """,
            ),
        )
        connection.execute(
            text("CREATE INDEX ix_memory_embeddings_status ON memory_embeddings(status)"),
        )
        connection.execute(
            text("CREATE INDEX ix_memory_embeddings_model ON memory_embeddings(model)"),
        )
        connection.execute(
            text(
                """
                UPDATE memories
                SET status = CASE
                    WHEN is_active = 1 THEN 'active'
                    WHEN superseded_by_id IS NOT NULL THEN 'superseded'
                    ELSE 'deleted'
                END
                WHERE status IS NULL OR status = ''
                """,
            ),
        )
        connection.execute(
            text(
                """
                UPDATE memories
                SET canonical_slot = CASE
                    WHEN lower(memory_text) LIKE 'current hardware:%' THEN 'current_hardware'
                    WHEN memory_type = 'preference' AND instr(memory_text, '=') > 0
                        THEN 'preference:' || lower(trim(substr(memory_text, 1, instr(memory_text, '=') - 1)))
                    WHEN memory_type = 'identity' AND instr(memory_text, '=') > 0
                        THEN 'identity:' || lower(trim(substr(memory_text, 1, instr(memory_text, '=') - 1)))
                    WHEN memory_type = 'project_related'
                        THEN 'project:' || lower(trim(memory_text))
                    ELSE memory_type
                END
                WHERE canonical_slot IS NULL OR canonical_slot = ''
                """,
            ),
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
