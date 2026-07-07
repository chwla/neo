from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.llm import LLMConfig, LLMMessage, LLMRegistry, OpenAICompatibleClient


class LLMRegistryTests(unittest.TestCase):
    def test_registry_supports_multiple_local_and_api_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = LLMRegistry(Path(directory) / "llms.json")
            configs = [
                LLMConfig(
                    id="local",
                    name="Local",
                    provider="ollama",
                    model="qwen:7b",
                    base_url="http://localhost:11434",
                ),
                LLMConfig(
                    id="hosted",
                    name="Hosted",
                    provider="openai_compatible",
                    model="model-a",
                    base_url="https://example.test/v1",
                    api_key="secret",
                ),
            ]
            registry.save(configs, "hosted")
            loaded, active = registry.load()
            self.assertEqual(active, "hosted")
            self.assertEqual([item.id for item in loaded], ["local", "hosted"])
            self.assertEqual(loaded[1].api_key, "secret")
            self.assertNotIn("api_key", loaded[1].public_dict())
            self.assertTrue(loaded[1].public_dict()["has_api_key"])

    def test_disabled_active_config_falls_back_to_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llms.json"
            path.write_text(
                json.dumps(
                    {
                        "active_id": "off",
                        "llms": [
                            {
                                "id": "off",
                                "name": "Off",
                                "provider": "ollama",
                                "model": "a",
                                "base_url": "http://localhost:1",
                                "enabled": False,
                            },
                            {
                                "id": "on",
                                "name": "On",
                                "provider": "ollama",
                                "model": "b",
                                "base_url": "http://localhost:2",
                                "enabled": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            _, active = LLMRegistry(path).load()
            self.assertEqual(active, "on")


class OpenAICompatibleClientTests(unittest.TestCase):
    @patch("app.services.llm.requests.post")
    def test_chat_maps_openai_response_and_usage(self, post: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "<think>hidden</think>Hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        response.raise_for_status.return_value = None
        post.return_value = response
        client = OpenAICompatibleClient("model-a", "https://example.test/v1", 30, 100, "key")
        result = client.chat_with_metadata([LLMMessage(role="user", content="Hi")])
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.thinking, "hidden")
        self.assertEqual(result.total_tokens, 12)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer key")

    @patch("app.services.llm.requests.post")
    def test_stream_maps_sse_chunks(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.iter_lines.return_value = iter(
            [
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"total_tokens":4}}',
                "data: [DONE]",
            ]
        )
        post.return_value = response
        client = OpenAICompatibleClient("model-a", "https://example.test/v1", 30, 100)
        events = list(client.chat_stream([LLMMessage(role="user", content="Hi")]))
        self.assertEqual([event.get("content") for event in events[:-1]], ["Hel", "lo"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["total_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
