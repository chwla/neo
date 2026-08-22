"""Two transports for one contract: native tool calling, and a parsed-text fallback.

Neo runs local models by default and many of them cannot do native tool calling,
so refusing to work without it would take Agent Mode away from a large share of
users. The fallback documents the tools in the system prompt and asks for a
single fenced JSON block instead. Both paths return the same ``ToolCall`` list,
which is what keeps the loop free of transport conditionals.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services.agent_core.types import ToolCall
from app.services.llm import LLMMessage

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

# Local models intermittently emit a tool call as *prose* in their own training
# format instead of through the provider's structured channel -- observed roughly
# one turn in eight with qwen3-coder on Ollama. The turn is not malformed from
# the model's point of view; it simply never reaches the tool-call field, so the
# loop sees "no tools requested" and treats real work as a finished answer.
# Recognising the two common formats turns that dropped turn into a real call.
_XML_FUNCTION = re.compile(
    r"<function=([A-Za-z0-9_.\-]+)\s*>(.*?)(?:</function>|\Z)", re.DOTALL
)
_XML_PARAMETER = re.compile(
    r"<parameter=([A-Za-z0-9_.\-]+)\s*>(.*?)(?:</parameter>|\Z)", re.DOTALL
)
_HERMES_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.DOTALL)
_SALVAGE_LEFTOVERS = re.compile(
    r"<function=.*?(?:</function>|\Z)|<tool_call>.*?(?:</tool_call>|\Z)|</tool_call>",
    re.DOTALL,
)

_REPAIR_PROMPT = (
    "That response was not valid JSON. Reply with nothing but a single fenced "
    "```json block containing an object with a `tool_calls` array, or with plain "
    "prose if no tool is needed."
)


class ToolProtocolError(ValueError):
    """The model's tool request could not be understood or was not permitted."""


@dataclass
class ModelTurn:
    """One reply from the model.

    ``finish_reason`` matters as much as the content: a turn that stopped because
    it hit the token ceiling has *not* finished its thought, and treating that as
    "the agent is done" ends runs in the middle of the work.
    """

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def __iter__(self):
        # Kept so existing `content, calls = ...` call sites still read naturally.
        return iter((self.content, self.tool_calls))


def render_tool_docs(schemas: list[dict[str, Any]]) -> str:
    """Describe the tools in prose for models that cannot be sent a schema array."""

    lines = [
        "You can call tools. To call one, reply with nothing but a fenced JSON block:",
        "```json",
        '{"tool_calls": [{"name": "<tool>", "arguments": {...}}]}',
        "```",
        "Reply with plain prose instead when you are finished and need no tool.",
        "",
        "Available tools:",
    ]
    for schema in schemas:
        function = schema.get("function", schema)
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        required = set(parameters.get("required") or [])
        arguments = ", ".join(
            f"{name}: {spec.get('type', 'any')}{'' if name in required else ' (optional)'}"
            for name, spec in properties.items()
        )
        lines.append(f"- {function.get('name')}({arguments}) — {function.get('description', '')}")
    return "\n".join(lines)


def _extract_json_object(text: str) -> str | None:
    """Pull the JSON payload out of a response that may also contain prose."""

    match = _FENCE.search(text)
    if match:
        return match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


def _looks_like_a_tool_attempt(text: str) -> bool:
    """Distinguish "the model tried to call a tool and got it wrong" from prose.

    Only a failed *attempt* earns a repair retry; ordinary prose is a final
    answer and must not be second-guessed.
    """

    return "tool_calls" in text or bool(_FENCE.search(text))


def parse_text_tool_calls(text: str, allowed: set[str]) -> list[ToolCall]:
    """Parse the fallback protocol. Raises ``ToolProtocolError`` on malformed input."""

    payload = _extract_json_object(text)
    if payload is None:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ToolProtocolError(f"Tool call block was not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ToolProtocolError("Tool call block must be a JSON object.")
    raw_calls = decoded.get("tool_calls")
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, list):
        raise ToolProtocolError("`tool_calls` must be an array.")

    calls: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            raise ToolProtocolError("Each tool call must be an object.")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ToolProtocolError("Each tool call needs a `name`.")
        if name not in allowed:
            raise ToolProtocolError(f"Tool '{name}' is not available.")
        arguments = raw.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolProtocolError(f"Arguments for '{name}' must be an object.")
        calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments))
    return calls


def _coerce_argument(raw: str) -> Any:
    """Read a parameter body as JSON when it is JSON, otherwise as literal text."""

    value = raw.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def salvage_tool_calls(text: str, allowed: set[str]) -> list[ToolCall]:
    """Recover tool calls a model wrote into its prose instead of the tool field.

    Unknown or malformed names are dropped rather than raised on: this runs only
    when the transport already reported no calls, so the worst case must stay
    "no calls recovered" rather than turning a plain answer into an error.
    """

    calls: list[ToolCall] = []
    for raw in _HERMES_CALL.findall(text):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        name = str(decoded.get("name") or "").strip()
        arguments = decoded.get("arguments")
        if name in allowed and isinstance(arguments, dict):
            calls.append(
                ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
            )
    for name, body in _XML_FUNCTION.findall(text):
        if name not in allowed:
            continue
        arguments = {
            key: _coerce_argument(value) for key, value in _XML_PARAMETER.findall(body)
        }
        if arguments:
            calls.append(
                ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
            )
    return calls


def _strip_tool_block(text: str) -> str:
    return _FENCE.sub("", text).strip()


def _strip_salvaged(text: str) -> str:
    """Remove recovered call syntax so it never reaches the user as prose."""

    return _SALVAGE_LEFTOVERS.sub("", text).strip()


def request_tool_calls(
    client: Any,
    messages: list[LLMMessage],
    schemas: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    num_predict: int | None = None,
) -> ModelTurn:
    """Ask the model what to do next, using whichever transport it supports.

    Returns the assistant's prose and the tool calls it requested. An empty list
    means the model asked for no tool this turn -- which the caller must treat as
    "stopped talking", not as "succeeded".
    """

    allowed = {schema.get("function", schema).get("name") for schema in schemas}
    allowed.discard(None)

    if bool(getattr(client, "supports_tools", lambda: False)()):
        result = client.chat_with_metadata(
            messages, temperature=temperature, num_predict=num_predict, tools=schemas
        )
        calls = [
            ToolCall(id=call["id"], name=call["name"], arguments=call.get("arguments") or {})
            for call in result.tool_calls
            if call.get("name") in allowed
        ]
        if not calls:
            # The model may have written the call into its prose instead. Only
            # reachable when the structured field was empty, so a genuine final
            # answer is untouched.
            calls = salvage_tool_calls(result.content, allowed)
            if calls:
                return ModelTurn(_strip_salvaged(result.content), calls, result.finish_reason)
        return ModelTurn(result.content, calls, result.finish_reason)

    attempt_messages = list(messages)
    for attempt in range(2):
        result = client.chat_with_metadata(
            attempt_messages, temperature=temperature, num_predict=num_predict
        )
        try:
            calls = parse_text_tool_calls(result.content, allowed)
        except ToolProtocolError:
            # A model ignoring the JSON protocol for its own native format is
            # answering correctly in the wrong dialect; take it before retrying.
            salvaged = salvage_tool_calls(result.content, allowed)
            if salvaged:
                return ModelTurn(
                    _strip_salvaged(result.content), salvaged, result.finish_reason
                )
            # Prose that merely mentions a tool is a final answer, not a broken
            # attempt, so only a real attempt is worth a repair round trip.
            if attempt == 0 and _looks_like_a_tool_attempt(result.content):
                attempt_messages = [
                    *attempt_messages,
                    LLMMessage(role="assistant", content=result.content),
                    LLMMessage(role="user", content=_REPAIR_PROMPT),
                ]
                continue
            raise
        if not calls:
            salvaged = salvage_tool_calls(result.content, allowed)
            if salvaged:
                return ModelTurn(
                    _strip_salvaged(result.content), salvaged, result.finish_reason
                )
        return ModelTurn(
            _strip_tool_block(result.content) if calls else result.content,
            calls,
            result.finish_reason,
        )

    raise ToolProtocolError("The model did not produce a usable tool call.")
