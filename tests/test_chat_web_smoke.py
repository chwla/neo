import unittest
from types import SimpleNamespace

from app.services.chat import NeoChatService
from app.services.context import ContextPackage
from app.services.search.types import SearchResult, WebSearchResponse
from app.services.web_search import WebSearchService


class FakeSearchProvider:
    name = "smoke"

    def search(self, query, max_results, time_filter=None):
        return WebSearchResponse(
            query=query,
            provider=self.name,
            results=[SearchResult(
                title="Neo smoke result",
                url="https://example.com/neo-smoke",
                snippet="A deterministic Web Search smoke result.",
                source="example.com",
                rank=1,
            )],
        )


class FakeChatModel:
    def chat_with_metadata(self, messages, **_kwargs):
        return SimpleNamespace(content=f"Echo: {messages[-1].content}")


class ChatAndWebSearchSmokeTest(unittest.TestCase):
    def test_chat_prompt_reaches_model_with_system_context(self):
        service = object.__new__(NeoChatService)
        context = ContextPackage(
            profile=[], preferences=[], goals=[], projects=[], relevant_memories=[],
            events=[], archive_results=[],
        )
        messages = service.build_messages("Hello Neo", [], context)
        response = FakeChatModel().chat_with_metadata(messages)
        self.assertEqual(messages[-1].content, "Hello Neo")
        self.assertIn("local personal AI assistant", messages[0].content)
        self.assertEqual(response.content, "Echo: Hello Neo")

    def test_web_search_reaches_configured_provider(self):
        service = WebSearchService(provider=FakeSearchProvider())
        service.settings.web_search_enabled = True
        response = service.search("latest Neo smoke status", max_results=3)
        self.assertIsNone(response.error)
        self.assertEqual(response.provider, "smoke")
        self.assertEqual(response.results[0].title, "Neo smoke result")
        self.assertTrue(response.provider_query)


if __name__ == "__main__":
    unittest.main()
