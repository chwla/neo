from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.llm_registry import store
from app.services.llm_registry.types import (
    ModelCreate,
    ModelUpdate,
    ProviderCreate,
    ProviderUpdate,
    RouteUpdate,
)

DEFAULT_ROUTES = (
    "chat",
    "research",
    "agent",
    "coding_agent",
    "patch_proposal",
    "summarization",
    "embedding",
    "title_generation",
)


def _slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")[:70]
    return cleaned or fallback


class LLMRegistryService:
    def __init__(self, *, initialize: bool = True) -> None:
        if initialize:
            store.initialize_llm_registry_tables()
            self.ensure_defaults()

    def ensure_defaults(self) -> None:
        settings = get_settings()
        providers = store.list_rows("workspace_llm_providers", "provider", "priority, name")
        if not providers:
            provider_type = settings.llm_provider.replace("-", "_")
            if provider_type not in {"ollama", "openai_compatible"}:
                provider_type = "disabled"
            default_model = (
                settings.openai_compat_model or settings.default_model
                if provider_type == "openai_compatible"
                else settings.default_model
            )
            provider_id = (
                "ollama-default" if provider_type == "ollama" else "openai-compatible-default"
            )
            base_url = (
                settings.ollama_url
                if provider_type == "ollama"
                else settings.openai_compat_base_url
            )
            if not base_url:
                provider_type = "disabled"
                provider_id = "disabled-default"
            provider = self.create_provider(
                ProviderCreate(
                    id=provider_id,
                    name="Ollama" if provider_type == "ollama" else "Default LLM Provider",
                    provider_type=provider_type,
                    base_url=base_url or None,
                    api_key_ref=(
                        settings.openai_compat_api_key_ref
                        if provider_type == "openai_compatible"
                        else None
                    ),
                    default_model=default_model,
                    enabled=provider_type != "disabled",
                    timeout_seconds=settings.chat_timeout_seconds,
                    metadata={"source": "environment"},
                )
            )
            model = self.create_model(
                ModelCreate(
                    id=f"{provider_id}-model",
                    provider_id=provider["id"],
                    model_name=default_model,
                    display_name=default_model,
                    max_output_tokens=settings.chat_num_predict,
                    enabled=provider_type != "disabled",
                    metadata={"source": "environment"},
                )
            )
            embedding_model = model
            if provider_type == "ollama":
                embedding_model = self.create_model(
                    ModelCreate(
                        id=f"{provider_id}-embedding-model",
                        provider_id=provider["id"],
                        model_name=settings.embedding_model,
                        display_name=settings.embedding_model,
                        supports_embeddings=True,
                        enabled=True,
                        metadata={"source": "environment"},
                    )
                )
            now = store.now_iso()
            for route_name in DEFAULT_ROUTES:
                route_model = embedding_model if route_name == "embedding" else model
                store.insert_route(
                    {
                        "id": str(uuid.uuid4()),
                        "route_name": route_name,
                        "provider_id": provider["id"],
                        "model_id": route_model["id"],
                        "fallback_provider_id": None,
                        "fallback_model_id": None,
                        "temperature": 0.4 if route_name == "chat" else 0.2,
                        "max_output_tokens": route_model.get("max_output_tokens"),
                        "enabled": True,
                        "metadata": {"source": "default"},
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        self._refresh_environment_default()
        self._migrate_legacy_json()

    def _refresh_environment_default(self) -> None:
        """Keep only automatically-created Ollama defaults aligned with the container.

        User-created providers, models, and routes are intentionally left untouched.
        This lets an upgraded Docker image recover from an unavailable bundled
        default model without overwriting an explicit provider choice.
        """

        settings = get_settings()
        provider = self.get_provider("ollama-default")
        model = self.get_model("ollama-default-model")
        if (
            provider is None
            or model is None
            or provider.get("provider_type") != "ollama"
            or provider.get("metadata", {}).get("source") != "environment"
            or model.get("metadata", {}).get("source") != "environment"
        ):
            return

        if provider.get("default_model") != settings.default_model:
            self.update_provider(
                provider["id"],
                ProviderUpdate(default_model=settings.default_model),
            )
        if model.get("model_name") != settings.default_model:
            self.update_model(
                model["id"],
                ModelUpdate(
                    model_name=settings.default_model,
                    display_name=settings.default_model,
                ),
            )

        for route in self.list_routes():
            if (
                route.get("provider_id") == provider["id"]
                and route.get("model_id") == model["id"]
                and route.get("metadata", {}).get("source") == "default"
            ):
                self.update_route(
                    route["route_name"],
                    RouteUpdate(
                        provider_id=provider["id"],
                        model_id=model["id"],
                        metadata=route.get("metadata", {}),
                    ),
                )

    def _migrate_legacy_json(self) -> None:
        path = Path(get_settings().llm_config_path)
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        active = payload.get("active_id")
        for legacy in payload.get("llms", []):
            config_id = _slug(str(legacy.get("id") or "legacy"), "legacy")
            existing = self.get_provider(config_id)
            if existing:
                if config_id == active:
                    route = self.get_route("chat")
                    model = next(
                        (item for item in self.list_models(config_id) if item["enabled"]),
                        None,
                    )
                    if model and (route or {}).get("metadata", {}).get("source") in {
                        "default",
                        "legacy",
                    }:
                        self.update_route(
                            "chat",
                            RouteUpdate(
                                provider_id=config_id,
                                model_id=model["id"],
                                metadata={"source": "legacy"},
                            ),
                        )
                continue
            provider_type = str(legacy.get("provider") or "ollama")
            if provider_type not in {"ollama", "openai_compatible"}:
                continue
            try:
                provider = self.create_provider(
                    ProviderCreate(
                        id=config_id,
                        name=str(legacy.get("name") or config_id),
                        provider_type=provider_type,
                        base_url=legacy.get("base_url"),
                        api_key_ref=legacy.get("api_key_env"),
                        default_model=legacy.get("model"),
                        enabled=bool(legacy.get("enabled", True)),
                        timeout_seconds=int(legacy.get("timeout_seconds", 240)),
                        metadata={
                            "source": "neo_llms.json",
                            "plaintext_key_ignored": bool(legacy.get("api_key")),
                        },
                    )
                )
                model = self.create_model(
                    ModelCreate(
                        id=f"{config_id}-model",
                        provider_id=provider["id"],
                        model_name=str(legacy.get("model") or provider["default_model"]),
                        display_name=str(legacy.get("model") or provider["default_model"]),
                        max_output_tokens=int(legacy.get("num_predict", 160)),
                        enabled=bool(legacy.get("enabled", True)),
                        metadata={"source": "neo_llms.json"},
                    )
                )
                if config_id == active:
                    self.update_route(
                        "chat",
                        RouteUpdate(
                            provider_id=provider["id"],
                            model_id=model["id"],
                            metadata={"source": "legacy"},
                        ),
                    )
            except (ValueError, sqlite3.IntegrityError):
                continue

    def list_providers(self) -> list[dict[str, Any]]:
        items = store.list_rows("workspace_llm_providers", "provider", "priority, name")
        for item in items:
            item["api_key_configured"] = bool(
                item.get("api_key_ref") and os.getenv(item["api_key_ref"])
            )
        return items

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        item = store.get_row("workspace_llm_providers", "provider", provider_id)
        if item:
            item["api_key_configured"] = bool(
                item.get("api_key_ref") and os.getenv(item["api_key_ref"])
            )
        return item

    def create_provider(self, request: ProviderCreate) -> dict[str, Any]:
        now = store.now_iso()
        data = request.model_dump()
        data["id"] = data["id"] or str(uuid.uuid4())
        data.update(created_at=now, updated_at=now)
        try:
            return store.insert_provider(data)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Provider id already exists.") from exc

    def update_provider(self, provider_id: str, request: ProviderUpdate) -> dict[str, Any]:
        current = self.get_provider(provider_id)
        if not current:
            raise LookupError("LLM provider not found.")
        updates = request.model_dump(exclude_unset=True)
        if "base_url" in updates and updates["base_url"]:
            updates["base_url"] = updates["base_url"].rstrip("/")
        updates["updated_at"] = store.now_iso()
        return store.update_row("workspace_llm_providers", "provider", provider_id, updates)

    def delete_provider(self, provider_id: str) -> None:
        if not self.get_provider(provider_id):
            raise LookupError("LLM provider not found.")
        try:
            if not store.delete_row("workspace_llm_providers", provider_id):
                raise LookupError("LLM provider not found.")
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Provider is referenced by a model or route; disable it instead."
            ) from exc

    def list_models(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        items = store.list_rows("workspace_llm_models", "model", "display_name, model_name")
        return [item for item in items if not provider_id or item["provider_id"] == provider_id]

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        return store.get_row("workspace_llm_models", "model", model_id)

    def create_model(self, request: ModelCreate) -> dict[str, Any]:
        if not self.get_provider(request.provider_id):
            raise LookupError("LLM provider not found.")
        now = store.now_iso()
        data = request.model_dump()
        data["id"] = data["id"] or str(uuid.uuid4())
        data.update(created_at=now, updated_at=now)
        try:
            return store.insert_model(data)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Model id already exists.") from exc

    def update_model(self, model_id: str, request: ModelUpdate) -> dict[str, Any]:
        if not self.get_model(model_id):
            raise LookupError("LLM model not found.")
        updates = request.model_dump(exclude_unset=True)
        updates["updated_at"] = store.now_iso()
        return store.update_row("workspace_llm_models", "model", model_id, updates)

    def delete_model(self, model_id: str) -> None:
        if not self.get_model(model_id):
            raise LookupError("LLM model not found.")
        try:
            store.delete_row("workspace_llm_models", model_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Model is referenced by a route; disable it instead.") from exc

    def list_routes(self) -> list[dict[str, Any]]:
        return store.list_rows("workspace_llm_routes", "route", "route_name")

    def get_route(self, route_name: str) -> dict[str, Any] | None:
        return store.get_row("workspace_llm_routes", "route", route_name, key="route_name")

    def update_route(self, route_name: str, request: RouteUpdate) -> dict[str, Any]:
        route = self.get_route(route_name)
        if not route:
            raise LookupError("LLM route not found.")
        updates = request.model_dump(exclude_unset=True)
        if "metadata" not in updates and any(
            key in updates
            for key in (
                "provider_id",
                "model_id",
                "fallback_provider_id",
                "fallback_model_id",
            )
        ):
            updates["metadata"] = {**route.get("metadata", {}), "source": "registry"}
        self._validate_route_targets({**route, **updates})
        updates["updated_at"] = store.now_iso()
        return store.update_row("workspace_llm_routes", "route", route["id"], updates)

    def _validate_route_targets(self, route: dict[str, Any]) -> None:
        for prefix in ("", "fallback_"):
            provider_id, model_id = (
                route.get(f"{prefix}provider_id"),
                route.get(f"{prefix}model_id"),
            )
            if not provider_id and not model_id:
                continue
            provider = self.get_provider(provider_id) if provider_id else None
            model = self.get_model(model_id) if model_id else None
            if not provider or not model or model["provider_id"] != provider["id"]:
                raise ValueError(f"{prefix or 'primary_'}provider/model mapping is invalid.")

    def resolve(self, route_name: str, config_id: str | None = None) -> dict[str, Any]:
        route = self.get_route(route_name)
        if not route or not route["enabled"]:
            raise LookupError(f"LLM route '{route_name}' is missing or disabled.")
        if config_id:
            provider = self.get_provider(config_id)
            models = self.list_models(config_id) if provider else []
            model = next((item for item in models if item["enabled"]), None)
            if provider and model:
                route = {**route, "provider_id": provider["id"], "model_id": model["id"]}
        self._validate_route_targets(route)
        return route
