from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.chat import NeoChatService
from app.services.chat_intent import InternalChatIntent, resolve_internal_chat_intent
from app.services.provider_runtime.client import ProviderRuntimeClient
from app.services.recovery.service import RecoveryService

REPRODUCTION_PROMPT = (
    "Explain in detail how a local-first AI assistant should manage long-term memory, "
    "including memory creation, updating, conflict resolution, supersession, archiving, "
    "deletion, privacy, and recovery after restart. Include examples and potential failure "
    "cases."
)


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
    assert service.ollama.stream_calls == [["normal-llm-message"]]

    assistant = service.store.messages[-1]
    assert assistant.role == "assistant"
    assert assistant.content == "A detailed normal answer."
    assert assistant.metadata == {
        "prompt_tokens": 101,
        "completion_tokens": 17,
        "total_tokens": 118,
        "duration_ms": 42,
        "thinking": None,
    }


def test_runtime_stream_exposes_provider_usage_for_chat_persistence() -> None:
    class Runtime:
        def start_stream(self, **_kwargs):
            return {"id": "request-1", "status": "running"}

        @staticmethod
        def request(_request_id):
            return {
                "id": "request-1",
                "status": "completed",
                "metadata": {"partial_response": "Streamed answer."},
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
        {"type": "chunk", "content": "Streamed answer."},
        {
            "type": "done",
            "prompt_tokens": 55,
            "completion_tokens": 21,
            "total_tokens": 76,
            "duration_ms": 123,
        },
    ]
