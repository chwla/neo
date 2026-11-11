"""Local preparation must preserve prompts, selected models and foreground work."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from check_chat_latency import service

from app.services.chat import (
    _PROMPT_AUTHORITY,
    _PROMPT_CORE,
    _PROMPT_HIGH,
    _PROMPT_LOW,
    _PROMPT_WORKSPACE,
    chat_prompt_prefix,
)
from app.services.current_time import today_for_prompt
from app.services.provider_runtime.service import ProviderRuntimeService


class StartupChecks(unittest.TestCase):
    def setUp(self):
        self.route = {
            "provider_record": {"provider_type": "ollama", "base_url": "http://localhost:11434"},
            "model_record": {"model_name": "arbitrary-model", "context_window": 32768},
            "model": "arbitrary-model",
        }
        self.settings = SimpleNamespace(
            chat_model_warmup=True,
            chat_num_predict=2048,
            chat_thinking_headroom_tokens=1024,
            ollama_keep_alive="30m",
        )
        self.client = Mock()
        patches = [
            patch("app.services.provider_runtime.service.select", return_value=self.route),
            patch("app.services.provider_runtime.service.get_settings", return_value=self.settings),
            patch(
                "app.services.provider_runtime.service.ollama_loaded_model_names",
                return_value=["another-idle-model"],
            ),
            patch(
                "app.services.provider_runtime.service._reuse_loaded_context",
                side_effect=lambda provider, model, needed: needed,
            ),
            patch("app.services.provider_runtime.service.build_client", return_value=self.client),
            patch("app.services.provider_runtime.streaming.all_live", return_value=set()),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def test_prefix_matches_original_in_both_modes(self):
        for high in (False, True):
            for workspace in (False, True):
                expected = (
                    _PROMPT_CORE
                    + f"Today's date is {today_for_prompt('Asia/Calcutta')}. Treat it as the "
                    "present, even when it is later than your training data: this year and earlier "
                    "years are not in the future. "
                    + (_PROMPT_WORKSPACE if workspace else "")
                    + (_PROMPT_HIGH if high else _PROMPT_LOW)
                    + _PROMPT_AUTHORITY
                )
                prefix = chat_prompt_prefix(
                    reasoning=high, carries_workspace=workspace, timezone="Asia/Calcutta"
                )
                self.assertEqual(prefix, expected)
                chat = service(high=high)
                messages = chat.build_messages(
                    "private user question",
                    [],
                    chat.build_context("private user question"),
                    project_context="No project context loaded." if workspace else None,
                )
                self.assertTrue(messages[0].content.startswith(prefix))
                self.assertNotIn("private user question", prefix)

    def test_prepares_selected_model_even_when_other_idle_model_is_loaded(self):
        result = ProviderRuntimeService(initialize=False).warm(prefix="public prefix")
        self.assertEqual(result["status"], "warmed")
        self.assertTrue(result["prefix_prepared"])
        self.assertEqual(result["num_ctx"], 4096)
        build = self.mocks[4]
        self.assertEqual(build.call_args.kwargs["num_predict"], 1)
        self.assertTrue(build.call_args.kwargs["disable_thinking"])
        args, kwargs = self.client.chat_with_metadata.call_args
        self.assertEqual([(m.role, m.content) for m in args[0]], [("system", "public prefix")])
        self.assertEqual(kwargs["num_predict"], 1)

    def test_detailed_context_uses_answer_budget_not_one_token_budget(self):
        result = ProviderRuntimeService(initialize=False).warm(prefix="prefix", reasoning=True)
        self.assertEqual(result["num_ctx"], 8192)

    def test_loaded_model_still_needs_prompt_preparation(self):
        self.mocks[2].return_value = ["arbitrary-model"]
        self.assertEqual(
            ProviderRuntimeService(initialize=False).warm(prefix="prefix")["status"], "warmed"
        )

    def test_cloud_and_disabled_warmup_never_generate(self):
        self.route["provider_record"]["provider_type"] = "openai_compatible"
        self.assertEqual(
            ProviderRuntimeService(initialize=False).warm(prefix="prefix")["status"], "not_local"
        )
        self.settings.chat_model_warmup = False
        self.assertEqual(
            ProviderRuntimeService(initialize=False).warm(prefix="prefix")["status"], "disabled"
        )
        self.client.chat_with_metadata.assert_not_called()

    def test_active_answer_takes_priority(self):
        self.mocks[5].return_value = {"active-request"}
        self.assertEqual(
            ProviderRuntimeService(initialize=False).warm(prefix="prefix")["status"], "busy"
        )
        self.client.chat_with_metadata.assert_not_called()

    def test_warmup_failure_is_best_effort(self):
        self.client.chat_with_metadata.side_effect = TimeoutError()
        self.assertEqual(
            ProviderRuntimeService(initialize=False).warm(prefix="prefix")["status"], "failed"
        )

    def test_endpoint_cooldown_tracks_model_and_effort(self):
        from app.api.routes import chat as api

        self.route.update(provider="ollama", provider_id="local", model_id="one")
        self.settings.database_url = "sqlite:///test-startup-only.db"
        api._LAST_WARMUP.clear()
        api._WARMUP_PENDING.clear()
        self.addCleanup(api._LAST_WARMUP.clear)
        self.addCleanup(api._WARMUP_PENDING.clear)
        with (
            patch.object(api, "get_settings", return_value=self.settings),
            patch.object(api, "all_live_generation_threads", return_value=set()),
            patch("app.services.provider_runtime.router.select", return_value=self.route),
            patch.object(
                api.threading,
                "Thread",
                side_effect=lambda target, **kw: SimpleNamespace(start=target),
            ),
        ):
            self.assertEqual(api.warm_chat_model()["status"], "started")
            self.assertEqual(api.warm_chat_model()["status"], "recent")
            self.route["model_id"] = "two"
            self.assertEqual(api.warm_chat_model()["status"], "started")
            self.assertEqual(
                api.warm_chat_model(api.ChatWarmRequest(effort="high"))["status"], "started"
            )
        self.assertEqual(self.client.chat_with_metadata.call_count, 3)


if __name__ == "__main__":
    unittest.main()
