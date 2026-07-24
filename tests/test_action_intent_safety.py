from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agents.guidance import agent_run_guidance
from app.services.chat import NeoChatService
from app.services.search.intent import resolve_search_intent
from app.services.search.types import SearchIntentKind


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain how to use a coding agent safely.",
        "Describe how to apply a patch.",
        "Write documentation about using an agent runner.",
        "How should a coding agent validate a patch?",
    ],
)
def test_agent_and_patch_topics_do_not_trigger_navigation_actions(prompt: str) -> None:
    assert agent_run_guidance(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "Start the agent runner for this task.",
        "Apply this patch proposal.",
    ],
)
def test_explicit_agent_actions_still_offer_safe_navigation(prompt: str) -> None:
    assert agent_run_guidance(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain how to use the MCP connector.",
        "What is an API connector?",
        "Compare MCP and REST tools.",
    ],
)
def test_connector_topics_remain_normal_chat(prompt: str) -> None:
    assert resolve_search_intent(prompt).kind == SearchIntentKind.NONE


def test_explicit_connector_command_resolves_to_connector_tool() -> None:
    assert (
        resolve_search_intent("Use the weather connector for New Delhi.").kind
        == SearchIntentKind.CONNECTOR_TOOL
    )


def test_unique_high_confidence_read_connector_can_run_from_normal_chat(
    monkeypatch,
) -> None:
    tool = SimpleNamespace(
        id="customer_lookup",
        name="customer_lookup",
        display_name="Customer lookup",
        input_schema={
            "type": "object",
            "required": ["customer_id"],
            "properties": {"customer_id": {"type": "integer"}},
        },
    )

    class FakeTools:
        def select_enabled_read_tool(self, capability, *, intent=None):
            assert "customer" in capability.lower()
            assert intent == capability
            return tool

        def invoke_connector(self, **kwargs):
            assert kwargs["tool_id"] == "customer_lookup"
            assert kwargs["arguments"] == {"customer_id": 123}
            return {
                "status": "completed",
                "call_id": "call-1",
                "result": {"status": "active"},
                "provenance": {"connector_name": "CRM"},
            }

    monkeypatch.setattr("app.services.chat.ToolsService", FakeTools)
    service = object.__new__(NeoChatService)
    intent = resolve_search_intent("Get customer with customer_id: 123")

    answer = service._connector_answer("Get customer with customer_id: 123", intent)

    assert answer is not None
    reply, metadata = answer
    assert '"status": "active"' in reply
    assert metadata["response_kind"] == "connector"


def test_informational_connector_prompt_does_not_even_attempt_selection(
    monkeypatch,
) -> None:
    class UnexpectedTools:
        def __init__(self):
            raise AssertionError("informational prompt attempted a connector lookup")

    monkeypatch.setattr("app.services.chat.ToolsService", UnexpectedTools)
    service = object.__new__(NeoChatService)
    prompt = "Explain how to use the MCP connector."

    assert service._connector_answer(prompt, resolve_search_intent(prompt)) is None
