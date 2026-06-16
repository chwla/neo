from __future__ import annotations

import re
from collections.abc import Iterator

import requests
from pydantic import BaseModel, Field


class OllamaMessage(BaseModel):
    role: str
    content: str


class OllamaClient:
    """Small HTTP client for a local Ollama chat model."""

    def __init__(
        self,
        model: str = "qwen3:8b-q4_K_M",
        base_url: str = "http://127.0.0.1:11434",
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [message.model_dump() for message in messages],
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self.clean_response(str(response.json()["message"]["content"]))

    def chat_stream(
        self,
        messages: list[OllamaMessage],
        temperature: float = 0.4,
    ) -> Iterator[str]:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [message.model_dump() for message in messages],
                "stream": True,
                "options": {"temperature": temperature},
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
                yield str(chunk["message"].get("content", ""))
            if chunk.get("done"):
                break

    def clean_response(self, content: str) -> str:
        """Hide Qwen thinking traces from the chat surface."""

        cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str
