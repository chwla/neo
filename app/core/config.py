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
    #: How many chats may generate at once before further turns wait their turn.
    #: Neo is local-first and usually points at one model server, so letting every
    #: background chat start at once makes all of them slow rather than any of
    #: them fast. A profile that has set the preference overrides this; this only
    #: decides what a fresh profile does.
    max_concurrent_turns: int = Field(default=3, ge=1, le=10)
    # Reasoning models spend part of this budget thinking before they emit any answer:
    # gemma4 uses ~550 tokens on reasoning alone, so a 512 budget hit the cap before
    # writing a word and every retry repeated it.
    #: How long Ollama keeps a model resident after a request. Its own default
    #: is 5 minutes, which is shorter than the gap between two chats -- so the
    #: model is evicted and the next message pays a full cold load (seconds for
    #: a small model, minutes for a large one). That cost lands on whoever
    #: starts a new conversation, which is exactly when it is least expected.
    ollama_keep_alive: str = Field(default="30m")
    chat_num_predict: int = Field(default=2048)
    chat_history_turns: int = Field(default=8, ge=1, le=24)
    llm_config_path: str = Field(default="neo_llms.json")
    workspace_files_dir: str = Field(default="data/workspace_files")
    workspace_repos_dir: str = Field(default="data/workspace_repos")
    #: Colon-separated absolute directories under which a folder may be attached
    #: live -- that is, with the agent editing the user's own files rather than a
    #: copy. Empty means "wherever ``validate_repo_root`` already allows", which
    #: is what you want when Neo runs on the host and every path is real. In a
    #: container only the bind mount is reachable, so it is set to that mount and
    #: becomes a genuine restriction.
    workspace_live_roots: str = Field(default="")
    #: The host directory that ``workspace_live_roots`` is a mount *of*, when Neo
    #: is containerised. Display only: the API keeps speaking container paths, and
    #: this is what lets the folder picker show ``~/Desktop`` for what the server
    #: knows as ``/workspace/Desktop``. Empty when Neo runs on the host, where the
    #: two are already the same path.
    workspace_host_root: str = Field(default="")
    frontend_dir: str = Field(default="app/static")
    workspace_repo_max_files: int = Field(default=500, ge=1)
    workspace_repo_max_total_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    workspace_repo_max_file_bytes: int = Field(default=1024 * 1024, ge=1)
    workspace_file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    workspace_extracted_text_max_chars: int = Field(default=500_000, ge=1)
    gallery_thumbnails_dir: str = Field(default="data/gallery_thumbnails")
    #: Images get their own ceiling: a retina screenshot routinely clears the
    #: 5 MiB that is generous for a source file, and refusing it would make the
    #: gallery useless for exactly the case it exists to serve.
    gallery_image_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    gallery_thumbnail_max_px: int = Field(default=512, ge=32, le=4096)
    #: What the describer sends the vision model. Full resolution costs minutes
    #: per image on a laptop and buys nothing: the caption and the on-screen text
    #: both survive the downscale.
    gallery_describe_max_px: int = Field(default=1024, ge=128, le=4096)
    gallery_semantic_weight: float = Field(default=0.35, ge=0, le=1)
    gallery_min_score: float = Field(default=0.12, ge=0, le=1)
    #: Cosine similarity below which a semantic hit is treated as no hit.
    #: An embedder scores almost any two strings above zero, so without a
    #: floor the ranker's fail-closed check passes everything it indexed.
    gallery_semantic_floor: float = Field(default=0.45, ge=0, le=1)
    #: Default for the gallery's duplicate policy. A profile that has set
    #: the toggle overrides this; it only decides what a fresh profile does.
    gallery_allow_duplicates: bool = False
    gallery_auto_describe: bool = Field(default=True)
    vision_model: str = Field(default="qwen2.5vl:7b")
    vision_timeout_seconds: int = Field(default=60, ge=1, le=600)
    simple_chat_num_predict: int = Field(default=1024)
    default_timezone: str = Field(default="UTC")
    memory_enabled: bool = Field(default=True)
    memory_extraction_enabled: bool = Field(default=True)
    memory_semantic_recall_enabled: bool = Field(default=True)
    memory_index_worker_enabled: bool = Field(default=True)
    memory_incognito: bool = Field(default=False)
    memory_extraction_two_stage: bool = Field(default=True)
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
    # The embedded SearXNG provider imports searx.webapp from this tree rather
    # than talking to a container. Absent by default: scripts/setup_searxng.py
    # fetches it, and without it the provider reports itself unavailable and the
    # fallback chain takes over.
    searxng_source_dir: str = Field(
        default="data/searxng",
        validation_alias=AliasChoices(
            "NEO_SEARXNG_SOURCE_DIR",
            "SEARXNG_SOURCE_DIR",
        ),
    )
    searxng_settings_path: str = Field(
        default="docker/searxng/settings.yml",
        validation_alias=AliasChoices(
            "NEO_SEARXNG_SETTINGS_PATH",
            "SEARXNG_SETTINGS_PATH",
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
        if "gallery_thumbnails_dir" not in fields_set:
            self.gallery_thumbnails_dir = str(data_root / "gallery_thumbnails")
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
                "gallery_thumbnails_dir": str(root / "gallery_thumbnails"),
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
