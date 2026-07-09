from __future__ import annotations

import os

from app.services.llm import BaseLLMClient, LLMChatResult, OllamaClient, OpenAICompatibleClient


class ProviderConfigurationError(RuntimeError):
    pass


class DisabledClient(BaseLLMClient):
    def __init__(self, model: str, message: str = "LLM provider is disabled.") -> None:
        self.model = model
        self.message = message

    def is_available(self) -> bool:
        return False

    def model_is_installed(self) -> bool:
        return False

    def chat_with_metadata(self, *_args, **_kwargs):
        raise ProviderConfigurationError(self.message)

    def chat_stream(self, *_args, **_kwargs):
        raise ProviderConfigurationError(self.message)


class MockClient(BaseLLMClient):
    def __init__(self, model: str) -> None:
        self.model = model

    def is_available(self) -> bool:
        return True

    def model_is_installed(self) -> bool:
        return True

    def chat_with_metadata(self, messages, temperature=0.4, num_predict=None):
        content = messages[-1].content if messages else "mock"
        return LLMChatResult(content=f"Mock: {content}", total_tokens=0, duration_ms=0)

    def chat_stream(self, messages, temperature=0.4, num_predict=None):
        result = self.chat_with_metadata(messages, temperature, num_predict)
        yield {"type": "chunk", "content": result.content}
        yield {"type": "done", "total_tokens": 0, "duration_ms": 0}


def build_client(provider: dict, model: dict, *, timeout=None, num_predict=None):
    if not provider.get("enabled") or not model.get("enabled"):
        return DisabledClient(model.get("model_name") or "disabled")
    provider_type = provider["provider_type"]
    if provider_type == "disabled":
        return DisabledClient(model.get("model_name") or "disabled")
    if provider_type == "mock":
        return MockClient(model["model_name"])
    base_url = provider.get("base_url")
    if not base_url:
        raise ProviderConfigurationError("LLM provider has no configured base URL.")
    common = {
        "model": model["model_name"],
        "base_url": base_url,
        "timeout": timeout or provider["timeout_seconds"],
        "num_predict": num_predict or model.get("max_output_tokens") or 160,
    }
    if provider_type == "ollama":
        return OllamaClient(**common)
    if provider_type == "openai_compatible":
        key_ref = provider.get("api_key_ref")
        api_key = os.getenv(key_ref) if key_ref else None
        if key_ref and not api_key:
            raise ProviderConfigurationError(
                f"API key environment variable '{key_ref}' is not configured."
            )
        return OpenAICompatibleClient(**common, api_key=api_key)
    raise ProviderConfigurationError(f"Unsupported LLM provider type '{provider_type}'.")
