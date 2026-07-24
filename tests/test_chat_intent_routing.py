from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.chat import NeoChatService
from app.services.chat_intent import InternalChatIntent, resolve_internal_chat_intent
from app.services.provider_runtime.client import ProviderRuntimeClient
from app.services.recovery.service import RecoveryService
from app.services.search.types import EvidenceChunk, WebContext
from app.services.source_citations import CitationFormatter, SourceCitation
from app.services.tasks.service import TaskContextService

REPRODUCTION_PROMPT = (
    "Explain in detail how a local-first AI assistant should manage long-term memory, "
    "including memory creation, updating, conflict resolution, supersession, archiving, "
    "deletion, privacy, and recovery after restart. Include examples and potential failure "
    "cases."
)

NORMAL_CHAT_INPUTS = [
    "hello",
    "hi",
    "hey",
    "helo",
    "good morning",
    "what can you do?",
    "testing",
    "recovery",
    "tell me about recovery",
    "projects are useful",
    "I want to research this later",
    "こんにちは 👋",
    "# Heading\n\n`markdown` **text**",
    "<article>Hello</article>",
]


@pytest.mark.parametrize(
    "prompt",
    [
        REPRODUCTION_PROMPT,
        "Explain recovery after an application restart.",
        "How should coding-agent recovery work?",
        "Describe how Neo finds recoverable runs.",
        "What is the purpose of the Recovery page?",
        "Compare backup, recovery, and continuity.",
        "Write documentation about recovering interrupted agent runs.",
        "Could you check recovery sometime?",
        "Explain how research, files, projects, notes, tasks, and tools work together.",
        "Describe how the coding agent handles failed tests and Git checkpoints.",
        "Write documentation about the test runner and its test history.",
    ],
)
def test_explanatory_or_ambiguous_prompts_never_select_internal_action(prompt: str) -> None:
    assert resolve_internal_chat_intent(prompt) is None


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Find my recoverable runs.", InternalChatIntent("recovery", "lookup")),
        ("Show interrupted agent runs.", InternalChatIntent("recovery", "lookup")),
        (
            "Open recovery and check for incomplete runs.",
            InternalChatIntent("recovery", "lookup"),
        ),
        (
            "Resume my last interrupted coding-agent run.",
            InternalChatIntent("recovery", "operation"),
        ),
        ("Show coding agent runs.", InternalChatIntent("coding", "lookup")),
        ("Show git status.", InternalChatIntent("git", "lookup")),
        ("List failed tests.", InternalChatIntent("tests", "lookup")),
        ("Show my open tasks.", InternalChatIntent("tasks", "lookup")),
        ("Create a task called Test Neo.", InternalChatIntent("tasks", "operation")),
        (
            "Run the saved test command.",
            InternalChatIntent("tests", "operation"),
        ),
    ],
)
def test_explicit_panel_commands_select_only_the_requested_feature(
    prompt: str, expected: InternalChatIntent
) -> None:
    assert resolve_internal_chat_intent(prompt) == expected


def test_recovery_result_cannot_replace_an_informational_answer() -> None:
    recovery = object.__new__(RecoveryService)
    recovery.list_runs = lambda **_kwargs: {"runs": [], "total": 0}

    assert recovery.answer_for_prompt(REPRODUCTION_PROMPT) is None
    assert recovery.answer_for_prompt("Describe how Neo finds recoverable runs.") is None
    assert recovery.answer_for_prompt("Find my recoverable runs.") == (
        "No recoverable agent or coding-agent runs were found."
    )


def test_explicit_task_creation_routes_to_safe_internal_guidance() -> None:
    tasks = TaskContextService()

    assert tasks.answer_for_prompt("Create a task called Test Neo.") == (
        "Open Tasks and choose New Task to review the title, priority, and any linked project "
        "before creating it. Chat does not create tasks implicitly."
    )


class _Message:
    def __init__(self, message_id: int, role: str, content: str, **metadata) -> None:
        self.id = message_id
        self.role = role
        self.content = content
        self.metadata = metadata


class _Store:
    def __init__(self) -> None:
        self.messages: list[_Message] = []

    def list_chat_messages(self, _chat_id: int) -> list[_Message]:
        return self.messages.copy()

    def add_chat_message(self, _chat_id: int, role: str, content: str, **metadata) -> _Message:
        message = _Message(len(self.messages) + 1, role, content, **metadata)
        self.messages.append(message)
        return message

    def rename_chat_from_prompt(self, _chat_id: int, _prompt: str) -> None:
        pass


class _Database:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def refresh(self, _record) -> None:
        pass


class _LLM:
    def __init__(self) -> None:
        self.stream_calls: list[list[object]] = []

    def chat_stream(self, messages, **_kwargs):
        self.stream_calls.append(messages)
        yield {"type": "chunk", "content": "A detailed "}
        yield {"type": "chunk", "content": "normal answer."}
        yield {
            "type": "done",
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_tokens": 118,
            "duration_ms": 42,
        }

    @staticmethod
    def clean_response(content: str) -> str:
        return content

    @staticmethod
    def extract_thinking(_content: str) -> None:
        return None


class _Trap:
    def context_for_prompt(self, _prompt: str) -> str:
        return ""

    def answer_for_prompt(self, _prompt: str) -> str:
        raise AssertionError("An internal feature must not run for an informational prompt.")


def _normal_streaming_service() -> NeoChatService:
    service = object.__new__(NeoChatService)
    service.db = _Database()
    service.store = _Store()
    service.ollama = _LLM()
    service.settings = SimpleNamespace(chat_history_turns=8)
    service.rule_result = {"warnings": []}
    service.last_web_debug = {}
    service._active_rules_reply = lambda _prompt: None
    service.extract_user_prompt = lambda _prompt, _chat_id: []
    service.build_context = lambda _prompt: object()
    service.project_context = SimpleNamespace(context_for_prompt=lambda _prompt: "")
    service.task_context = _Trap()
    service.file_context = SimpleNamespace(context_for_prompt=lambda _prompt: "")
    service.code_index = SimpleNamespace(context_for_prompt=lambda _prompt: "")
    service.symbol_awareness = SimpleNamespace(context_for_prompt=lambda _prompt: "")
    service.test_runner = _Trap()
    service.git_context = _Trap()
    service.coding_agent = _Trap()
    service.recovery = _Trap()
    service.web_search = SimpleNamespace(
        build_context=lambda _query: SimpleNamespace(
            needed=False, citations=[], evidence_chunks=[], context_text="", warning=None
        )
    )
    service._web_query_with_memory_region = lambda query, _context: query
    service._direct_reply = lambda _prompt: None
    service._web_failure_reply = lambda _context: None
    service._direct_web_reply = lambda _prompt, _context: None
    service._web_debug = lambda *_args, **_kwargs: {"web_search_needed": False}
    service.build_messages = lambda *_args: ["normal-llm-message"]
    service._num_predict = lambda *_args: 128
    return service


def test_original_prompt_streams_through_llm_and_persists_generation_metadata() -> None:
    service = _normal_streaming_service()

    events = list(service.stream_message(chat_id=1, prompt=REPRODUCTION_PROMPT))

    assert [event["content"] for event in events if event["type"] == "chunk"] == [
        "A detailed ",
        "normal answer.",
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "A detailed normal answer."
    assert events[-1]["prompt_tokens"] == 101
    assert events[-1]["completion_tokens"] == 17
    assert events[-1]["total_tokens"] == 118
    assert events[-1]["duration_ms"] == 42
    assert events[-1]["web_debug"]["routing"] == {
        "chat_id": 1,
        "message_id": 2,
        "normalized_input_length": len(REPRODUCTION_PROMPT),
        "input_sha256": events[-1]["web_debug"]["routing"]["input_sha256"],
        "selected_route": "llm",
        "component": "default_chat_route",
        "matched_intent": None,
        "confidence": 0.0,
        "fuzzy_candidate": None,
        "direct_feature_service": None,
        "provider_invoked": True,
        "provider": None,
        "model": None,
        "fallback_reason": None,
        "response_source": "provider",
        "final_status": "completed",
    }
    assert service.ollama.stream_calls == [["normal-llm-message"]]

    assistant = service.store.messages[-1]
    assert assistant.role == "assistant"
    assert assistant.content == "A detailed normal answer."
    assert assistant.metadata["prompt_tokens"] == 101
    assert assistant.metadata["completion_tokens"] == 17
    assert assistant.metadata["total_tokens"] == 118
    assert assistant.metadata["duration_ms"] == 42
    assert assistant.metadata["thinking"] is None
    assert assistant.metadata["response_kind"] == "normal_chat"
    assert assistant.metadata["route_name"] == "chat"
    assert assistant.metadata["metadata"]["search_intent"]["kind"] == "none"


def test_release_chat_rejects_publication_date_and_untrusted_single_source() -> None:
    service = _normal_streaming_service()
    service.citation_formatter = CitationFormatter()
    context = WebContext(
        query="God of War next game release date official",
        needed=True,
        answer_mode="fact_lookup",
        evidence_chunks=[
            EvidenceChunk(
                source_index=1,
                source_title="State of Play June 2026 announcements",
                source_url=(
                    "https://blog.playstation.com/2026/06/02/"
                    "state-of-play-june-2026-all-announcements-trailers/"
                ),
                source="blog.playstation.com",
                text=(
                    "Published June 2, 2026. God of War: Sons of Sparta and "
                    "God of War Laufey are coming to PS5."
                ),
                relevance_score=12,
            ),
            EvidenceChunk(
                source_index=2,
                source_title="God of War Laufey release date",
                source_url="https://nerdyinfo.com/god-of-war-laufey",
                source="nerdyinfo.com",
                text="God of War Laufey releases on June 2, 2026.",
                relevance_score=10,
            ),
        ],
        citations=[
            SourceCitation(
                index=1,
                title="State of Play June 2026 announcements",
                url=(
                    "https://blog.playstation.com/2026/06/02/"
                    "state-of-play-june-2026-all-announcements-trailers/"
                ),
                source="blog.playstation.com",
                fetched=True,
            ),
            SourceCitation(
                index=2,
                title="God of War Laufey release date",
                url="https://nerdyinfo.com/god-of-war-laufey",
                source="nerdyinfo.com",
                fetched=True,
            ),
        ],
    )
    service.web_search = SimpleNamespace(build_context_forced=lambda _query: context)
    service._direct_web_reply = NeoChatService._direct_web_reply.__get__(service)
    service._verified_release_answer = NeoChatService._verified_release_answer.__get__(service)
    service._is_release_date_query = NeoChatService._is_release_date_query.__get__(service)
    service._target_region = NeoChatService._target_region.__get__(service)

    events = list(
        service.stream_message(
            chat_id=1,
            prompt="When is the next God of War game going to release?",
        )
    )

    reply = events[-1]["reply"]
    assert "cannot report a verified date yet" in reply
    assert "June 2, 2026" not in reply
    assert "blog.playstation.com" in reply
    assert service.ollama.stream_calls == []
    assert [message.role for message in service.store.messages] == ["user", "assistant"]
    assert isinstance(events[-1]["duration_ms"], int)
    assert events[-1]["duration_ms"] >= 0
    assert service.store.messages[-1].metadata["duration_ms"] == events[-1]["duration_ms"]


def test_native_provider_thinking_is_persisted_and_returned_to_the_client() -> None:
    class ThinkingLLM(_LLM):
        def chat_stream(self, messages, **_kwargs):
            self.stream_calls.append(messages)
            yield {"type": "thinking", "content": "I should answer this carefully."}
            yield {"type": "chunk", "content": "Answer."}
            yield {
                "type": "done",
                "thinking": "I should answer this carefully.",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "duration_ms": 7,
            }

    service = _normal_streaming_service()
    service.ollama = ThinkingLLM()

    events = list(service.stream_message(chat_id=1, prompt="Explain this."))

    assert [event["type"] for event in events] == ["thinking", "chunk", "done"]
    assert events[-1]["thinking"] == "I should answer this carefully."
    assert service.store.messages[-1].metadata["thinking"] == "I should answer this carefully."


@pytest.mark.parametrize("prompt", NORMAL_CHAT_INPUTS)
def test_normal_text_uses_the_real_streaming_chat_service_and_never_returns_suggestions(
    prompt: str,
) -> None:
    """Exercise the same ``stream_message`` entry point used by the chat API."""

    service = _normal_streaming_service()

    events = list(service.stream_message(chat_id=1, prompt=prompt))

    assert service.ollama.stream_calls == [["normal-llm-message"]]
    assert len([message for message in service.store.messages if message.role == "user"]) == 1
    assistants = [message for message in service.store.messages if message.role == "assistant"]
    assert len(assistants) == 1
    assert "Did you mean" not in assistants[0].content
    assert events[-1]["type"] == "done"
    assert events[-1]["total_tokens"] == 118
    assert events[-1]["web_debug"]["routing"]["selected_route"] == "llm"
    assert events[-1]["web_debug"]["routing"]["provider_invoked"] is True


def test_same_normal_input_in_separate_chats_invokes_provider_independently() -> None:
    first, second = _normal_streaming_service(), _normal_streaming_service()

    list(first.stream_message(chat_id=1, prompt="hello"))
    list(second.stream_message(chat_id=2, prompt="hello"))

    assert len(first.ollama.stream_calls) == 1
    assert len(second.ollama.stream_calls) == 1


def test_runtime_stream_exposes_provider_usage_for_chat_persistence() -> None:
    class Runtime:
        def start_stream(self, **_kwargs):
            return {"id": "request-1", "status": "running"}

        @staticmethod
        def request(_request_id):
            return {
                "id": "request-1",
                "status": "completed",
                "route_name": "chat",
                "provider_name": "ollama",
                "model_name": "test-model",
                "metadata": {
                    "partial_response": "Streamed answer.",
                    "thinking": "Runtime reasoning.",
                },
                "provider_usage": {
                    "prompt_tokens": 55,
                    "completion_tokens": 21,
                    "total_tokens": 76,
                },
                "latency_ms": 123,
            }

    client = object.__new__(ProviderRuntimeClient)
    client.route_name = "chat"
    client.num_predict = None
    client.timeout = None
    client.runtime = Runtime()

    events = list(client.chat_stream([], num_predict=128))

    assert events == [
        {"type": "start", "request_id": "request-1"},
        {"type": "thinking", "content": "Runtime reasoning."},
        {"type": "chunk", "content": "Streamed answer."},
        {
            "type": "done",
            "prompt_tokens": 55,
            "completion_tokens": 21,
            "total_tokens": 76,
            "duration_ms": 123,
            "provider_request_id": "request-1",
            "route_name": "chat",
            "provider": "ollama",
            "model": "test-model",
            "fallback_used": False,
            "finish_reason": None,
            "thinking": "Runtime reasoning.",
        },
    ]
