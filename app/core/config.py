from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the local Neo memory service."""

    model_config = SettingsConfigDict(env_prefix="NEO_", env_file=".env", extra="ignore")

    database_url: str = Field(default="sqlite:///./neo_memory.db")
    qdrant_url: str = Field(default="http://localhost:6333")


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()

