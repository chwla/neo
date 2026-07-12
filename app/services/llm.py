from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol

import requests
from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings

_registry_lock = RLock()


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMChatResult(BaseModel):
    content: str
    thinking: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    route_name: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    fallback_used: bool = False
    provider_request_id: str | None = None


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str


class LLMClient(Protocol):
    model: str

    def is_available(self) -> bool: ...
    def model_is_installed(self) -> bool: ...
    def chat(self, messages: list[LLMMessage], temperature: float = 0.4) -> str: ...
    def chat_with_metadata(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> LLMChatResult: ...
    def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> Iterator[dict[str, Any]]: ...
    def clean_response(self, content: str) -> str: ...
    def extract_thinking(self, content: str) -> str | None: ...


class LLMConfig(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["ollama", "openai_compatible"]
    model: str = Field(min_length=1, max_length=240)
    base_url: str = Field(min_length=1, max_length=500)
    api_key_env: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=1000)
    enabled: bool = True
    timeout_seconds: int = Field(default=240, ge=1, le=3600)
    num_predict: int = Field(default=160, ge=1, le=32768)

    @model_validator(mode="after")
    def normalize_url(self) -> LLMConfig:
        self.base_url = self.base_url.rstrip("/")
        return self

    def resolved_api_key(self) -> str | None:
        return (os.getenv(self.api_key_env) if self.api_key_env else None) or self.api_key

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"api_key"})
        data["has_api_key"] = bool(self.resolved_api_key())
        return data


class BaseLLMClient:
    def clean_response(self, content: str) -> str:
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    def extract_thinking(self, content: str) -> str | None:
        blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
        thinking = "\n\n".join(block.strip() for block in blocks if block.strip())
        return thinking or None

    def chat(self, messages: list[LLMMessage], temperature: float = 0.4) -> str:
        return self.chat_with_metadata(messages, temperature).content


class OllamaClient(BaseLLMClient):
    def __init__(self, model: str, base_url: str, timeout: int, num_predict: int) -> None:
        self.model, self.base_url = model, base_url.rstrip("/")
        self.timeout, self.num_predict = timeout, num_predict

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def model_is_installed(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            return any(item.get("name") == self.model for item in response.json().get("models", []))
        except requests.RequestException:
            return False

    def _options(self, temperature: float, num_predict: int | None) -> dict[str, Any]:
        return {"temperature": temperature, "num_predict": num_predict or self.num_predict}

    def chat_with_metadata(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> LLMChatResult:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [m.model_dump() for m in messages],
                "stream": False,
                "options": self._options(temperature, num_predict),
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        raw = str(payload["message"]["content"])
        prompt, completion = payload.get("prompt_eval_count"), payload.get("eval_count")
        elapsed = int((time.perf_counter() - started) * 1000)
        duration = payload.get("total_duration")
        return LLMChatResult(
            content=self.clean_response(raw),
            thinking=self.extract_thinking(raw),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=(
                prompt + completion
                if isinstance(prompt, int) and isinstance(completion, int)
                else None
            ),
            duration_ms=int(duration / 1_000_000) if duration else elapsed,
        )

    def chat_stream(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [m.model_dump() for m in messages],
                "stream": True,
                "options": self._options(temperature, num_predict),
            },
            stream=True,
            timeout=self.timeout,
        )
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            content = str(chunk.get("message", {}).get("content", ""))
            if content:
                yield {"type": "chunk", "content": content}
            if chunk.get("done"):
                prompt, completion = chunk.get("prompt_eval_count"), chunk.get("eval_count")
                duration = chunk.get("total_duration")
                yield {
                    "type": "done",
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": (
                        prompt + completion
                        if isinstance(prompt, int) and isinstance(completion, int)
                        else None
                    ),
                    "duration_ms": (
                        int(duration / 1_000_000)
                        if duration
                        else int((time.perf_counter() - started) * 1000)
                    ),
                }
                break


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(
        self, model: str, base_url: str, timeout: int, num_predict: int, api_key: str | None = None
    ) -> None:
        self.model, self.base_url = model, base_url.rstrip("/")
        self.timeout, self.num_predict, self.api_key = timeout, num_predict, api_key

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=3)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def model_is_installed(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=3)
            response.raise_for_status()
            return any(item.get("id") == self.model for item in response.json().get("data", []))
        except requests.RequestException:
            return False

    def _payload(
        self, messages: list[LLMMessage], temperature: float, num_predict: int | None, stream: bool
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": num_predict or self.num_predict,
            "stream": stream,
        }

    def chat_with_metadata(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> LLMChatResult:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=self._payload(messages, temperature, num_predict, False),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        raw = str(payload["choices"][0]["message"].get("content") or "")
        usage = payload.get("usage") or {}
        return LLMChatResult(
            content=self.clean_response(raw),
            thinking=self.extract_thinking(raw),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def chat_stream(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=self._payload(messages, temperature, num_predict, True),
            stream=True,
            timeout=self.timeout,
        )
        response.raise_for_status()
        usage: dict[str, Any] = {}
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            usage = chunk.get("usage") or usage
            content = str((chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or "")
            if content:
                yield {"type": "chunk", "content": content}
        yield {
            "type": "done",
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


class LLMRegistry:
    def __init__(self, path: Path | None = None) -> None:
        settings = get_settings()
        self.path = path or Path(settings.llm_config_path)
        self._settings = settings

    def _default(self) -> tuple[list[LLMConfig], str]:
        config = LLMConfig(
            id="ollama-default",
            name="Ollama",
            provider="ollama",
            model=self._settings.chat_model,
            base_url=self._settings.ollama_url,
            timeout_seconds=self._settings.chat_timeout_seconds,
            num_predict=self._settings.chat_num_predict,
        )
        return [config], config.id

    def load(self) -> tuple[list[LLMConfig], str]:
        with _registry_lock:
            if not self.path.exists():
                return self._default()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        configs = [LLMConfig.model_validate(item) for item in payload.get("llms", [])]
        if not configs:
            return self._default()
        active = payload.get("active_id")
        if active not in {item.id for item in configs if item.enabled}:
            active = next((item.id for item in configs if item.enabled), configs[0].id)
        return configs, active

    def save(self, configs: list[LLMConfig], active_id: str) -> str:
        with _registry_lock:
            if not configs or not any(item.enabled for item in configs):
                raise ValueError("At least one enabled LLM configuration is required")
            if active_id not in {item.id for item in configs if item.enabled}:
                active_id = next(item.id for item in configs if item.enabled)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active_id": active_id,
                "llms": [item.model_dump(exclude_none=True) for item in configs],
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            return active_id

    def list(self) -> tuple[list[LLMConfig], str]:
        return self.load()

    def upsert(self, config: LLMConfig) -> tuple[list[LLMConfig], str]:
        with _registry_lock:
            configs, active_id = self.load()
            index = next((i for i, item in enumerate(configs) if item.id == config.id), None)
            if index is None:
                configs.append(config)
            else:
                if config.api_key is None:
                    config.api_key = configs[index].api_key
                configs[index] = config
            active_id = self.save(configs, active_id)
            return configs, active_id

    def select(self, config_id: str) -> tuple[list[LLMConfig], str]:
        with _registry_lock:
            configs, _ = self.load()
            if not any(item.id == config_id and item.enabled for item in configs):
                raise ValueError("Enabled LLM configuration not found")
            self.save(configs, config_id)
            return configs, config_id

    def delete(self, config_id: str) -> tuple[list[LLMConfig], str]:
        with _registry_lock:
            configs, active_id = self.load()
            remaining = [item for item in configs if item.id != config_id]
            if len(remaining) == len(configs):
                raise KeyError("LLM configuration not found")
            if not remaining or not any(item.enabled for item in remaining):
                raise ValueError("At least one enabled LLM configuration is required")
            if active_id == config_id:
                active_id = next((item.id for item in remaining if item.enabled), remaining[0].id)
            active_id = self.save(remaining, active_id)
            return remaining, active_id

    def get(self, config_id: str | None = None) -> LLMConfig:
        configs, active = self.load()
        wanted = config_id or active
        config = next((item for item in configs if item.id == wanted and item.enabled), None)
        if config is None:
            raise ValueError(f"LLM configuration '{wanted}' was not found or is disabled")
        return config

    def client(
        self,
        config_id: str | None = None,
        *,
        num_predict: int | None = None,
        timeout: int | None = None,
    ) -> LLMClient:
        config = self.get(config_id)
        common = {
            "model": config.model,
            "base_url": config.base_url,
            "timeout": timeout or config.timeout_seconds,
            "num_predict": num_predict or config.num_predict,
        }
        if config.provider == "ollama":
            return OllamaClient(**common)
        return OpenAICompatibleClient(**common, api_key=config.resolved_api_key())


def get_llm_client(
    config_id: str | None = None,
    *,
    num_predict: int | None = None,
    timeout: int | None = None,
    route_name: str = "chat",
) -> LLMClient:
    # Provider Runtime retains the registry's routes while adding bounded
    # retries, rate limits, redacted audit records, and fallback metadata.
    from app.services.provider_runtime.client import ProviderRuntimeClient

    return ProviderRuntimeClient(route_name, num_predict=num_predict, timeout=timeout)
