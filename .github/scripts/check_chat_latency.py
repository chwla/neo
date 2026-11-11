"""Regression checks for Quick routing cost and conditional prompt context."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.chat import (
    _PROMPT_AUTHORITY,
    _PROMPT_CORE,
    _PROMPT_LOW,
    _PROMPT_WORKSPACE,
    NeoChatService,
)
from app.services.llm import ChatTurn
from app.services.memory.prompt import STABLE_MEMORY_POLICY
from app.services.provider_runtime import streaming
from app.services.provider_runtime.client import ProviderRuntimeClient
from app.services.provider_runtime.service import ProviderRuntimeService
from app.services.search.intent import SearchIntentResolver


def service(*, high=False, web=False):
    chat = object.__new__(NeoChatService)
    chat.low_effort, chat.reasoning = not high, high
    chat.settings = SimpleNamespace(web_search_enabled=web, chat_history_turns=20)
    chat.memory_enabled = chat.memory_prompt_enabled = True
    chat._turn_timezone = "Asia/Calcutta"
    chat._timings = {}
    chat.turn_images = []
    chat.turn_image_items = []
    chat.rule_result = {"resolved_rules": {}, "applied_profiles": [], "warnings": []}
    chat.search_intent_resolver = SearchIntentResolver()
    chat._build_memory_selection = Mock(return_value=None)
    chat._store_search_intent = Mock()
    chat.helper_llm = Mock()
    chat.helper_llm.chat_with_metadata.side_effect = AssertionError("Unexpected routing inference")
    return chat


class QuickLatencyChecks(unittest.TestCase):
    def test_plain_quick_turns_never_call_helper(self):
        chat = service()
        for text in ("what can you di", "explain recursion", "write a Python sorting function"):
            intent = chat._resolve_search_intent(text, [], timezone=None, locale=None)
            result = chat._route_turn(text, [], intent, timezone=None, locale=None, message_id=1)
            self.assertEqual(result.route, "direct")
            self.assertEqual(result.source_label_json, intent.source_label_json)
        chat.helper_llm.chat_with_metadata.assert_not_called()

    def test_detailed_turn_skips_router_without_disabling_reasoning(self):
        chat = service(high=True)
        intent = chat._resolve_search_intent("calculate 17 * 23", [], timezone=None, locale=None)
        result = chat._route_turn(
            "calculate 17 * 23", [], intent, timezone=None, locale=None, message_id=1
        )
        self.assertEqual(result.route, "direct")
        self.assertTrue(chat.reasoning)
        chat.helper_llm.chat_with_metadata.assert_not_called()

    def test_greeting_after_assistant_question_does_not_route(self):
        chat = service()
        prior = [SimpleNamespace(role="assistant", content="How can I help you today?")]
        self.assertEqual(
            chat._resolve_search_intent(
                "hello",
                prior,
                timezone=None,
                locale=None,
            ).route,
            "greeting",
        )

    def test_capability_requests_keep_semantic_router(self):
        resolver = SearchIntentResolver()
        for query in (
            "remind me to call Mum",
            "dentist tomorrow at 3",
            "am I free Friday?",
            "weather in Delhi",
            "search the web for Python",
            "current USD prices",
            "move it to next week",
            "connect to my email",
        ):
            self.assertIsNone(resolver.resolve_without_model(query, web_enabled=False), query)
        self.assertIsNone(
            resolver.resolve_without_model("What is the new Fable app?", web_enabled=True)
        )
        self.assertIsNone(
            resolver.resolve_without_model("write about the latest releases", web_enabled=True)
        )

    def test_explicit_routes_still_resolve(self):
        chat = service(web=True)
        for query, route in (
            ("search the web for Python", "web"),
            ("what time is it", "time"),
            ("convert 10 USD to INR", "currency"),
        ):
            self.assertEqual(
                chat._resolve_search_intent(
                    query,
                    [],
                    timezone="Asia/Calcutta",
                    locale=None,
                ).route,
                route,
            )

    def test_original_prompt_retains_history_and_authority(self):
        chat = service()
        context = chat.build_context("hello")
        history = [ChatTurn(role="user", content="My name is Sam.")]
        messages = chat.build_messages(
            "hello",
            history,
            context,
            project_context="No project context loaded.",
            task_context="No task context loaded.\n\nNo file context loaded.\n\n"
            "No codebase index context requested.\n\nNo symbol navigation context requested.",
        )
        prompt = messages[0].content
        self.assertIn(_PROMPT_WORKSPACE, prompt)
        self.assertIn(_PROMPT_AUTHORITY, prompt)
        self.assertIn(_PROMPT_CORE, prompt)
        self.assertIn(_PROMPT_LOW, prompt)
        self.assertIn("No calendar event was created", prompt)
        self.assertIn("Web search is turned off", prompt)
        self.assertEqual(messages[1].content, "My name is Sam.")
        self.assertEqual(messages[-1].content, "hello")

    def test_real_memory_workspace_rules_and_images_survive(self):
        chat = service()
        chat._build_memory_selection.return_value = SimpleNamespace(
            serialized=SimpleNamespace(content="Untrusted memory: Sam prefers tea.")
        )
        chat.turn_images = ["image-base64"]
        messages = chat.build_messages(
            "help",
            [],
            chat.build_context("help"),
            project_context="Project: a chess engine",
            task_context="Task: implement castling",
        )
        self.assertIn(STABLE_MEMORY_POLICY, messages[0].content)
        self.assertIn(_PROMPT_WORKSPACE, messages[0].content)
        self.assertIn("Project: a chess engine", messages[0].content)
        self.assertIn("Task: implement castling", messages[0].content)
        self.assertEqual(messages[1].content, "Untrusted memory: Sam prefers tea.")
        self.assertEqual(messages[-1].images, ["image-base64"])

    def test_high_effort_keeps_full_prompt(self):
        chat = service(high=True)
        prompt = chat.build_messages("explain", [], chat.build_context("explain"))[0].content
        self.assertIn(_PROMPT_AUTHORITY, prompt)
        self.assertIn("Work through difficult", prompt)

    def test_stream_sleeps_until_persisted_updates_and_keeps_final_chunk(self):
        identifier = "latency-regression"
        self.addCleanup(streaming.clear, identifier)
        state = {"id": identifier, "status": "running", "metadata": {}}
        reads = []

        def produce(cancelled):
            for metadata, status in (
                ({"thinking": "Considering"}, "streaming"),
                ({"thinking": "Considering", "partial_response": "Hi"}, "streaming"),
                ({"thinking": "Considering", "partial_response": "Hi!"}, "completed"),
            ):
                if cancelled.wait(0.08):
                    return
                state.update(status=status, metadata=metadata)
                streaming.notify(identifier)
            streaming.clear(identifier)

        def start_stream(**kwargs):
            initial = dict(state)
            streaming.start(identifier, produce)
            return initial

        def request(request_id):
            reads.append(request_id)
            return dict(state)

        client = ProviderRuntimeClient()
        client.runtime = Mock(start_stream=start_stream, request=request)
        events = list(client.chat_stream([]))
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "chunk"), "Hi!")
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(any(e["type"] == "thinking" for e in events))
        self.assertLessEqual(len(reads), 5, "The waiting consumer must not busy-poll SQLite")
        client.runtime.cancel_stream.assert_not_called()

    def test_stopping_stream_still_cancels_provider(self):
        client = ProviderRuntimeClient()
        client.runtime = Mock()
        client.runtime.start_stream.return_value = {"id": "stop", "status": "running"}
        events = client.chat_stream([])
        self.assertEqual(next(events)["type"], "start")
        events.close()
        client.runtime.cancel_stream.assert_called_once_with("stop")

    def test_partial_wakes_reader_only_after_persistence(self):
        runtime = ProviderRuntimeService(initialize=False)
        actions = []
        with (
            patch("app.services.provider_runtime.service.store") as store,
            patch(
                "app.services.provider_runtime.service.notify",
                side_effect=lambda _: actions.append("wake"),
            ),
        ):
            store.get_request.return_value = {"metadata": {}}
            store.update_request.side_effect = lambda *a, **k: actions.append("persist")
            runtime._partial("request", "hello")
        self.assertEqual(actions, ["persist", "wake"])


if __name__ == "__main__":
    unittest.main()
