from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import requests

from app.core.config import get_settings
from app.services.llm import LLMConfig, LLMRegistry, ollama_supports_vision
from app.services.llm_registry import store
from app.services.llm_registry.types import (
    ModelCreate,
    ModelUpdate,
    ProviderCreate,
    ProviderUpdate,
    RouteUpdate,
)

# Providers already attempted in this process, so an unreachable Ollama costs one
# short timeout per provider per process rather than one per request.
_DISCOVERY_ATTEMPTED: set[str] = set()

#: The routes the composer's model picker binds. Both chat and agent runs are
#: started from the same dropdown, so a selection has to move both.
PICKER_ROUTES = ("chat", "agent")

DEFAULT_ROUTES = (
    "chat",
    "research",
    "agent",
    "coding_agent",
    "patch_proposal",
    "summarization",
    "embedding",
    "title_generation",
    "vision",
)

#: Routes that want a model with a particular capability rather than whatever
#: answers chat. Anything absent here follows the chat route.
_ROUTE_CAPABILITY = {"embedding": "supports_embeddings", "vision": "supports_vision"}


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
        self._autodiscover_once()
        self._ensure_default_routes()

    def _autodiscover_once(self) -> None:
        """Register whatever the local Ollama already serves, once per provider.

        Without this a user who pulls a new model has to find the Sync button before it
        appears anywhere in Neo. Failures are silent: discovery is a convenience, and a
        stopped Ollama must never stop the registry from initialising.
        """
        settings = get_settings()
        for provider in self.list_providers():
            if provider.get("provider_type") != "ollama" or not provider.get("enabled"):
                continue
            metadata = provider.get("metadata") or {}
            # A profile that finished discovery before context windows were recorded still
            # needs one more pass, otherwise its models keep the conservative default and
            # long prompts are rejected against a limit the model does not actually have.
            missing_context = any(
                not model.get("context_window") for model in self.list_models(provider["id"])
            )
            if metadata.get("discovery_completed") and not missing_context:
                continue
            key = f"{settings.database_url}::{provider['id']}"
            if key in _DISCOVERY_ATTEMPTED:
                continue
            _DISCOVERY_ATTEMPTED.add(key)
            try:
                self.discover_provider_models(provider["id"], timeout=2)
            except Exception:  # noqa: BLE001 - convenience path, never fatal
                continue
            self.update_provider(
                provider["id"],
                ProviderUpdate(metadata={**metadata, "discovery_completed": True}),
            )

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

    def _already_served(self, legacy: dict[str, Any]) -> bool:
        """True when some registered provider on the same endpoint already has this model."""
        model_name = str(legacy.get("model") or "").strip()
        base_url = str(legacy.get("base_url") or "").rstrip("/")
        if not model_name or not base_url:
            return False
        for provider in self.list_providers():
            if str(provider.get("base_url") or "").rstrip("/") != base_url:
                continue
            if any(item["model_name"] == model_name for item in self.list_models(provider["id"])):
                return True
        return False

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
            if self._already_served(legacy):
                # A picker entry for a model an existing provider already serves. Importing
                # it would create a second provider for the same endpoint.
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
            provider = store.insert_provider(data)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Provider id already exists.") from exc
        if provider.get("default_model"):
            # Otherwise "Add provider" leaves you with a provider you cannot actually pick.
            self.create_model(
                ModelCreate(
                    provider_id=provider["id"],
                    model_name=str(provider["default_model"]),
                    display_name=str(provider["default_model"]),
                    enabled=bool(provider.get("enabled", True)),
                    metadata={"source": "provider_default"},
                )
            )
        return provider

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
        existing = next(
            (
                item
                for item in self.list_models(request.provider_id)
                if item["model_name"] == request.model_name
            ),
            None,
        )
        if existing:
            # Adding a model the provider already has is a no-op rather than an error, so
            # discovery, the legacy import, and the Add model form cannot duplicate rows.
            self._sync_model_to_picker(existing)
            return existing
        now = store.now_iso()
        data = request.model_dump()
        data["id"] = data["id"] or str(uuid.uuid4())
        data.update(created_at=now, updated_at=now)
        try:
            created = store.insert_model(data)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Model id already exists.") from exc
        self._sync_model_to_picker(created)
        return created

    # Model names an Ollama install reports as embedders rather than chat models.
    _EMBEDDING_HINTS = ("embed", "bge", "gte", "minilm")

    @staticmethod
    def _served_context_window(base_url: str, model_name: str, timeout: int) -> int | None:
        """Ask Ollama how much context this model actually reads.

        The key is namespaced by architecture (``qwen3moe.context_length``,
        ``gemma4.context_length``), so match on the suffix rather than guessing the
        family. Returns None when the provider is unreachable or reports nothing usable:
        an unknown window falls back to a conservative default, which is a smaller
        problem than recording a wrong one.
        """
        try:
            response = requests.post(
                f"{base_url}/api/show", json={"model": model_name}, timeout=timeout
            )
            response.raise_for_status()
            info = response.json().get("model_info") or {}
        except (requests.RequestException, ValueError):
            return None
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int) and value > 0:
                return value
        return None

    def _backfill_context_windows(self, provider: dict[str, Any], timeout: int) -> list[str]:
        """Fill in the window for models registered before it was recorded.

        Only rows that have no window are touched, so a value the user set by hand
        survives every rediscovery.
        """
        base_url = (provider.get("base_url") or "").rstrip("/")
        repaired: list[str] = []
        for model in self.list_models(provider["id"]):
            if model.get("context_window"):
                continue
            window = self._served_context_window(base_url, model["model_name"], timeout)
            if not window:
                continue
            self.update_model(model["id"], ModelUpdate(context_window=window))
            repaired.append(model["model_name"])
        return repaired

    def _backfill_vision_support(self, provider: dict[str, Any], timeout: int) -> list[str]:
        """Record which models can actually see.

        ``supports_vision`` has been a column, a Pydantic field and a checkbox since
        before anything read it, and it defaults to 0 -- so every model reports as
        blind. This is the same defect the tool-calling probe was added to fix.

        Only ever sets the flag true. A probe that says no leaves the column alone
        rather than overwriting a deliberate yes, so a value the user ticked by hand
        survives every rediscovery.
        """
        base_url = (provider.get("base_url") or "").rstrip("/")
        if not base_url or provider.get("provider_type") != "ollama":
            return []
        repaired: list[str] = []
        for model in self.list_models(provider["id"]):
            if model.get("supports_vision") or model.get("supports_embeddings"):
                continue
            if not ollama_supports_vision(base_url, model["model_name"], timeout):
                continue
            self.update_model(model["id"], ModelUpdate(supports_vision=True))
            repaired.append(model["model_name"])
        return repaired

    def _ensure_default_routes(self) -> list[str]:
        """Create any route that was added after this profile was first seeded.

        ``ensure_defaults`` writes routes only when the provider table is empty, so
        an existing profile never receives a newly introduced route and every caller
        of it fails with "route is missing or disabled" -- on a database that looks
        perfectly healthy. Seeding the gap is idempotent and cannot disturb a route
        the user has already pointed somewhere.
        """
        missing = [name for name in DEFAULT_ROUTES if not self.get_route(name)]
        if not missing:
            return []
        chat = self.get_route("chat")
        if not chat:
            # Nothing to model a new route on. ensure_defaults seeds the whole set
            # the first time a provider appears, so this resolves itself.
            return []
        now = store.now_iso()
        created: list[str] = []
        for route_name in missing:
            model_id = self._capable_model_id(route_name, chat["provider_id"], chat["model_id"])
            store.insert_route(
                {
                    "id": str(uuid.uuid4()),
                    "route_name": route_name,
                    "provider_id": chat["provider_id"],
                    "model_id": model_id,
                    "fallback_provider_id": None,
                    "fallback_model_id": None,
                    "temperature": 0.4 if route_name == "chat" else 0.2,
                    "max_output_tokens": chat.get("max_output_tokens"),
                    "enabled": True,
                    "metadata": {"source": "route_backfill"},
                    "created_at": now,
                    "updated_at": now,
                }
            )
            created.append(route_name)
        return created

    def _capable_model_id(self, route_name: str, provider_id: str, default_model_id: str) -> str:
        """Prefer a model that can do what the route is for, else follow chat."""

        capability = _ROUTE_CAPABILITY.get(route_name)
        if not capability:
            return default_model_id
        for model in self.list_models(provider_id):
            if model.get(capability) and model.get("enabled"):
                return model["id"]
        return default_model_id

    def discover_provider_models(self, provider_id: str, *, timeout: int = 5) -> dict[str, Any]:
        """Register any model the provider already serves but the registry does not know.

        Existing rows are never modified, so a rediscovery is safe to repeat and cannot
        clobber capability flags or display names the user has edited.
        """
        provider = self.get_provider(provider_id)
        if not provider:
            raise LookupError("LLM provider not found.")
        if provider.get("provider_type") != "ollama":
            raise ValueError("Model discovery is only supported for Ollama providers.")
        base_url = (provider.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("This provider has no endpoint configured.")

        try:
            response = requests.get(f"{base_url}/api/tags", timeout=timeout)
            response.raise_for_status()
            served = response.json().get("models", [])
        except requests.RequestException as exc:
            raise ConnectionError(f"Could not reach {base_url}.") from exc

        known = {row["model_name"] for row in self.list_models(provider_id)}
        settings = get_settings()
        added: list[dict[str, Any]] = []

        for item in served:
            name = (item.get("name") or "").strip()
            if not name or name in known:
                continue
            details = item.get("details") or {}
            is_embedding = any(hint in name.lower() for hint in self._EMBEDDING_HINTS)
            added.append(
                self.create_model(
                    ModelCreate(
                        id=_slug(f"{provider_id}-{name}", str(uuid.uuid4())),
                        provider_id=provider_id,
                        model_name=name,
                        display_name=name,
                        context_window=self._served_context_window(base_url, name, timeout),
                        max_output_tokens=None if is_embedding else settings.chat_num_predict,
                        supports_embeddings=is_embedding,
                        enabled=True,
                        metadata={
                            "source": "ollama_discovery",
                            "parameter_size": details.get("parameter_size"),
                            "quantization_level": details.get("quantization_level"),
                            "family": details.get("family"),
                        },
                    )
                )
            )
            known.add(name)

        context_repaired = self._backfill_context_windows(provider, timeout)
        vision_repaired = self._backfill_vision_support(provider, timeout)

        # Mirror every chat model, not just the new ones, so a registry model that is
        # missing from the picker is repaired rather than skipped.
        picker_added = self._mirror_to_picker(provider, self.list_models(provider_id))

        return {
            "added": added,
            "already_registered": sorted(known - {model["model_name"] for model in added}),
            "picker_added": picker_added,
            "context_repaired": context_repaired,
            "vision_repaired": vision_repaired,
        }

    def bind_picker_routes(self, model_name: str, base_url: str | None) -> bool:
        """Point the routes the composer's picker owns at this model.

        The composer dropdown writes the legacy active_id, but generation resolves its
        model through the registry route. Without this the dropdown can say one model
        while a different one answers. Chat and agent runs share the one picker, so a
        selection moves both routes -- binding only chat would leave an agent run
        answering on whatever model it was seeded with.
        """
        wanted = (base_url or "").rstrip("/")
        for provider in self.list_providers():
            if wanted and str(provider.get("base_url") or "").rstrip("/") != wanted:
                continue
            for model in self.list_models(provider["id"]):
                if model["model_name"] != model_name or not model.get("enabled"):
                    continue
                for route_name in PICKER_ROUTES:
                    self.update_route(
                        route_name,
                        RouteUpdate(
                            provider_id=provider["id"],
                            model_id=model["id"],
                            metadata={"source": "picker_selection"},
                        ),
                    )
                return True
        return False

    def _picker_id(self, provider_id: str, model_name: str) -> str:
        return _slug(f"{provider_id}-{model_name}", "llm")

    def _sync_model_to_picker(self, model: dict[str, Any]) -> None:
        """Add or refresh this model's entry in the chat picker.

        The picker is a separate legacy store, so without this a model added through
        Settings is invisible in chat -- it shows in the registry but cannot be selected.
        """
        provider = self.get_provider(model["provider_id"])
        if not provider or provider.get("provider_type") not in {"ollama", "openai_compatible"}:
            return
        if model.get("supports_embeddings"):
            return
        base_url = str(provider.get("base_url") or "").rstrip("/")
        if not base_url:
            return

        settings = get_settings()
        config_id = self._picker_id(provider["id"], model["model_name"])
        entry = LLMConfig(
            id=config_id,
            name=str(provider.get("name") or "LLM"),
            provider=str(provider["provider_type"]),
            model=str(model["model_name"]),
            base_url=base_url,
            api_key_env=provider.get("api_key_ref") or None,
            enabled=bool(model.get("enabled", True)) and bool(provider.get("enabled", True)),
            timeout_seconds=int(provider.get("timeout_seconds") or settings.chat_timeout_seconds),
            num_predict=int(model.get("max_output_tokens") or settings.chat_num_predict),
        )

        registry = LLMRegistry()
        configs, active_id = registry.load()
        configs = [item for item in configs if item.id != config_id and item.model != entry.model]
        configs.append(entry)
        if not any(item.enabled for item in configs):
            return
        registry.save(configs, active_id)

    def _remove_model_from_picker(self, model: dict[str, Any]) -> None:
        config_id = self._picker_id(model["provider_id"], model["model_name"])
        registry = LLMRegistry()
        configs, active_id = registry.load()
        remaining = [item for item in configs if item.id != config_id]
        if len(remaining) == len(configs) or not any(item.enabled for item in remaining):
            # Never leave the picker empty; the registry requires one enabled entry.
            return
        registry.save(remaining, active_id)

    def _mirror_to_picker(
        self, provider: dict[str, Any], models: list[dict[str, Any]]
    ) -> list[str]:
        """Mirror discovered chat models into the legacy picker list.

        The chat model picker reads the legacy JSON registry, not this one, so a model
        registered here is invisible in chat until it also has a picker entry.
        """
        chat_models = [model for model in models if not model["supports_embeddings"]]
        if not chat_models:
            return []

        registry = LLMRegistry()
        configs, active_id = registry.load()
        known = {config.model for config in configs}
        settings = get_settings()
        added: list[str] = []

        for model in chat_models:
            name = model["model_name"]
            if name in known:
                continue
            configs.append(
                LLMConfig(
                    id=_slug(f"{provider['id']}-{name}", str(uuid.uuid4())),
                    name=str(provider.get("name") or "Ollama"),
                    provider="ollama",
                    model=name,
                    base_url=str(provider.get("base_url") or settings.ollama_url),
                    timeout_seconds=int(
                        provider.get("timeout_seconds") or settings.chat_timeout_seconds
                    ),
                    num_predict=settings.chat_num_predict,
                )
            )
            known.add(name)
            added.append(name)

        if added:
            registry.save(configs, active_id)
        return added

    def update_model(self, model_id: str, request: ModelUpdate) -> dict[str, Any]:
        if not self.get_model(model_id):
            raise LookupError("LLM model not found.")
        updates = request.model_dump(exclude_unset=True)
        updates["updated_at"] = store.now_iso()
        updated = store.update_row("workspace_llm_models", "model", model_id, updates)
        self._sync_model_to_picker(updated)
        return updated

    def delete_model(self, model_id: str) -> None:
        model = self.get_model(model_id)
        if not model:
            raise LookupError("LLM model not found.")
        try:
            store.delete_row("workspace_llm_models", model_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Model is referenced by a route; disable it instead.") from exc
        self._remove_model_from_picker(model)

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
