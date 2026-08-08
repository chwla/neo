"""Backward-compatible imports for extensions that still use the old module name."""

from app.core.config import get_settings
from app.services.llm import (
    ChatTurn,
)
from app.services.llm import (
    LLMChatResult as OllamaChatResult,
)
from app.services.llm import (
    LLMMessage as OllamaMessage,
)
from app.services.llm import (
    OllamaClient as _OllamaClient,
)


class OllamaClient(_OllamaClient):
    def __init__(self, model=None, base_url=None, timeout=None, num_predict=None):
        settings = get_settings()
        super().__init__(
            model or settings.chat_model,
            base_url or settings.ollama_url,
            timeout or settings.chat_timeout_seconds,
            num_predict or settings.chat_num_predict,
        )


__all__ = ["ChatTurn", "OllamaChatResult", "OllamaClient", "OllamaMessage"]
