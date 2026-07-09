from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ProviderType = Literal["ollama", "openai_compatible", "disabled", "mock"]


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
