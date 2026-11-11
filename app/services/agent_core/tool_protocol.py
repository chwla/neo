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
from app.services.disclosure import Label, invocation_label
from app.services.llm import LLMMessage

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

# Local models intermittently emit a tool call as *prose* in their own training
# format instead of through the provider's structured channel -- observed roughly
# one turn in eight with qwen3-coder on Ollama. The turn is not malformed from
# the model's point of view; it simply never reaches the tool-call field, so the
# loop sees "no tools requested" and treats real work as a finished answer.
# Recognising the two common formats turns that dropped turn into a real call.
_XML_FUNCTION = re.compile(r"<function=([A-Za-z0-9_.\-]+)\s*>(.*?)(?:</function>|\Z)", re.DOTALL)
_XML_PARAMETER = re.compile(r"<parameter=([A-Za-z0-9_.\-]+)\s*>(.*?)(?:</parameter>|\Z)", re.DOTALL)
_HERMES_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.DOTALL)
_SALVAGE_LEFTOVERS = re.compile(
    r"<function=.*?(?:</function>|\Z)|<tool_call>.*?(?:</tool_call>|\Z)|</tool_call>",
    re.DOTALL,
)

_REPAIR_PROMPT = (
    "Use only the available tools and supply their required arguments. "
    "If no available tool can fulfill the request, explain what is missing in plain prose."
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
    #: What answered and what it cost, for the run's transcript. The chat path
    #: records the same on every message; an agent turn is no less a model call.
    metadata: dict[str, Any] = field(default_factory=dict)
    source_label: Label = field(default_factory=lambda: Label.unknown("unlabelled_model_turn"))

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
        lines.append(f"- {function.get('name')}({arguments}): {function.get('description', '')}")
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

    return bool(
        re.search(r'"tool_calls"\s*:|<tool_call>|<function=', text)
        or re.match(r'\s*\{\s*"name"\s*:', _extract_json_object(text) or "")
    )


def parse_text_tool_calls(text: str, allowed: set[str]) -> list[ToolCall]:
    """Parse the fallback protocol. Raises ``ToolProtocolError`` on malformed input.

    A lone ``{"name": ..., "parameters": ...}`` object is taken as one call. That
    is Llama 3.x's trained format, and small models keep producing it even after
    being asked for the array; refusing it cost a whole extra model call (about a
    minute on CPU) only to end the run on a call that was perfectly clear.
    """

    payload = _extract_json_object(text)
    if payload is None:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ToolProtocolError(f"Tool call block was not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ToolProtocolError("Tool call block must be a JSON object.")
    if "tool_calls" not in decoded:
        if "name" in decoded and ("arguments" in decoded or "parameters" in decoded):
            decoded = {"tool_calls": [decoded]}
        else:
            return []
    raw_calls = decoded["tool_calls"]
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
        arguments = raw.get("arguments", raw.get("parameters", {}))
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
        arguments = {key: _coerce_argument(value) for key, value in _XML_PARAMETER.findall(body)}
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

    functions = [schema.get("function", schema) for schema in schemas]
    required = {
        function["name"]: (function.get("parameters") or {}).get("required", [])
        for function in functions
        if function.get("name")
    }
    allowed = set(required)
    call_label = invocation_label(message.source_label for message in messages)

    def meta(result: Any) -> dict[str, Any]:
        return {
            key: value
            for key, value in (
                ("provider_name", getattr(result, "provider_name", None)),
                ("model_name", getattr(result, "model_name", None)),
                ("total_tokens", getattr(result, "total_tokens", None)),
                ("duration_ms", getattr(result, "duration_ms", None)),
            )
            if value is not None
        }

    native = bool(getattr(client, "supports_tools", lambda: False)())
    attempt_messages = list(messages)
    for attempt in range(2):
        result = client.chat_with_metadata(
            attempt_messages, temperature=temperature, num_predict=num_predict,
            **({"tools": schemas} if native else {}),
        )
        content = result.content
        try:
            calls = []
            if native and result.tool_calls:
                for raw in result.tool_calls:
                    name = raw["name"]
                    arguments = raw.get("arguments")
                    if not isinstance(arguments, dict):
                        raise ToolProtocolError(f"Arguments for '{name}' must be an object.")
                    calls.append(ToolCall(id=raw["id"], name=name, arguments=arguments))
            else:
                calls = salvage_tool_calls(content, allowed)
                if calls:
                    content = _strip_salvaged(content)
                elif _looks_like_a_tool_attempt(content):
                    calls = parse_text_tool_calls(content, allowed)
                    if not calls and re.search(r"<tool_call>|<function=", content):
                        raise ToolProtocolError("The response did not contain a usable call.")
                    if calls:
                        payload = _extract_json_object(content)
                        content = "" if content.strip() == payload else _strip_tool_block(content)

            for call in calls:
                if call.name not in allowed:
                    raise ToolProtocolError(f"Tool '{call.name}' is not available.")
                missing = set(required[call.name]) - call.arguments.keys()
                if missing:
                    raise ToolProtocolError(
                        f"'{call.name}' needs these arguments: {', '.join(sorted(missing))}."
                    )
            return ModelTurn(content, calls, result.finish_reason, meta(result), call_label)
        except ToolProtocolError as exc:
            if result.finish_reason == "length":
                return ModelTurn("", [], result.finish_reason, meta(result), call_label)
            if attempt:
                raise
            format_hint = (
                "Use the provider's tool call field." if native else
                'Use a fenced JSON object with a "tool_calls" array, "name" and "arguments".'
            )
            attempt_messages = [
                *attempt_messages,
                LLMMessage(
                    role="user",
                    content=f"The last tool request failed: {exc}\n{_REPAIR_PROMPT} {format_hint}",
                    source_label=call_label,
                ),
            ]

    raise ToolProtocolError("The model did not produce a usable tool call.")
