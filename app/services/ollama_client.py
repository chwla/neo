from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import requests
from pydantic import BaseModel, Field

from app.core.config import get_settings


class OllamaMessage(BaseModel):
    role: str
    content: str


class OllamaChatResult(BaseModel):
    content: str
    thinking: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None


class OllamaClient:
    """Small HTTP client for a local Ollama chat model."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        num_predict: int | None = None,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.chat_model
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.timeout = timeout or settings.chat_timeout_seconds
        self.num_predict = num_predict or settings.chat_num_predict

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
        except requests.RequestException:
            return False
        return True

    def model_is_installed(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
        except requests.RequestException:
            return False
        models = response.json().get("models", [])
        return any(model.get("name") == self.model for model in models)

    def chat(self, messages: list[OllamaMessage], temperature: float = 0.4) -> str:
        return self.chat_with_metadata(messages, temperature).content

    def _options(self, temperature: float, num_predict: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": num_predict or self.num_predict,
        }
        return options

    def chat_with_metadata(
        self,
        messages: list[OllamaMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> OllamaChatResult:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [message.model_dump() for message in messages],
                "stream": False,
                "options": self._options(temperature, num_predict),
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        payload = response.json()
        raw_content = str(payload["message"]["content"])
        prompt_tokens = payload.get("prompt_eval_count")
        completion_tokens = payload.get("eval_count")
        total_duration = payload.get("total_duration")
        duration_ms = int(total_duration / 1_000_000) if total_duration else elapsed_ms
        return OllamaChatResult(
            content=self.clean_response(raw_content),
            thinking=self.extract_thinking(raw_content),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=(
                prompt_tokens + completion_tokens
                if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
                else None
            ),
            duration_ms=duration_ms,
        )

    def chat_stream(
        self,
        messages: list[OllamaMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [message.model_dump() for message in messages],
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
            chunk = requests.models.complexjson.loads(line)
            if "message" in chunk:
                content = str(chunk["message"].get("content", ""))
                if content:
                    yield {"type": "chunk", "content": content}
            if chunk.get("done"):
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                prompt_tokens = chunk.get("prompt_eval_count")
                completion_tokens = chunk.get("eval_count")
                total_duration = chunk.get("total_duration")
                duration_ms = int(total_duration / 1_000_000) if total_duration else elapsed_ms
                yield {
                    "type": "done",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": (
                        prompt_tokens + completion_tokens
                        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int)
                        else None
                    ),
                    "duration_ms": duration_ms,
                }
                break

    def clean_response(self, content: str) -> str:
        """Hide Qwen thinking traces from the chat surface."""

        cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    def extract_thinking(self, content: str) -> str | None:
        blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
        thinking = "\n\n".join(block.strip() for block in blocks if block.strip())
        return thinking or None


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str
