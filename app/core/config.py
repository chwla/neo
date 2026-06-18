from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the local Neo memory service."""

    model_config = SettingsConfigDict(env_prefix="NEO_", env_file=".env", extra="ignore")

    database_url: str = Field(default="sqlite:///./neo_memory.db")
    qdrant_url: str = Field(default="http://localhost:6333")
    ollama_url: str = Field(default="http://127.0.0.1:11434")
    chat_model: str = Field(default="llama3.2:3b")
    chat_timeout_seconds: int = Field(default=240)
    chat_num_predict: int = Field(default=160)
    simple_chat_num_predict: int = Field(default=96)
    extraction_after_turn_enabled: bool = Field(default=False)
    semantic_retrieval_enabled: bool = Field(default=False)
    auto_embed_memories: bool = Field(default=False)
    embedding_provider: str = Field(default="ollama")
    embedding_model: str = Field(default="nomic-embed-text:latest")
    embedding_timeout_seconds: int = Field(default=10)
    max_semantic_candidates: int = Field(default=50)
    semantic_similarity_threshold: float = Field(default=0.55)
    hybrid_fts_weight: float = Field(default=1.4)
    hybrid_semantic_weight: float = Field(default=2.0)
    hybrid_slot_weight: float = Field(default=3.0)
    hybrid_importance_weight: float = Field(default=0.05)


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
