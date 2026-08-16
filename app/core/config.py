from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

active_profile_database_url: ContextVar[str | None] = ContextVar(
    "active_profile_database_url", default=None
)
active_profile_storage_dir: ContextVar[str | None] = ContextVar(
    "active_profile_storage_dir", default=None
)


class Settings(BaseSettings):
    """Runtime settings for the local Neo memory service."""

    model_config = SettingsConfigDict(env_prefix="NEO_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: str | None = Field(default=None)
    database_url: str = Field(default="sqlite:///./neo_memory.db")
    qdrant_url: str = Field(default="http://localhost:6333")
    ollama_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices("OLLAMA_BASE_URL", "NEO_OLLAMA_BASE_URL", "NEO_OLLAMA_URL"),
    )
    chat_model: str = Field(default="llama3.2:3b")
    llm_provider: str = Field(default="ollama")
    default_model: str = Field(
        default="llama3.2:3b",
        validation_alias=AliasChoices("NEO_DEFAULT_MODEL", "NEO_CHAT_MODEL"),
    )
    openai_compat_base_url: str = Field(default="")
    openai_compat_api_key_ref: str = Field(default="OPENAI_API_KEY")
    openai_compat_model: str = Field(default="")
    chat_timeout_seconds: int = Field(default=240)
    # Reasoning models spend part of this budget thinking before they emit any answer:
    # gemma4 uses ~550 tokens on reasoning alone, so a 512 budget hit the cap before
    # writing a word and every retry repeated it.
    chat_num_predict: int = Field(default=2048)
    chat_history_turns: int = Field(default=8, ge=1, le=24)
    llm_config_path: str = Field(default="neo_llms.json")
    workspace_files_dir: str = Field(default="data/workspace_files")
    workspace_repos_dir: str = Field(default="data/workspace_repos")
    frontend_dir: str = Field(default="app/static")
    workspace_repo_max_files: int = Field(default=500, ge=1)
    workspace_repo_max_total_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    workspace_repo_max_file_bytes: int = Field(default=1024 * 1024, ge=1)
    workspace_file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    workspace_extracted_text_max_chars: int = Field(default=500_000, ge=1)
    simple_chat_num_predict: int = Field(default=1024)
    default_timezone: str = Field(default="UTC")
    memory_enabled: bool = Field(default=True)
    memory_extraction_enabled: bool = Field(default=True)
    memory_semantic_recall_enabled: bool = Field(default=True)
    memory_index_worker_enabled: bool = Field(default=True)
    memory_incognito: bool = Field(default=False)
    memory_extraction_provider: str = Field(default="ollama")
    memory_extraction_endpoint: str = Field(default="")
    memory_extraction_model: str = Field(default="")
    memory_extraction_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    memory_extraction_response_timeout_seconds: int = Field(default=120, ge=1, le=600)
    memory_extraction_warmup_timeout_seconds: int = Field(default=300, ge=1, le=900)
    memory_ollama_request_mode: str = Field(default="auto")
    memory_extraction_max_input_chars: int = Field(default=12_000, ge=500, le=50_000)
    memory_recall_max_records: int = Field(default=5, ge=1, le=20)
    memory_recall_max_chars: int = Field(default=2400, ge=200, le=12000)
    memory_recall_min_score: float = Field(default=0.18, ge=0, le=1)
    # When enabled, a recognised personal question is answered from memory with a
    # fixed sentence and the model never runs.  It is off by default so recalled
    # memory is given to the model as context and the model decides whether to
    # use it, search, or answer from its own knowledge.
    memory_direct_answer_enabled: bool = Field(default=False)
    memory_worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    memory_worker_batch_size: int = Field(default=25, ge=1, le=500)
    memory_worker_poll_seconds: float = Field(default=2, ge=0.1, le=300)
    memory_retry_max_attempts: int = Field(default=5, ge=1, le=100)
    memory_dead_letter_threshold: int | None = Field(default=None, ge=1, le=100)
    memory_retry_base_seconds: int = Field(default=5, ge=1, le=3600)
    memory_retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    memory_retry_jitter_seconds: int = Field(default=0, ge=0, le=3600)
    memory_fts_candidate_limit: int = Field(default=50, ge=1, le=500)
    memory_vector_candidate_limit: int = Field(default=50, ge=1, le=500)
    memory_semantic_weight: float = Field(default=0.35, ge=0, le=1)
    memory_semantic_cap: float = Field(default=1, ge=0, le=1)
    # Cosine similarity at which a new candidate is treated as a restatement of
    # an existing memory rather than a second one.  Deliberately high: merging
    # two genuinely different facts loses data, while missing a duplicate only
    # leaves the store as it was before this check existed.
    memory_semantic_duplicate_threshold: float = Field(default=0.93, ge=0, le=1)
    memory_query_embedding_timeout_seconds: int = Field(default=5, ge=1, le=60)
    memory_index_embedding_timeout_seconds: int = Field(default=30, ge=1, le=300)
    memory_provider_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    memory_reconciliation_batch_size: int = Field(default=250, ge=1, le=5000)
    memory_alert_oldest_pending_seconds: int = Field(default=900, ge=1)
    memory_alert_dead_letter_count: int = Field(default=1, ge=0)
    memory_alert_min_coverage_ratio: float = Field(default=0.95, ge=0, le=1)
    memory_alert_consecutive_provider_failures: int = Field(default=3, ge=1)
    memory_alert_stale_ghost_rate: float = Field(default=0.05, ge=0, le=1)
    memory_alert_lease_expiration_rate: float = Field(default=0.1, ge=0, le=1)
    memory_embedding_model: str = Field(default="nomic-embed-text:latest")
    memory_embedding_provider: str = Field(default="ollama")
    memory_embedding_version: str = Field(default="1")
    memory_embedding_dimension: int = Field(default=768, ge=1, le=65536)
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
        default="disabled",
        validation_alias=AliasChoices(
            "NEO_SEARCH_PROVIDER",
            "SEARCH_PROVIDER",
            "WEB_SEARCH_PROVIDER",
            "NEO_WEB_SEARCH_PROVIDER",
        ),
    )
    searxng_instance: str = Field(
        default="http://localhost:8080",
        validation_alias=AliasChoices(
            "NEO_SEARXNG_URL",
            "SEARXNG_URL",
            "SEARXNG_INSTANCE",
            "NEO_SEARXNG_INSTANCE",
        ),
    )
    web_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEB_SEARCH_API_KEY", "NEO_WEB_SEARCH_API_KEY"),
    )
    web_search_fallback_providers: str = Field(
        default="",
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

    @model_validator(mode="after")
    def apply_data_directory(self) -> "Settings":
        fields_set = self.model_fields_set
        if "default_model" not in fields_set:
            self.default_model = self.chat_model
        if "web_search_enabled" not in fields_set:
            self.web_search_enabled = self.web_search_provider != "disabled"
        if not self.data_dir:
            return self
        data_root = Path(self.data_dir).expanduser().resolve()
        data_root.mkdir(parents=True, exist_ok=True)
        if "database_url" not in fields_set:
            self.database_url = f"sqlite:///{data_root / 'neo.db'}"
        if "workspace_files_dir" not in fields_set:
            self.workspace_files_dir = str(data_root / "workspace_files")
        if "workspace_repos_dir" not in fields_set:
            self.workspace_repos_dir = str(data_root / "workspace_repos")
        if "llm_config_path" not in fields_set:
            self.llm_config_path = str(data_root / "neo_llms.json")
        return self


@lru_cache
def _base_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Return settings for the current profile, or the app defaults outside a session."""

    settings = _base_settings()
    profile_database_url = active_profile_database_url.get()
    if profile_database_url is None:
        return settings
    storage_dir = active_profile_storage_dir.get()
    updates: dict[str, str] = {"database_url": profile_database_url}
    if storage_dir:
        root = Path(storage_dir)
        updates.update(
            {
                "data_dir": str(root),
                "workspace_files_dir": str(root / "workspace_files"),
                "workspace_repos_dir": str(root / "workspace_repos"),
                "llm_config_path": str(root / "neo_llms.json"),
            }
        )
    return settings.model_copy(update=updates)


def get_base_settings() -> Settings:
    """Return process-wide paths, regardless of the active profile context."""

    return _base_settings()


# Keep the public cache-reset hook used by the test suite and CLI setup code.
get_settings.cache_clear = _base_settings.cache_clear  # type: ignore[attr-defined]
