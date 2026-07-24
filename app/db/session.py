from collections.abc import Generator

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


def _sessionmaker_for_current_database() -> sessionmaker:
    url = active_profile_database_url.get() or get_settings().database_url
    if url == str(engine.url):
        return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    if url not in _sessionmakers:
        _sessionmakers[url] = sessionmaker(
            bind=build_engine(url), autoflush=False, autocommit=False, future=True
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
    ensure_memory_metadata_columns(target_engine)
    ensure_typed_memory_columns(target_engine)
    ensure_memory_embedding_table(target_engine)


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


def ensure_memory_metadata_columns(target_engine=engine) -> None:
    """Add traceability columns for existing SQLite memory databases."""

    inspector = inspect(target_engine)
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
    with target_engine.begin() as connection:
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


def ensure_typed_memory_columns(target_engine=engine) -> None:
    """Apply additive, idempotent columns used by typed memory records."""

    inspector = inspect(target_engine)
    table_names = set(inspector.get_table_names())
    table_columns = {
        "memories": {
            "fingerprint": "VARCHAR(64)",
            "expires_at": "DATETIME",
        },
        "preferences": {
            "canonical_slot": "VARCHAR(160)",
            "fingerprint": "VARCHAR(64)",
        },
        "goals": {
            "target_date": "DATE",
            "horizon_months": "INTEGER",
            "fingerprint": "VARCHAR(64)",
        },
        "events": {
            "fingerprint": "VARCHAR(64)",
        },
        "memory_sources": {
            "detachment_reason": "VARCHAR(32)",
        },
    }
    indexes = {
        "ix_memories_fingerprint": ("memories", "fingerprint"),
        "ix_preferences_canonical_slot": ("preferences", "canonical_slot"),
        "ix_preferences_fingerprint": ("preferences", "fingerprint"),
        "ix_goals_fingerprint": ("goals", "fingerprint"),
        "ix_events_fingerprint": ("events", "fingerprint"),
    }
    with target_engine.begin() as connection:
        for table_name, columns in table_columns.items():
            if table_name not in table_names:
                continue
            existing = {column["name"] for column in inspect(target_engine).get_columns(table_name)}
            for name, column_type in columns.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}"),
                    )
        for index_name, (table_name, column_name) in indexes.items():
            if table_name in table_names:
                connection.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})"
                    ),
                )


def ensure_memory_embedding_table(target_engine=engine) -> None:
    """Create embedding metadata table for existing SQLite databases."""

    inspector = inspect(target_engine)
    if "memory_embeddings" in inspector.get_table_names():
        return
    with target_engine.begin() as connection:
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
                        THEN 'preference:' || lower(trim(substr(
                            memory_text, 1, instr(memory_text, '=') - 1
                        )))
                    WHEN memory_type = 'identity' AND instr(memory_text, '=') > 0
                        THEN 'identity:' || lower(trim(substr(
                            memory_text, 1, instr(memory_text, '=') - 1
                        )))
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
