from functools import lru_cache

from pydantic import AliasChoices, Field
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
    llm_config_path: str = Field(default="neo_llms.json")
    workspace_files_dir: str = Field(default="data/workspace_files")
    workspace_repos_dir: str = Field(default="data/workspace_repos")
    workspace_repo_max_files: int = Field(default=500, ge=1)
    workspace_repo_max_total_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    workspace_repo_max_file_bytes: int = Field(default=1024 * 1024, ge=1)
    workspace_file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    workspace_extracted_text_max_chars: int = Field(default=500_000, ge=1)
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
    web_search_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("WEB_SEARCH_ENABLED", "NEO_WEB_SEARCH_ENABLED"),
    )
    web_search_provider: str = Field(
        default="searxng",
        validation_alias=AliasChoices("WEB_SEARCH_PROVIDER", "NEO_WEB_SEARCH_PROVIDER"),
    )
    searxng_instance: str = Field(
        default="http://localhost:8080",
        validation_alias=AliasChoices("SEARXNG_INSTANCE", "NEO_SEARXNG_INSTANCE"),
    )
    web_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEB_SEARCH_API_KEY", "NEO_WEB_SEARCH_API_KEY"),
    )
    web_search_fallback_providers: str = Field(
        default="duckduckgo,bing_html",
        validation_alias=AliasChoices(
            "WEB_SEARCH_FALLBACK_PROVIDERS",
            "NEO_WEB_SEARCH_FALLBACK_PROVIDERS",
        ),
    )
    tavily_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TAVILY_API_KEY", "NEO_TAVILY_API_KEY"),
    )
    brave_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BRAVE_API_KEY", "NEO_BRAVE_API_KEY", "DATA_BRAVE_API_KEY"),
    )
    serper_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SERPER_API_KEY", "NEO_SERPER_API_KEY"),
    )
    web_search_max_results: int = Field(
        default=5,
        ge=1,
        le=10,
        validation_alias=AliasChoices("WEB_SEARCH_MAX_RESULTS", "NEO_WEB_SEARCH_MAX_RESULTS"),
    )
    web_fetch_max_pages: int = Field(
        default=3,
        ge=0,
        le=5,
        validation_alias=AliasChoices("WEB_FETCH_MAX_PAGES", "NEO_WEB_FETCH_MAX_PAGES"),
    )
    web_fetch_timeout_seconds: float = Field(
        default=8.0,
        gt=0,
        le=30,
        validation_alias=AliasChoices(
            "WEB_FETCH_TIMEOUT_SECONDS",
            "NEO_WEB_FETCH_TIMEOUT_SECONDS",
        ),
    )
    web_fetch_max_bytes: int = Field(
        default=1_000_000,
        ge=10_000,
        le=5_000_000,
        validation_alias=AliasChoices("WEB_FETCH_MAX_BYTES", "NEO_WEB_FETCH_MAX_BYTES"),
    )
    web_search_user_agent: str = Field(
        default="Neo/1.0 local personal assistant (+https://localhost)",
        validation_alias=AliasChoices("WEB_SEARCH_USER_AGENT", "NEO_WEB_SEARCH_USER_AGENT"),
    )
    web_context_max_tokens: int = Field(
        default=1200,
        ge=200,
        le=4000,
        validation_alias=AliasChoices("WEB_CONTEXT_MAX_TOKENS", "NEO_WEB_CONTEXT_MAX_TOKENS"),
    )
    web_cache_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("WEB_CACHE_ENABLED", "NEO_WEB_CACHE_ENABLED"),
    )
    research_fetch_timeout_seconds: float = Field(
        default=12.0,
        gt=0,
        le=30,
        validation_alias=AliasChoices(
            "RESEARCH_FETCH_TIMEOUT_SECONDS",
            "NEO_RESEARCH_FETCH_TIMEOUT_SECONDS",
        ),
    )
    research_max_fetch_workers: int = Field(
        default=4,
        ge=1,
        le=8,
        validation_alias=AliasChoices(
            "RESEARCH_MAX_FETCH_WORKERS",
            "NEO_RESEARCH_MAX_FETCH_WORKERS",
        ),
    )
    research_fetch_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        validation_alias=AliasChoices(
            "RESEARCH_FETCH_RETRIES",
            "NEO_RESEARCH_FETCH_RETRIES",
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
