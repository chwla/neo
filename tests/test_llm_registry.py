from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.llm import LLMMessage, get_llm_client
from app.services.llm_registry.usage import safe_error


class LLMRegistryFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root / 'neo.db'}"
        os.environ["NEO_LLM_CONFIG_PATH"] = str(self.root / "neo_llms.json")
        os.environ["NEO_LLM_PROVIDER"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "http://ollama.test:11434"
        os.environ["NEO_DEFAULT_MODEL"] = "qwen-test:latest"
        get_settings.cache_clear()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        get_settings.cache_clear()
        for key in (
            "NEO_DATABASE_URL",
            "NEO_LLM_CONFIG_PATH",
            "NEO_LLM_PROVIDER",
            "OLLAMA_BASE_URL",
            "NEO_DEFAULT_MODEL",
            "TEST_PROVIDER_KEY",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def create_provider(self, provider_id="provider-a", provider_type="openai_compatible"):
        response = self.client.post(
            "/api/llm/providers",
            json={
                "id": provider_id,
                "name": "Provider A",
                "provider_type": provider_type,
                "base_url": "https://provider.test/v1" if provider_type != "mock" else None,
                "api_key_ref": "TEST_PROVIDER_KEY" if provider_type != "mock" else None,
                "default_model": "model-a",
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["provider"]

    def create_model(self, provider_id, model_id="model-a"):
        response = self.client.post(
            "/api/llm/models",
            json={
                "id": model_id,
                "provider_id": provider_id,
                "model_name": model_id,
                "display_name": model_id.upper(),
                "supports_json": True,
                "max_output_tokens": 500,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["model"]

    def test_defaults_models_routes_and_docker_environment(self) -> None:
        providers = self.client.get("/api/llm/providers").json()["providers"]
        models = self.client.get("/api/llm/models").json()["models"]
        routes = self.client.get("/api/llm/routes").json()["routes"]
        self.assertEqual(providers[0]["provider_type"], "ollama")
        self.assertEqual(providers[0]["base_url"], "http://ollama.test:11434")
        self.assertIn("qwen-test:latest", {item["model_name"] for item in models})
        self.assertFalse(
            next(item for item in models if item["id"] == "ollama-default-model")[
                "supports_json"
            ]
        )
        self.assertEqual(
            {item["route_name"] for item in routes},
            {
                "chat",
                "research",
                "agent",
                "coding_agent",
                "patch_proposal",
                "summarization",
                "embedding",
                "title_generation",
            },
        )
        with patch.dict(
            os.environ,
            {
                "NEO_LLM_PROVIDER": "openai_compatible",
                "NEO_DEFAULT_MODEL": "docker-model",
                "NEO_OPENAI_COMPAT_BASE_URL": "http://host.docker.internal:9000/v1",
            },
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.llm_provider, "openai_compatible")
        self.assertEqual(settings.default_model, "docker-model")

    def test_provider_model_crud_route_update_and_no_secret_leak(self) -> None:
        os.environ["TEST_PROVIDER_KEY"] = "super-secret-value"
        provider = self.create_provider()
        model = self.create_model(provider["id"])
        public = self.client.get("/api/llm/providers").json()["providers"]
        serialized = json.dumps(public)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn('"api_key"', serialized)
        self.assertTrue(
            next(item for item in public if item["id"] == provider["id"])["api_key_configured"]
        )

        updated = self.client.patch(f"/api/llm/providers/{provider['id']}", json={"priority": 5})
        self.assertEqual(updated.json()["provider"]["priority"], 5)
        route = self.client.patch(
            "/api/llm/routes/chat",
            json={"provider_id": provider["id"], "model_id": model["id"]},
        )
        self.assertEqual(route.status_code, 200, route.text)
        self.assertEqual(route.json()["route"]["model_id"], model["id"])
        disabled_json = self.client.patch(
            f"/api/llm/models/{model['id']}", json={"supports_json": False}
        )
        self.assertFalse(disabled_json.json()["model"]["supports_json"])
        restarted = TestClient(create_app())
        persisted = restarted.get("/api/llm/providers").json()["providers"]
        self.assertIn(provider["id"], {item["id"] for item in persisted})
        persisted_route = next(
            item
            for item in restarted.get("/api/llm/routes").json()["routes"]
            if item["route_name"] == "chat"
        )
        self.assertEqual(persisted_route["model_id"], model["id"])
        self.assertEqual(
            self.client.patch(
                f"/api/llm/models/{model['id']}", json={"enabled": False}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete(f"/api/llm/providers/{provider['id']}").status_code,
            400,
        )
        redacted = safe_error(
            RuntimeError("request failed with super-secret-value"), provider
        )
        self.assertNotIn("super-secret-value", redacted)
        self.assertIn("[redacted]", redacted)
        ignored = self.client.post(
            "/api/llm/providers",
            json={
                "name": "Unsafe",
                "provider_type": "openai_compatible",
                "base_url": "https://unsafe.test/v1",
                "api_key": "must-not-be-accepted",
            },
        )
        self.assertEqual(ignored.status_code, 201)
        self.assertNotIn("must-not-be-accepted", ignored.text)

    @patch("app.services.llm.requests.post")
    def test_openai_compatible_success_records_usage(self, post: Mock) -> None:
        os.environ["TEST_PROVIDER_KEY"] = "secret-token"
        provider, model = self.create_provider(), None
        model = self.create_model(provider["id"])
        self.client.patch(
            "/api/llm/routes/chat",
            json={"provider_id": provider["id"], "model_id": model["id"]},
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "Hello"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
        post.return_value = response
        result = get_llm_client(route_name="chat").chat_with_metadata(
            [LLMMessage(role="user", content="Hi")]
        )
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.provider_id, provider["id"])
        self.assertFalse(result.fallback_used)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")
        usage = self.client.get("/api/llm/usage?route_name=chat&status=success").json()
        self.assertEqual(usage["total"], 1)
        self.assertEqual(usage["calls"][0]["total_tokens"], 6)
        self.assertNotIn("secret-token", json.dumps(usage))

    @patch("app.services.llm.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_retryable_failure_uses_configured_fallback_and_records_both(self, _post: Mock) -> None:
        os.environ["TEST_PROVIDER_KEY"] = "configured-for-test"
        primary = self.create_provider("primary", "openai_compatible")
        primary_model = self.create_model(primary["id"], "primary-model")
        fallback = self.create_provider("fallback", "mock")
        fallback_model = self.create_model(fallback["id"], "fallback-model")
        self.client.patch(
            "/api/llm/routes/patch_proposal",
            json={
                "provider_id": primary["id"],
                "model_id": primary_model["id"],
                "fallback_provider_id": fallback["id"],
                "fallback_model_id": fallback_model["id"],
            },
        )
        result = get_llm_client(route_name="patch_proposal").chat_with_metadata(
            [LLMMessage(role="user", content="proposal")]
        )
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.provider_id, fallback["id"])
        calls = self.client.get("/api/llm/usage?route_name=patch_proposal").json()["calls"]
        self.assertEqual({item["status"] for item in calls}, {"failed", "success"})
        self.assertTrue(
            next(item for item in calls if item["status"] == "success")["fallback_used"]
        )

    def test_disabled_missing_and_failed_providers_are_structured_and_logged(self) -> None:
        provider = self.create_provider("disabled-provider", "mock")
        model = self.create_model(provider["id"], "disabled-model")
        self.client.patch(f"/api/llm/providers/{provider['id']}", json={"enabled": False})
        self.client.patch(
            "/api/llm/routes/agent",
            json={"provider_id": provider["id"], "model_id": model["id"]},
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            get_llm_client(route_name="agent").chat_with_metadata(
                [LLMMessage(role="user", content="plan")]
            )
        calls = self.client.get("/api/llm/usage?route_name=agent&status=failed").json()
        self.assertEqual(calls["total"], 1)
        missing = self.client.patch(
            "/api/llm/routes/research",
            json={"provider_id": "missing", "model_id": "missing"},
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("mapping is invalid", missing.json()["detail"])

    def test_streaming_call_records_route_metadata_and_usage(self) -> None:
        provider = self.create_provider("stream-mock", "mock")
        model = self.create_model(provider["id"], "stream-model")
        self.client.patch(
            "/api/llm/routes/chat",
            json={"provider_id": provider["id"], "model_id": model["id"]},
        )
        events = list(
            get_llm_client(route_name="chat").chat_stream(
                [LLMMessage(role="user", content="stream")]
            )
        )
        self.assertEqual(events[-1]["route_name"], "chat")
        self.assertEqual(events[-1]["provider_id"], provider["id"])
        self.assertFalse(events[-1]["fallback_used"])
        usage = self.client.get(
            f"/api/llm/usage?route_name=chat&provider_id={provider['id']}&status=success"
        ).json()
        self.assertEqual(usage["total"], 1)

    def test_main_call_roles_resolve_through_registry_wrapper(self) -> None:
        for route_name in (
            "chat",
            "research",
            "agent",
            "coding_agent",
            "patch_proposal",
            "summarization",
            "embedding",
            "title_generation",
        ):
            client = get_llm_client(route_name=route_name)
            self.assertEqual(client.route_name, route_name)
            self.assertTrue(client.route["provider_id"])
            self.assertTrue(client.route["model_id"])

    @patch("app.services.llm.requests.get")
    def test_ollama_health_success_and_failure(self, get: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"models": [{"name": "qwen-test:latest"}]}
        get.return_value = response
        success = self.client.post("/api/llm/health", json={"route_name": "chat"})
        self.assertEqual(success.status_code, 200)
        self.assertTrue(success.json()["available"])
        get.side_effect = requests.ConnectionError("offline")
        failure = self.client.post("/api/llm/health", json={"route_name": "chat"})
        self.assertEqual(failure.status_code, 200)
        self.assertFalse(failure.json()["available"])
        self.assertEqual(failure.json()["error"], "Provider is unavailable.")

    def test_legacy_json_migrates_without_plaintext_key(self) -> None:
        legacy_path = Path(os.environ["NEO_LLM_CONFIG_PATH"])
        legacy_path.write_text(
            json.dumps(
                {
                    "active_id": "legacy-hosted",
                    "llms": [
                        {
                            "id": "legacy-hosted",
                            "name": "Legacy Hosted",
                            "provider": "openai_compatible",
                            "model": "legacy-model",
                            "base_url": "https://legacy.test/v1",
                            "api_key": "must-not-migrate",
                            "api_key_env": "TEST_PROVIDER_KEY",
                            "enabled": True,
                        }
                    ],
                }
            )
        )
        from app.services.llm_registry.service import LLMRegistryService

        LLMRegistryService().ensure_defaults()
        providers = self.client.get("/api/llm/providers").json()["providers"]
        legacy = next(item for item in providers if item["id"] == "legacy-hosted")
        self.assertEqual(legacy["api_key_ref"], "TEST_PROVIDER_KEY")
        self.assertNotIn("must-not-migrate", json.dumps(legacy))
        route = next(
            item
            for item in self.client.get("/api/llm/routes").json()["routes"]
            if item["route_name"] == "chat"
        )
        self.assertEqual(route["provider_id"], "legacy-hosted")
        override = self.create_provider("registry-override", "mock")
        override_model = self.create_model(override["id"], "registry-override-model")
        self.client.patch(
            "/api/llm/routes/chat",
            json={"provider_id": override["id"], "model_id": override_model["id"]},
        )
        LLMRegistryService().ensure_defaults()
        route = next(
            item
            for item in self.client.get("/api/llm/routes").json()["routes"]
            if item["route_name"] == "chat"
        )
        self.assertEqual(route["provider_id"], override["id"])


if __name__ == "__main__":
    unittest.main()
