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
    """One transcript turn.

    ``tool_calls`` carries an assistant turn that asked for tools;
    ``tool_call_id``/``name`` carry the matching ``role="tool"`` result. Both stay
    optional so every existing two-field call site keeps working untouched, and
    provider serialisation drops them when unset -- a provider that is sent
    ``"tool_calls": null`` rejects the request.

    ``images`` carries what the turn shows the model. Each entry is base64 image
    data, with or without a ``data:`` prefix; the two providers disagree about
    which they want, so both forms are accepted here and each serialiser emits
    the one its provider expects.
    """

    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    images: list[str] | None = None


class LLMChatResult(BaseModel):
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    thinking: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    route_name: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    fallback_used: bool = False
    provider_request_id: str | None = None
    finish_reason: str | None = None


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
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMChatResult: ...
    def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> Iterator[dict[str, Any]]: ...
    def clean_response(self, content: str) -> str: ...
    def extract_thinking(self, content: str) -> str | None: ...


class ProviderUsagePersistenceError(RuntimeError):
    """Neo could not persist provider usage after a model operation."""


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


def _tool_call_id(index: int, raw: dict[str, Any]) -> str:
    """Ollama omits tool-call ids, so one is synthesised to keep results correlatable."""

    return str(raw.get("id") or f"call_{index}")


def _decode_arguments(value: Any) -> dict[str, Any]:
    """OpenAI sends arguments as a JSON *string*; Ollama sends an object."""

    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def normalize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    """Flatten either provider's tool-call shape into ``{id, name, arguments}``."""

    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        calls.append(
            {
                "id": _tool_call_id(index, raw),
                "name": name,
                "arguments": _decode_arguments(function.get("arguments")),
            }
        )
    return calls


#: Base64 opens with a stable prefix per container format, so the media type can
#: be read off the encoded string without decoding it or carrying it separately.
_B64_MAGIC = (
    ("iVBORw0KGgo", "image/png"),
    ("/9j/", "image/jpeg"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)


def _image_b64(value: str) -> str:
    """Bare base64, whether a data URL or bare base64 came in."""

    return value.split(";base64,", 1)[1] if value.startswith("data:") else value


def _image_data_url(value: str) -> str:
    """A data URL, whether a data URL or bare base64 came in."""

    if value.startswith("data:"):
        return value
    mime = next((m for prefix, m in _B64_MAGIC if value.startswith(prefix)), "image/png")
    return f"data:{mime};base64,{value}"


def _ollama_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Serialise for Ollama's /api/chat, which takes object arguments and no ids."""

    payload: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [
                {"function": {"name": call["name"], "arguments": call.get("arguments") or {}}}
                for call in message.tool_calls
            ]
        if message.role == "tool" and message.name:
            item["tool_name"] = message.name
        if message.images:
            # Ollama takes a per-message array of bare base64 -- a data URL in
            # here is accepted and then silently ignored.
            item["images"] = [_image_b64(image) for image in message.images]
        payload.append(item)
    return payload


def _openai_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Serialise for /chat/completions, which requires ids and stringified arguments."""

    payload: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool" and message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.images:
            # The block form is only emitted when there is something to show:
            # some gateways reject a block list where they expected a string.
            item["content"] = [
                {"type": "text", "text": message.content},
                *(
                    {"type": "image_url", "image_url": {"url": _image_data_url(image)}}
                    for image in message.images
                ),
            ]
        payload.append(item)
    return payload


class BaseLLMClient:
    def clean_response(self, content: str) -> str:
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    def extract_thinking(self, content: str) -> str | None:
        blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
        thinking = "\n\n".join(block.strip() for block in blocks if block.strip())
        return thinking or None

    def chat(self, messages: list[LLMMessage], temperature: float = 0.4) -> str:
        return self.chat_with_metadata(messages, temperature).content


#: Capability answers are cached per (base_url, model): the probe is a network
#: round trip and the answer only changes when a model is re-pulled.
_TOOL_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}


def ollama_supports_tools(base_url: str, model: str, timeout: int = 5) -> bool:
    """Ask Ollama whether a model can do native tool calling.

    The registry carries a ``supports_tools`` flag, but it defaults to false and
    nothing populates it, so trusting it alone silently downgrades every model to
    the text fallback. Ollama reports the answer directly, so ask.
    """

    key = (base_url.rstrip("/"), model)
    if key in _TOOL_SUPPORT_CACHE:
        return _TOOL_SUPPORT_CACHE[key]
    supported = False
    try:
        response = requests.post(
            f"{key[0]}/api/show", json={"model": model}, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
        capabilities = payload.get("capabilities") or []
        supported = "tools" in capabilities
        if not supported:
            # Older Ollama builds omit `capabilities`; the template mentioning
            # tools is the next best signal.
            supported = ".Tools" in str(payload.get("template") or "")
    except Exception:
        supported = False
    _TOOL_SUPPORT_CACHE[key] = supported
    return supported


_VISION_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}


def ollama_supports_vision(base_url: str, model: str, timeout: int = 5) -> bool:
    """Ask Ollama whether a model can accept images.

    The same shape as ``ollama_supports_tools``, and for the same reason: the
    registry's ``supports_vision`` column defaults to false and nothing has ever
    populated it, so trusting it alone reports every model as blind.
    """

    key = (base_url.rstrip("/"), model)
    if key in _VISION_SUPPORT_CACHE:
        return _VISION_SUPPORT_CACHE[key]
    supported = False
    try:
        response = requests.post(f"{key[0]}/api/show", json={"model": model}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        supported = "vision" in (payload.get("capabilities") or [])
    except Exception:
        supported = False
    _VISION_SUPPORT_CACHE[key] = supported
    return supported


_THINKING_SUPPORT_CACHE: dict[tuple[str, str], bool] = {}


def ollama_supports_thinking(base_url: str, model: str, timeout: int = 5) -> bool:
    """Ask Ollama whether a model reasons before it answers.

    Asked so that ``think`` can be *turned off* safely. A model that has never
    heard of the field rejects the request outright, so the answer has to come
    from the provider rather than from an assumption about the model name.
    """

    key = (base_url.rstrip("/"), model)
    if key in _THINKING_SUPPORT_CACHE:
        return _THINKING_SUPPORT_CACHE[key]
    supported = False
    try:
        response = requests.post(f"{key[0]}/api/show", json={"model": model}, timeout=timeout)
        response.raise_for_status()
        supported = "thinking" in (response.json().get("capabilities") or [])
    except Exception:
        supported = False
    _THINKING_SUPPORT_CACHE[key] = supported
    return supported


class OllamaClient(BaseLLMClient):
    def __init__(
        self,
        model: str,
        base_url: str,
        timeout: int,
        num_predict: int,
        num_ctx: int | None = None,
        keep_alive: str | None = None,
        disable_thinking: bool = False,
    ) -> None:
        self.model, self.base_url = model, base_url.rstrip("/")
        self.timeout, self.num_predict = timeout, num_predict
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.disable_thinking = disable_thinking

    def _body(self, messages: list[LLMMessage], temperature: float, num_predict, *, stream: bool):
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _ollama_messages(messages),
            "stream": stream,
            "options": self._options(temperature, num_predict),
        }
        if self.keep_alive:
            # Without this Ollama applies its own 5-minute idle timeout, which
            # is shorter than the pause before someone opens a new chat -- so
            # the first message of that chat reloads the whole model.
            body["keep_alive"] = self.keep_alive
        if self.disable_thinking and self.supports_thinking():
            # By far the largest thing a low-effort turn gives up. Measured on
            # gemma4 answering "hi": 137 of the 151 tokens it generated were
            # reasoning about how to greet someone, and at 23 tok/s that is the
            # whole of the wait. Worth every token on a hard question, which is
            # why this is a setting and not a change.
            body["think"] = False
        return body

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def supports_tools(self) -> bool:
        return ollama_supports_tools(self.base_url, self.model)

    def supports_vision(self) -> bool:
        return ollama_supports_vision(self.base_url, self.model)

    def supports_thinking(self) -> bool:
        return ollama_supports_thinking(self.base_url, self.model)

    def model_is_installed(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            response.raise_for_status()
            return any(item.get("name") == self.model for item in response.json().get("models", []))
        except requests.RequestException:
            return False

    def _options(self, temperature: float, num_predict: int | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": num_predict or self.num_predict,
        }
        if self.num_ctx:
            # Ollama defaults to a small context (4096 as of 0.32) and silently drops the
            # oldest tokens past it. Sizing the window to the request keeps a long prompt
            # intact instead of answering a quietly truncated version of it.
            options["num_ctx"] = self.num_ctx
        return options

    def chat_with_metadata(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMChatResult:
        started = time.perf_counter()
        body: dict[str, Any] = self._body(
            messages, temperature, num_predict, stream=False
        )
        if tools:
            body["tools"] = tools
        response = requests.post(
            f"{self.base_url}/api/chat",
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        message = payload["message"]
        raw = str(message.get("content") or "")
        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        native_thinking = str(message.get("thinking") or message.get("reasoning") or "").strip()
        prompt, completion = payload.get("prompt_eval_count"), payload.get("eval_count")
        elapsed = int((time.perf_counter() - started) * 1000)
        duration = payload.get("total_duration")
        return LLMChatResult(
            content=self.clean_response(raw),
            tool_calls=tool_calls,
            thinking=native_thinking or self.extract_thinking(raw),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=(
                prompt + completion
                if isinstance(prompt, int) and isinstance(completion, int)
                else None
            ),
            duration_ms=int(duration / 1_000_000) if duration else elapsed,
            finish_reason=str(payload.get("done_reason") or "stop"),
            provider_name="ollama",
            model_name=self.model,
        )

    def chat_stream(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            json=self._body(messages, temperature, num_predict, stream=True),
            stream=True,
            timeout=self.timeout,
        )
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            message = chunk.get("message", {})
            content = str(message.get("content", ""))
            thinking = str(message.get("thinking") or message.get("reasoning") or "")
            if thinking:
                yield {"type": "thinking", "content": thinking}
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
                    "finish_reason": str(chunk.get("done_reason") or "stop"),
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
        self,
        messages: list[LLMMessage],
        temperature: float,
        num_predict: int | None,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(messages),
            "temperature": temperature,
            "max_tokens": num_predict or self.num_predict,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        return payload

    def chat_with_metadata(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMChatResult:
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=self._payload(messages, temperature, num_predict, False, tools),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        message = payload["choices"][0]["message"]
        raw = str(message.get("content") or "")
        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        native_thinking = str(
            message.get("reasoning")
            or message.get("reasoning_content")
            or message.get("thinking")
            or ""
        ).strip()
        usage = payload.get("usage") or {}
        return LLMChatResult(
            content=self.clean_response(raw),
            tool_calls=tool_calls,
            thinking=native_thinking or self.extract_thinking(raw),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            duration_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=str(payload["choices"][0].get("finish_reason") or "stop"),
            provider_name="openai_compatible",
            model_name=self.model,
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
        finish_reason: str | None = None
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            usage = chunk.get("usage") or usage
            choice = (chunk.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta", {})
            thinking = str(
                delta.get("reasoning")
                or delta.get("reasoning_content")
                or delta.get("thinking")
                or ""
            )
            if thinking:
                yield {"type": "thinking", "content": thinking}
            content = str(delta.get("content") or "")
            if content:
                yield {"type": "chunk", "content": content}
        yield {
            "type": "done",
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "finish_reason": str(finish_reason or "stop"),
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
            model=self._settings.default_model,
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
    disable_thinking: bool = False,
) -> LLMClient:
    # Provider Runtime retains the registry's routes while adding bounded
    # retries, rate limits, redacted audit records, and fallback metadata.
    from app.services.provider_runtime.client import ProviderRuntimeClient

    return ProviderRuntimeClient(
        route_name,
        num_predict=num_predict,
        timeout=timeout,
        disable_thinking=disable_thinking,
    )
