from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ProviderType = Literal["ollama", "openai_compatible", "disabled", "mock"]


#: Keys Neo itself uses for web search. Named like provider keys, so the suffix
#: rule alone let a model configuration send them to any endpoint.
RESERVED_KEY_NAMES = frozenset(
    {
        "TAVILY_API_KEY",
        "BRAVE_API_KEY",
        "DATA_BRAVE_API_KEY",
        "SERPER_API_KEY",
        "WEB_SEARCH_API_KEY",
    }
)


def check_key_env_name(name: str | None) -> str | None:
    """Refuse an environment variable a provider may not take its key from.

    A provider's key is read from the named variable and sent, as a bearer token,
    to whatever base URL the provider names, and any signed-in session can set
    both. So the name has to be one meant for a model provider: never one of
    Neo's own settings (``NEO_CONNECTOR_MASTER_KEY`` opens every profile's sealed
    tokens), and only the conventional ``*_API_KEY`` spelling, which keeps cloud
    and VCS credentials, session tokens and the rest of the environment out.
    """

    if name is None:
        return None
    upper = name.upper()
    if upper in RESERVED_KEY_NAMES:
        raise ValueError(
            f"{name} is Neo's own search key, not a model provider's, and cannot be "
            "sent to a model endpoint."
        )
    if upper.startswith("NEO_") or not upper.endswith("_API_KEY"):
        raise ValueError(
            "The key must come from an environment variable named like a provider "
            "API key (for example OPENAI_API_KEY), not one of Neo's own NEO_ settings."
        )
    return name


class ProviderCreate(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    name: str = Field(min_length=1, max_length=120)
    provider_type: ProviderType
    base_url: str | None = Field(default=None, max_length=500)
    api_key_ref: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
    default_model: str | None = Field(default=None, max_length=240)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_key_ref")
    @classmethod
    def key_env_is_a_provider_key(cls, value: str | None) -> str | None:
        return check_key_env_name(value)

    @model_validator(mode="after")
    def normalize(self) -> ProviderCreate:
        self.name = self.name.strip()
        self.base_url = self.base_url.rstrip("/") if self.base_url else None
        self.default_model = self.default_model.strip() if self.default_model else None
        if self.provider_type in {"ollama", "openai_compatible"} and not self.base_url:
            raise ValueError("A base URL is required for enabled network providers.")
        return self


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider_type: ProviderType | None = None
    base_url: str | None = Field(default=None, max_length=500)
    api_key_ref: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
    default_model: str | None = Field(default=None, max_length=240)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    metadata: dict[str, Any] | None = None

    @field_validator("api_key_ref")
    @classmethod
    def key_env_is_a_provider_key(cls, value: str | None) -> str | None:
        return check_key_env_name(value)


class ModelCreate(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,119}$")
    provider_id: str = Field(min_length=1, max_length=80)
    model_name: str = Field(min_length=1, max_length=240)
    display_name: str | None = Field(default=None, max_length=240)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    supports_tools: bool = False
    supports_json: bool = False
    supports_vision: bool = False
    supports_embeddings: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUpdate(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=240)
    display_name: str | None = Field(default=None, max_length=240)
    capabilities: dict[str, Any] | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    supports_tools: bool | None = None
    supports_json: bool | None = None
    supports_vision: bool | None = None
    supports_embeddings: bool | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class RouteUpdate(BaseModel):
    provider_id: str | None = None
    model_id: str | None = None
    fallback_provider_id: str | None = None
    fallback_model_id: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class HealthRequest(BaseModel):
    route_name: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
