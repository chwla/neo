from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings


class MemoryConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemorySettings:
    """Runtime controls for the one production memory system."""

    enabled: bool = True
    extraction_enabled: bool = True
    foreground_extraction_enabled: bool = True
    post_turn_extraction_enabled: bool = True
    live_extraction_model_enabled: bool = False
    extraction_provider: str = ""
    extraction_endpoint: str = ""
    extraction_model: str = ""
    extraction_connect_timeout_seconds: int = 5
    extraction_response_timeout_seconds: int = 120
    extraction_warmup_timeout_seconds: int = 300
    ollama_request_mode: str = "auto"
    extraction_max_input_chars: int = 12_000
    lexical_recall_enabled: bool = True
    secure_prompt_enabled: bool = True
    direct_answer_reads_enabled: bool = True
    research_recall_enabled: bool = True
    recall_max_records: int = 5
    recall_max_chars: int = 2_400
    recall_min_score: float = 0.18
    index_worker_enabled: bool = True
    fts_index_enabled: bool = True
    vector_index_enabled: bool = True
    semantic_recall_enabled: bool = True
    reconciliation_enabled: bool = True
    health_routes_enabled: bool = True

    def __post_init__(self) -> None:
        if self.live_extraction_model_enabled:
            if not self.extraction_endpoint.strip():
                raise MemoryConfigurationError("memory_live_extraction_requires_endpoint")
            if self.extraction_provider not in {"direct_json", "ollama"}:
                raise MemoryConfigurationError("memory_live_extraction_requires_explicit_provider")
        if self.ollama_request_mode not in {"auto", "ollama_schema", "ollama_json"}:
            raise MemoryConfigurationError("memory_ollama_request_mode_invalid")
        if not 500 <= self.extraction_max_input_chars <= 50_000:
            raise MemoryConfigurationError("memory_extraction_input_limit_out_of_range")
        if not 1 <= self.recall_max_records <= 20:
            raise MemoryConfigurationError("memory_recall_record_limit_out_of_range")
        if not 200 <= self.recall_max_chars <= 12_000:
            raise MemoryConfigurationError("memory_recall_char_limit_out_of_range")
        if not 0 <= self.recall_min_score <= 1:
            raise MemoryConfigurationError("memory_recall_score_out_of_range")

    @classmethod
    def from_settings(cls, settings: Settings) -> MemorySettings:
        provider = settings.memory_extraction_provider
        endpoint = settings.memory_extraction_endpoint
        if provider == "ollama" and not endpoint.strip():
            endpoint = f"{settings.ollama_url.rstrip('/')}/api/chat"
        return cls(
            enabled=settings.memory_enabled,
            extraction_enabled=settings.memory_extraction_enabled,
            foreground_extraction_enabled=settings.memory_extraction_enabled,
            post_turn_extraction_enabled=settings.memory_extraction_enabled,
            live_extraction_model_enabled=settings.memory_extraction_enabled,
            extraction_provider=provider,
            extraction_endpoint=endpoint,
            extraction_model=settings.memory_extraction_model or settings.chat_model,
            extraction_connect_timeout_seconds=settings.memory_extraction_connect_timeout_seconds,
            extraction_response_timeout_seconds=settings.memory_extraction_response_timeout_seconds,
            extraction_warmup_timeout_seconds=settings.memory_extraction_warmup_timeout_seconds,
            ollama_request_mode=settings.memory_ollama_request_mode,
            extraction_max_input_chars=settings.memory_extraction_max_input_chars,
            lexical_recall_enabled=True,
            secure_prompt_enabled=True,
            direct_answer_reads_enabled=True,
            research_recall_enabled=True,
            recall_max_records=settings.memory_recall_max_records,
            recall_max_chars=settings.memory_recall_max_chars,
            recall_min_score=settings.memory_recall_min_score,
            index_worker_enabled=settings.memory_index_worker_enabled,
            fts_index_enabled=settings.memory_index_worker_enabled,
            vector_index_enabled=(
                settings.memory_index_worker_enabled and settings.memory_semantic_recall_enabled
            ),
            semantic_recall_enabled=settings.memory_semantic_recall_enabled,
            reconciliation_enabled=settings.memory_index_worker_enabled,
            health_routes_enabled=True,
        )

    def owner_is_enabled(self, owner_id: str) -> bool:
        return bool(self.enabled and owner_id)
