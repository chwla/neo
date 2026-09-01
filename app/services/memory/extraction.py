"""Bounded extraction-model provider protocols for Phase 4."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import ModelExtractionInput
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelProposalResponse,
    SubjectHint,
)
from app.services.memory.taxonomy import CORE_IDENTITY_SLOT_KEYS, MemoryType

MAX_PROVIDER_RESPONSE_BYTES = 128_000
MAX_SANITIZED_ERROR_CHARS = 240
PROMPT_VERSION = "memory-extraction-schema-v2-fields"
TWO_STAGE_PROMPT_VERSION = "memory-extraction-two-stage-v1"

# The identity attributes recall privileges, taken from the slot keys themselves so
# the extractor cannot propose an attribute the fast path does not recognise.
_IDENTITY_KEYS = frozenset(
    key.removeprefix("identity:global:") for key in CORE_IDENTITY_SLOT_KEYS
)
SYNTHETIC_PROBE_INPUT = "PHASE4_OLLAMA_SYNTHETIC_CAPABILITY_PROBE"
#: What the capability probe asks for. Exact and tiny on purpose -- see the
#: comment on ``base_payload`` in ``probe_ollama_provider``.
OLLAMA_PROBE_INSTRUCTION = (
    "Reply with exactly this JSON object and nothing else, on one line, "
    'with no code fence and no explanation: {"probe":"ok"}'
)
#: Generous next to the six tokens the instruction above actually produces. Low
#: enough to bound a model that ignores it, high enough that no honest reply is
#: cut off and misread as an unsupported option.
PROBE_NUM_PREDICT = 256


def _extraction_keep_alive() -> str:
    """How long extraction asks Ollama to keep the model resident.

    Read from the same setting the chat client uses rather than pinned here.
    Ollama applies whichever value arrived last, so a hardcoded "10m" on a
    background extraction quietly overrode the chat client's "30m" and the next
    message paid a full cold load -- about six seconds on this hardware, landing
    on the user rather than on the background job that caused it.
    """

    from app.core.config import get_settings

    return get_settings().ollama_keep_alive

OLLAMA_SYSTEM_INSTRUCTION = """You are a bounded memory proposal extractor.
Return exactly one JSON object matching the supplied schema. Do not return prose,
markdown, analysis, or reasoning. Use only user-authored spans supplied in the input.
Never invent owner IDs, canonical memory IDs, lifecycle state, database operations,
or trusted predecessor IDs. Every proposal you return is independently re-checked
downstream by grounding, taxonomy, sensitivity, and correction policy, so propose the
fact and let those layers decide whether it is kept. Use the exclusions array only for
a statement that is temporary, hypothetical, about a third party, or authored by the
assistant; a statement the user makes about themselves is a proposal, not an exclusion.

Every memory belongs to exactly one of six fields. Choose memory_type_hint so the
fact lands in the right field:
- Profile: who the user is. Use "identity" for name, age, origin, location;
  "education" for schools and degrees; "employment" for employer and role.
- Preferences: how the user wants things done. Use "preference".
- Goals: what the user intends to achieve. Use "goal".
- Projects: a named body of work the user is building. Use "project".
- Events: something that happened or is scheduled. Use "event", or "activity"
  for what the user is currently doing.
- Miscellaneous: any other durable fact. Use "knowledge".

Use "project" only for a fact that is specific to the named piece of work being
discussed, because those memories are readable only inside that project. A fact
that stays true for the user everywhere is never a "project" fact: preferences,
goals, profile details and events belong to their own field even when the user
happens to mention them while working on a project."""

# What to extract, in every request mode.  This guidance used to live inside the JSON
# mode contract below, so schema mode — which `auto` selects whenever Ollama advertises
# grammar support — never received it.  The model then saw only the terse instruction
# above and returned zero assertions for every implicitly stated fact.  Grammar mode
# constrains the response *shape*; nothing but this text constrains the response
# *content*, so it must be sent regardless of mode.
OLLAMA_EXTRACTION_CONTRACT = """Extraction contract:
Stable first-person identity, work/tool facts, preferences, recurring activities,
ongoing goals, and projects are durable assertions even when
explicit_memory_intent is false. Temporary, hypothetical, question, and
third-party statements are not durable assertions. For a durable assertion,
copy the input message_id and cite the shortest exact supporting substring with
zero-based start/end character offsets and an identical quoted_text. typed_value
and display_hint must be supported by that exact quoted span.
For example, in the text "I use Python for work.", the durable knowledge value
"Python" is supported by quoted_text "Python" at start 6 and end 12, with
domain_hint "software_development", durability "durable", and
sensitivity_hint "normal".
Use empty arrays when there are no matching proposals."""

OLLAMA_JSON_SHAPE_CONTRACT = """JSON mode response contract:
Return exactly these top-level keys: schema_version, assertions, retractions, exclusions.
schema_version must be 1 and the other three values must be JSON arrays.
Assertion objects may contain only: proposal_id, source_spans, subject_hint,
memory_type_hint, domain_hint, slot_hint, typed_value, display_hint, durability,
confidence, sensitivity_hint, correction_group, explicit_type_change,
explicit_domain_change, explicit_slot_change, additive, expires_at.
Every assertion requires proposal_id, source_spans, subject_hint, memory_type_hint,
typed_value, display_hint, durability, confidence, and sensitivity_hint.
subject_hint is exactly user, other, or ambiguous. memory_type_hint is exactly
identity, preference, goal, project, education, employment, activity, event, or
knowledge. durability is exactly durable, temporary, or uncertain.
sensitivity_hint is exactly normal, sensitive, or prohibited.
Retraction objects may contain only: proposal_id, source_spans, subject_hint,
old_value_hint, memory_type_hint, domain_hint, slot_hint, confidence,
correction_group, explicit_forget.
Every retraction requires proposal_id, source_spans, subject_hint, old_value_hint,
and confidence.
Exclusion objects may contain only: proposal_id, reason.
Source span objects may contain only: message_id, start, end, quoted_text.
Never add wrapper keys such as proposal, memory, result, reasoning, explanation,
action, operation, target_id, memory_id, owner_id, or canonical_id."""


def proposal_response_schema() -> dict[str, Any]:
    """Return the response schema with every top-level key required.

    Pydantic omits a field from ``required`` when it has a default, and all four
    top-level fields default to empty.  A grammar built from that schema therefore
    accepts ``{}``, and a constrained model takes the cheapest path the grammar
    allows: ``qwen3-coder:30b`` returned ``{"exclusions": []}`` — one key, no
    assertions — for every message, because emitting nothing was valid.  Forcing the
    keys to be required makes the grammar demand an ``assertions`` array, so the model
    has to decide what belongs in it instead of declining to answer.

    The defaults stay in place for parsing, so a response that omits a key is still
    accepted on the way back in; this only constrains generation.
    """

    schema = ModelProposalResponse.model_json_schema()
    schema["required"] = ["schema_version", "assertions", "retractions", "exclusions"]
    return schema


class ExtractionProviderKind(StrEnum):
    DIRECT_JSON = "direct_json"
    OLLAMA = "ollama"
    FIXTURE = "fixture"


class OllamaRequestMode(StrEnum):
    AUTO = "auto"
    SCHEMA = "ollama_schema"
    JSON = "ollama_json"


@dataclass(frozen=True)
class ProviderResponseMetadata:
    provider_kind: str
    http_status: int | None = None
    response_envelope_shape: str = "not_applicable"
    content_present: bool = False
    content_byte_length: int = 0
    response_content_hash: str | None = None
    sanitized_failure_code: str | None = None
    sanitized_error_message: str | None = None
    timeout_stage: str | None = None


@dataclass(frozen=True)
class OllamaCapabilities:
    schema_format_supported: bool = False
    json_format_supported: bool = False
    think_field_supported: bool = False
    seed_option_supported: bool = False
    num_predict_option_supported: bool = False
    keep_alive_supported: bool = False


@dataclass(frozen=True)
class OllamaProbeResult:
    provider_reachable: bool
    model_available: bool
    warmup_success: bool
    ollama_version: str | None
    capabilities: OllamaCapabilities
    selected_request_mode: OllamaRequestMode | None
    sanitized_failure_code: str | None = None
    sanitized_error_message: str | None = None
    warmup_latency_ms: int | None = None

    @property
    def successful(self) -> bool:
        return bool(
            self.provider_reachable
            and self.model_available
            and self.warmup_success
            and self.selected_request_mode is not None
        )


@dataclass(frozen=True)
class HttpTransportResponse:
    status: int
    body: bytes


class ProviderTransportTimeout(TimeoutError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


class ProviderTransportFailure(OSError):
    pass


class JsonHttpTransport(Protocol):
    def request(
        self,
        method: str,
        endpoint: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
    ) -> HttpTransportResponse: ...


class StdlibJsonHttpTransport:
    """HTTP transport with distinct connection and response/read timeouts."""

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
    ) -> HttpTransportResponse:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderTransportFailure("invalid_provider_endpoint")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        kwargs: dict[str, Any] = {
            "host": parsed.hostname,
            "port": port,
            "timeout": connect_timeout_seconds,
        }
        if connection_type is http.client.HTTPSConnection:
            kwargs["context"] = ssl.create_default_context()
        connection = connection_type(**kwargs)
        stage = "connect"
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout_seconds)
            stage = "read"
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            connection.request(method, path, body=body, headers=dict(headers))
            response = connection.getresponse()
            payload = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            return HttpTransportResponse(status=response.status, body=payload)
        except TimeoutError:
            raise ProviderTransportTimeout(stage) from None
        except (OSError, http.client.HTTPException):
            raise ProviderTransportFailure("provider_transport_failure") from None
        finally:
            connection.close()


class ExtractionModelError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        metadata: ProviderResponseMetadata | None = None,
    ) -> None:
        self.code = code
        self.metadata = metadata
        super().__init__(code)


class ExtractionModelTimeout(ExtractionModelError):
    pass


@dataclass(frozen=True)
class ExtractionModelResponse:
    raw_output: str | bytes | dict[str, Any]
    model_version: str
    prompt_version: str
    metadata: ProviderResponseMetadata = ProviderResponseMetadata(
        provider_kind=ExtractionProviderKind.FIXTURE.value,
        response_envelope_shape="fixture",
    )


class ExtractionModel(Protocol):
    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse: ...


class ExtractionModelProvider(ExtractionModel, Protocol):
    provider_kind: ExtractionProviderKind
    call_count: int


def _encoded_output(value: str | bytes | dict[str, Any]) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _content_metadata(
    provider_kind: ExtractionProviderKind,
    content: bytes,
    *,
    http_status: int | None = None,
    response_envelope_shape: str,
    sanitized_failure_code: str | None = None,
    sanitized_error_message: str | None = None,
    timeout_stage: str | None = None,
) -> ProviderResponseMetadata:
    return ProviderResponseMetadata(
        provider_kind=provider_kind.value,
        http_status=http_status,
        response_envelope_shape=response_envelope_shape,
        content_present=bool(content),
        content_byte_length=len(content),
        response_content_hash=(hashlib.sha256(content).hexdigest() if content else None),
        sanitized_failure_code=sanitized_failure_code,
        sanitized_error_message=sanitized_error_message,
        timeout_stage=timeout_stage,
    )


def _safe_error_message(
    value: object,
    *,
    forbidden_texts: Sequence[str] = (),
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > MAX_SANITIZED_ERROR_CHARS:
        return None
    if not re.fullmatch(r"[A-Za-z0-9 _.,:;/'\"(){}\[\]+=@-]+", normalized):
        return None
    lowered = normalized.casefold()
    unsafe_markers = (
        "password",
        "api key",
        "access token",
        "secret",
        "diagnosis",
        "my memory",
    )
    if any(marker in lowered for marker in unsafe_markers):
        return None
    for forbidden in forbidden_texts:
        candidate = " ".join(forbidden.strip().split()).casefold()
        if len(candidate) >= 4 and candidate in lowered:
            return None
    return normalized


def _ollama_error_text(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


def _ollama_failure_code(status: int, error: str | None) -> str:
    lowered = (error or "").casefold()
    if "not found" in lowered and "model" in lowered:
        return "ollama_model_not_found"
    if "invalid model" in lowered or "model name" in lowered:
        return "ollama_invalid_model_name"
    if "unknown field" in lowered and "think" in lowered:
        return "ollama_unknown_field_think"
    if "failed to parse grammar" in lowered or "failed to initialize samplers" in lowered:
        return "ollama_unsupported_format_schema"
    if "format" in lowered and any(
        marker in lowered for marker in ("schema", "object", "unmarshal", "unsupported")
    ):
        return "ollama_unsupported_format_schema"
    if "option" in lowered or "seed" in lowered or "num_predict" in lowered:
        return "ollama_invalid_options"
    if any(marker in lowered for marker in ("too large", "context length", "request size")):
        return "ollama_request_too_large"
    if any(marker in lowered for marker in ("out of memory", "system memory", "memory required")):
        return "ollama_insufficient_memory"
    if any(marker in lowered for marker in ("runner process", "failed to load", "load model")):
        return "ollama_model_load_failed"
    if status >= 500:
        return "ollama_server_error"
    return "ollama_invalid_request"


class FixtureExtractionModel:
    """Deterministic fixture provider; values may be responses, sequences, or exceptions."""

    provider_kind = ExtractionProviderKind.FIXTURE

    def __init__(
        self,
        fixtures: Mapping[
            str,
            str
            | bytes
            | dict[str, Any]
            | Exception
            | Sequence[str | bytes | dict[str, Any] | Exception],
        ],
        *,
        model_version: str = "memory-fixture-v1",
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._fixtures = dict(fixtures)
        self._positions: dict[str, int] = {}
        self.model_version = model_version
        self.prompt_version = prompt_version
        self.call_count = 0

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        self.call_count += 1
        if request.user_message not in self._fixtures:
            raise ExtractionModelError(
                "fixture_not_found",
                metadata=ProviderResponseMetadata(
                    provider_kind=self.provider_kind.value,
                    response_envelope_shape="fixture_missing",
                ),
            )
        fixture = self._fixtures[request.user_message]
        if isinstance(fixture, Sequence) and not isinstance(fixture, (str, bytes, dict)):
            position = self._positions.get(request.user_message, 0)
            value = fixture[min(position, len(fixture) - 1)]
            self._positions[request.user_message] = position + 1
        else:
            value = fixture
        if isinstance(value, Exception):
            raise value
        encoded = _encoded_output(value)
        return ExtractionModelResponse(
            raw_output=value,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            metadata=_content_metadata(
                self.provider_kind,
                encoded,
                response_envelope_shape="fixture",
            ),
        )


class _JsonHttpExtractionProvider:
    provider_kind: ExtractionProviderKind

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        connect_timeout_seconds: int = 5,
        response_timeout_seconds: int | None = None,
        timeout_seconds: int | None = None,
        bearer_token: str | None = None,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("extraction_endpoint_required")
        if not model.strip():
            raise ValueError("extraction_model_required")
        # `is None` rather than an `or` chain: zero is falsy, so `or` treated an
        # explicit 0 as "not supplied" and substituted the default before the
        # range check below ever saw it.  Zero was the single out-of-range value
        # that did not raise.
        if response_timeout_seconds is not None:
            response_timeout = response_timeout_seconds
        elif timeout_seconds is not None:
            response_timeout = timeout_seconds
        else:
            response_timeout = 120
        if not 1 <= connect_timeout_seconds <= 60:
            raise ValueError("extraction_connect_timeout_out_of_range")
        if not 1 <= response_timeout <= 600:
            raise ValueError("extraction_response_timeout_out_of_range")
        self.endpoint = endpoint
        self.model = model
        self.connect_timeout_seconds = connect_timeout_seconds
        self.response_timeout_seconds = response_timeout
        self._bearer_token = bearer_token
        self._transport = transport or StdlibJsonHttpTransport()
        self.call_count = 0
        self.last_sanitized_error_message: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    def _http_error(
        self,
        status: int,
        body: bytes,
        *,
        forbidden_texts: Sequence[str],
    ) -> tuple[str, str, str | None]:
        del forbidden_texts
        return (
            f"model_http_{status}",
            "http_error_body" if body else "http_error_empty",
            None,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any] | None,
        read_timeout_seconds: int | None = None,
    ) -> HttpTransportResponse:
        encoded = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        return self._transport.request(
            method,
            endpoint,
            body=encoded,
            headers=self._headers(),
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=(read_timeout_seconds or self.response_timeout_seconds),
        )

    def _post(
        self,
        payload: dict[str, Any],
        *,
        read_timeout_seconds: int | None = None,
        forbidden_texts: Sequence[str] = (),
    ) -> tuple[int, bytes]:
        self.call_count += 1
        self.last_sanitized_error_message = None
        try:
            response = self._request(
                "POST",
                self.endpoint,
                payload=payload,
                read_timeout_seconds=read_timeout_seconds,
            )
        except ProviderTransportTimeout as exc:
            raise ExtractionModelTimeout(
                "model_timeout",
                metadata=ProviderResponseMetadata(
                    provider_kind=self.provider_kind.value,
                    sanitized_failure_code=f"provider_{exc.stage}_timeout",
                    timeout_stage=exc.stage,
                ),
            ) from None
        except ProviderTransportFailure:
            raise ExtractionModelError(
                "model_transport_failure",
                metadata=ProviderResponseMetadata(
                    provider_kind=self.provider_kind.value,
                    sanitized_failure_code="provider_transport_failure",
                ),
            ) from None
        body = response.body
        if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ExtractionModelError(
                "model_output_too_large",
                metadata=ProviderResponseMetadata(
                    provider_kind=self.provider_kind.value,
                    http_status=response.status,
                    response_envelope_shape="oversized_response",
                    sanitized_failure_code="provider_response_too_large",
                ),
            )
        if response.status >= 400:
            code, shape, safe_message = self._http_error(
                response.status,
                body,
                forbidden_texts=forbidden_texts,
            )
            metadata = _content_metadata(
                self.provider_kind,
                body,
                http_status=response.status,
                response_envelope_shape=shape,
                sanitized_failure_code=code,
                sanitized_error_message=safe_message,
            )
            self.last_sanitized_error_message = safe_message
            raise ExtractionModelError(code, metadata=metadata)
        return response.status, body


class DirectJsonExtractionProvider(_JsonHttpExtractionProvider):
    """HTTP provider whose response body is the extraction-schema JSON itself."""

    provider_kind = ExtractionProviderKind.DIRECT_JSON

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        status, body = self._post(
            {
                "model": self.model,
                "input": request.model_dump(mode="json"),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": proposal_response_schema(),
                },
                "temperature": 0,
                "stream": False,
            },
            forbidden_texts=(
                request.user_message,
                *(item.content for item in request.supporting_window),
            ),
        )
        return ExtractionModelResponse(
            raw_output=body,
            model_version=self.model,
            prompt_version=PROMPT_VERSION,
            metadata=_content_metadata(
                self.provider_kind,
                body,
                http_status=status,
                response_envelope_shape="direct_schema_body_v1",
            ),
        )


class OllamaChatExtractionProvider(_JsonHttpExtractionProvider):
    """Strict adapter for Ollama's non-streaming ``/api/chat`` envelope."""

    provider_kind = ExtractionProviderKind.OLLAMA

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        request_mode: OllamaRequestMode | str = OllamaRequestMode.SCHEMA,
        capabilities: OllamaCapabilities | None = None,
        connect_timeout_seconds: int = 5,
        response_timeout_seconds: int | None = None,
        timeout_seconds: int | None = None,
        bearer_token: str | None = None,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        super().__init__(
            endpoint,
            model=model,
            connect_timeout_seconds=connect_timeout_seconds,
            response_timeout_seconds=response_timeout_seconds,
            timeout_seconds=timeout_seconds,
            bearer_token=bearer_token,
            transport=transport,
        )
        try:
            mode = OllamaRequestMode(request_mode)
        except ValueError:
            raise ValueError("unsupported_ollama_request_mode") from None
        if mode is OllamaRequestMode.AUTO:
            raise ValueError("ollama_auto_mode_requires_capability_probe")
        self.request_mode = mode
        self.capabilities = capabilities or OllamaCapabilities(
            schema_format_supported=mode is OllamaRequestMode.SCHEMA,
            json_format_supported=mode is OllamaRequestMode.JSON,
            num_predict_option_supported=True,
        )

    def _http_error(
        self,
        status: int,
        body: bytes,
        *,
        forbidden_texts: Sequence[str],
    ) -> tuple[str, str, str | None]:
        error = _ollama_error_text(body)
        code = _ollama_failure_code(status, error)
        return (
            code,
            "ollama_error_v1" if error is not None else "unknown_http_error_envelope",
            _safe_error_message(error, forbidden_texts=forbidden_texts),
        )

    @staticmethod
    def _format_for(mode: OllamaRequestMode) -> str | dict[str, Any]:
        if mode is OllamaRequestMode.SCHEMA:
            return proposal_response_schema()
        return "json"

    def _payload(self, request: ModelExtractionInput) -> dict[str, Any]:
        system_instruction = f"{OLLAMA_SYSTEM_INSTRUCTION}\n{OLLAMA_EXTRACTION_CONTRACT}"
        if self.request_mode is OllamaRequestMode.JSON:
            system_instruction = f"{system_instruction}\n{OLLAMA_JSON_SHAPE_CONTRACT}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        request.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "stream": False,
            "format": self._format_for(self.request_mode),
            "options": {"temperature": 0},
        }
        if self.capabilities.think_field_supported:
            payload["think"] = False
        if self.capabilities.seed_option_supported:
            payload["options"]["seed"] = 0
        if self.capabilities.num_predict_option_supported:
            payload["options"]["num_predict"] = 2048
        if self.capabilities.keep_alive_supported:
            payload["keep_alive"] = _extraction_keep_alive()
        return payload

    def _decode_response(
        self,
        status: int,
        body: bytes,
        *,
        forbidden_texts: Sequence[str] = (),
    ) -> ExtractionModelResponse:
        outer_metadata = _content_metadata(
            self.provider_kind,
            body,
            http_status=status,
            response_envelope_shape="unknown_response_envelope",
        )
        try:
            envelope = json.loads(body)
        except (TypeError, ValueError):
            lines = tuple(line for line in body.splitlines() if line.strip())
            if len(lines) > 1:
                raise ExtractionModelError(
                    "streamed_response_rejected",
                    metadata=replace(
                        outer_metadata,
                        response_envelope_shape="ollama_ndjson_stream",
                    ),
                ) from None
            raise ExtractionModelError(
                "malformed_provider_envelope", metadata=outer_metadata
            ) from None
        if not isinstance(envelope, dict):
            raise ExtractionModelError("unknown_response_envelope", metadata=outer_metadata)
        if isinstance(envelope.get("error"), str):
            code = _ollama_failure_code(status, envelope["error"])
            safe_message = _safe_error_message(envelope["error"], forbidden_texts=forbidden_texts)
            self.last_sanitized_error_message = safe_message
            raise ExtractionModelError(
                code,
                metadata=replace(
                    outer_metadata,
                    response_envelope_shape="ollama_error_v1",
                    sanitized_failure_code=code,
                    sanitized_error_message=safe_message,
                ),
            )
        if envelope.get("done") is False:
            raise ExtractionModelError(
                "streamed_response_rejected",
                metadata=replace(
                    outer_metadata,
                    response_envelope_shape="ollama_partial_chat_v1",
                ),
            )
        message = envelope.get("message")
        if not isinstance(message, dict) or (message.get("role") not in {None, "assistant"}):
            raise ExtractionModelError("unknown_response_envelope", metadata=outer_metadata)
        content = message.get("content")
        if not isinstance(content, str):
            raise ExtractionModelError("unknown_response_envelope", metadata=outer_metadata)
        content_bytes = content.encode("utf-8")
        metadata = _content_metadata(
            self.provider_kind,
            content_bytes,
            http_status=status,
            response_envelope_shape="ollama_chat_v1",
        )
        if not content.strip():
            raise ExtractionModelError("empty_model_content", metadata=metadata)
        if len(content_bytes) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ExtractionModelError("model_output_too_large", metadata=metadata)
        return ExtractionModelResponse(
            raw_output=content,
            model_version=str(envelope.get("model") or self.model),
            prompt_version=PROMPT_VERSION,
            metadata=metadata,
        )

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        status, body = self._post(
            self._payload(request),
            forbidden_texts=(
                request.user_message,
                *(item.content for item in request.supporting_window),
            ),
        )
        return self._decode_response(
            status,
            body,
            forbidden_texts=(
                request.user_message,
                *(item.content for item in request.supporting_window),
            ),
        )


TWO_STAGE_FACT_INSTRUCTION = """You extract durable facts about the user from their message.

Return JSON: {"facts": [{"fact": "...", "quote": "...", "value": "..."}]}

For each fact:
- "fact": one short sentence about the user, third person, starting with "User".
- "quote": the exact words COPIED CHARACTER-FOR-CHARACTER from the user's message that
  state this fact. It must appear verbatim in the message. Never rephrase it.
- "value": the specific thing being remembered, copied from the message (a name, place,
  tool, role, or short phrase).

Extract facts even when the user did not ask you to remember anything.
Include: identity, job, employer, role, location, preferences, ongoing goals, projects,
skills, relationships, recurring activities.
Exclude: questions, temporary states or moods, hypotheticals, facts about other people,
and anything the assistant said.
If there is nothing durable, return {"facts": []}.

Examples:
Message: "Hi, my name is John. I am a software engineer."
Output: {"facts": [
  {"fact": "User's name is John", "quote": "my name is John", "value": "John"},
  {"fact": "User is a software engineer", "quote": "I am a software engineer",
   "value": "software engineer"}]}

Message: "what's the weather like?"
Output: {"facts": []}

Message: "I'm really tired today"
Output: {"facts": []}"""

TWO_STAGE_CLASSIFY_INSTRUCTION = """Classify one fact about the user.

Return JSON: {"memory_type": "...", "identity_key": null}

memory_type is exactly one of:
identity, preference, goal, project, education, employment, activity, event, knowledge

Use "identity" for who the user durably is: their name, age, origin, employer,
occupation, or where they currently live.
When memory_type is "identity", identity_key is exactly one of:
name, age, origin, employer, occupation, current_location
For every other memory_type, identity_key is null.

Examples:
Fact: "User's name is John"           -> {"memory_type":"identity","identity_key":"name"}
Fact: "User is a software engineer" -> {"memory_type":"identity","identity_key":"occupation"}
Fact: "User works at Stripe"          -> {"memory_type":"identity","identity_key":"employer"}
Fact: "User lives in Berlin" -> {"memory_type":"identity","identity_key":"current_location"}
Fact: "User prefers dark mode"        -> {"memory_type":"preference","identity_key":null}
Fact: "User wants to learn Portuguese" -> {"memory_type":"goal","identity_key":null}"""

_FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fact", "quote", "value"],
                "properties": {
                    "fact": {"type": "string"},
                    "quote": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        }
    },
}

_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["memory_type", "identity_key"],
    "properties": {
        "memory_type": {
            "type": "string",
            "enum": [item.value for item in MemoryType],
        },
        "identity_key": {
            "type": ["string", "null"],
            "enum": [None, *sorted(_IDENTITY_KEYS)],
        },
    },
}


class TwoStageOllamaChatExtractionProvider(OllamaChatExtractionProvider):
    """Ask the model what is worth remembering, then how to file it.

    The single-call contract required one grammar-constrained pass to choose a
    nine-way type, a domain, a slot, a typed value, a display string, a durability,
    a confidence, a sensitivity *and* character offsets whose quoted text matched
    the source byte for byte.  Local models answer that badly: they returned a
    classification object in ``typed_value`` and a taxonomy label such as
    ``"location"`` in ``display_hint``, so a correctly understood sentence still
    produced an unusable memory.  Constrained decoding makes this worse rather
    than better — the grammar guarantees the shape, never the meaning.

    Splitting it lets each call be easy.  Stage A asks only for short natural
    sentences plus the words the user actually wrote, which local models get right
    across colloquial phrasings the deterministic grammars never matched.  Stage B
    asks only for one enum per fact, which is where ``identity:global:employer``
    finally comes from, so a question sharing no words with the stored value can
    still reach it.

    Offsets are computed here from the returned quote rather than requested from
    the model, so grounding keeps the exact-span guarantee it always had while the
    model is never asked to count characters.
    """

    def _chat_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        forbidden_texts: Sequence[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema if self.request_mode is OllamaRequestMode.SCHEMA else "json",
            "options": {"temperature": 0},
        }
        if self.capabilities.think_field_supported:
            payload["think"] = False
        if self.capabilities.num_predict_option_supported:
            payload["options"]["num_predict"] = 2048
        if self.capabilities.keep_alive_supported:
            payload["keep_alive"] = _extraction_keep_alive()
        status, body = self._post(payload, forbidden_texts=forbidden_texts)
        decoded = self._decode_response(status, body, forbidden_texts=forbidden_texts)
        raw = decoded.raw_output
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(text) if isinstance(text, str) else text
        except (TypeError, ValueError):
            raise ExtractionModelError(
                "malformed_model_json",
                metadata=decoded.metadata,
            ) from None
        if not isinstance(parsed, dict):
            raise ExtractionModelError("malformed_model_json", metadata=decoded.metadata)
        return parsed

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        forbidden = (
            request.user_message,
            *(item.content for item in request.supporting_window),
        )
        facts_payload = self._chat_json(
            system=TWO_STAGE_FACT_INSTRUCTION,
            user=request.user_message,
            schema=_FACTS_SCHEMA,
            forbidden_texts=forbidden,
        )
        assertions: list[dict[str, Any]] = []
        raw_facts = facts_payload.get("facts")
        for index, item in enumerate(raw_facts if isinstance(raw_facts, list) else []):
            if len(assertions) >= request.maximum_candidates:
                break
            proposal = self._assertion_for(request, item, index)
            if proposal is not None:
                assertions.append(proposal)
        output = {
            "schema_version": 1,
            "assertions": assertions,
            "retractions": [],
            "exclusions": [],
        }
        return ExtractionModelResponse(
            raw_output=json.dumps(output, ensure_ascii=False),
            model_version=self.model,
            prompt_version=TWO_STAGE_PROMPT_VERSION,
            metadata=_content_metadata(
                self.provider_kind,
                json.dumps(output, ensure_ascii=False).encode("utf-8"),
                http_status=200,
                response_envelope_shape="ollama_two_stage_v1",
            ),
        )

    def _assertion_for(
        self,
        request: ModelExtractionInput,
        item: object,
        index: int,
    ) -> dict[str, Any] | None:
        """Turn one extracted fact into a proposal, or drop it if it is not grounded.

        A quote the model did not copy verbatim is discarded rather than repaired.
        Locating it ourselves is what keeps the span honest, and a quote we cannot
        find is exactly the case where the model has started paraphrasing.
        """

        if not isinstance(item, Mapping):
            return None
        fact = str(item.get("fact") or "").strip()
        quote = str(item.get("quote") or "").strip()
        value = str(item.get("value") or "").strip()
        if not fact or not quote:
            return None
        message = request.user_message
        start = message.find(quote)
        if start < 0:
            return None
        if not value or value not in message:
            value = quote
        memory_type, identity_key = self._classify(fact)
        proposal: dict[str, Any] = {
            "proposal_id": f"fact-{index}",
            "source_spans": [
                {
                    "message_id": request.message_id,
                    "start": start,
                    "end": start + len(quote),
                    "quoted_text": quote,
                }
            ],
            "subject_hint": SubjectHint.USER.value,
            "memory_type_hint": memory_type,
            "typed_value": value,
            "display_hint": fact,
            "durability": DurabilityHint.DURABLE.value,
            "confidence": 0.9,
            "sensitivity_hint": Sensitivity.NORMAL.value,
        }
        if identity_key is not None:
            proposal["slot_hint"] = identity_key
        return proposal

    def _classify(self, fact: str) -> tuple[str, str | None]:
        """Return the taxonomy type for one fact, defaulting to a storable type.

        A classification failure must not lose the fact: ``knowledge`` is the
        catch-all the taxonomy already uses for a durable statement that fits no
        richer field, so an unusable answer here costs precision, not the memory.
        """

        try:
            payload = self._chat_json(
                system=TWO_STAGE_CLASSIFY_INSTRUCTION,
                user=f"Fact: {fact}",
                schema=_CLASSIFY_SCHEMA,
                forbidden_texts=(),
            )
        except ExtractionModelError:
            return MemoryType.KNOWLEDGE.value, None
        raw_type = payload.get("memory_type")
        try:
            memory_type = MemoryType(str(raw_type)).value
        except ValueError:
            return MemoryType.KNOWLEDGE.value, None
        identity_key = payload.get("identity_key")
        if memory_type != MemoryType.IDENTITY.value:
            return memory_type, None
        if not isinstance(identity_key, str) or identity_key not in _IDENTITY_KEYS:
            # An identity fact with no usable attribute would land in
            # ``identity:global:profile_fact``, an exclusive slot every unlabelled
            # identity fact would then fight over, each overwriting the last.
            return MemoryType.KNOWLEDGE.value, None
        return memory_type, identity_key


def _ollama_api_endpoint(chat_endpoint: str, leaf: str) -> str:
    parsed = urlsplit(chat_endpoint)
    path = parsed.path
    if path.endswith("/api/chat"):
        path = path[: -len("/api/chat")]
    path = f"{path.rstrip('/')}/api/{leaf}"
    authority = parsed.netloc
    return f"{parsed.scheme}://{authority}{path}"


def _probe_chat(
    provider: OllamaChatExtractionProvider,
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
) -> tuple[bool, str | None, str | None, int]:
    started = monotonic()
    try:
        status, body = provider._post(  # noqa: SLF001 - same-module protocol probe
            payload,
            read_timeout_seconds=timeout_seconds,
        )
        provider._decode_response(status, body)  # noqa: SLF001
        return True, None, None, int((monotonic() - started) * 1000)
    except (ExtractionModelError, ExtractionModelTimeout) as exc:
        metadata = exc.metadata or ProviderResponseMetadata(provider_kind="ollama")
        return (
            False,
            metadata.sanitized_failure_code or exc.code,
            metadata.sanitized_error_message,
            int((monotonic() - started) * 1000),
        )


def probe_ollama_provider(
    endpoint: str,
    *,
    model: str,
    connect_timeout_seconds: int = 5,
    response_timeout_seconds: int = 120,
    warmup_timeout_seconds: int = 300,
    requested_mode: OllamaRequestMode | str = OllamaRequestMode.AUTO,
    bearer_token: str | None = None,
    transport: JsonHttpTransport | None = None,
) -> OllamaProbeResult:
    try:
        requested = OllamaRequestMode(requested_mode)
    except ValueError:
        raise ValueError("unsupported_ollama_request_mode") from None
    provider = OllamaChatExtractionProvider(
        endpoint,
        model=model,
        request_mode=OllamaRequestMode.SCHEMA,
        connect_timeout_seconds=connect_timeout_seconds,
        response_timeout_seconds=response_timeout_seconds,
        bearer_token=bearer_token,
        transport=transport,
    )

    def get_json(leaf: str) -> tuple[int | None, object | None, str | None]:
        try:
            response = provider._request(  # noqa: SLF001 - same-module protocol probe
                "GET",
                _ollama_api_endpoint(endpoint, leaf),
                payload=None,
                read_timeout_seconds=response_timeout_seconds,
            )
        except ProviderTransportTimeout as exc:
            return None, None, f"provider_{exc.stage}_timeout"
        except ProviderTransportFailure:
            return None, None, "provider_transport_failure"
        try:
            payload = json.loads(response.body)
        except (TypeError, ValueError):
            payload = None
        if response.status >= 400:
            return (
                response.status,
                payload,
                _ollama_failure_code(response.status, _ollama_error_text(response.body)),
            )
        return response.status, payload, None

    version_status, version_payload, version_error = get_json("version")
    provider_reachable = version_status is not None
    version = (
        str(version_payload.get("version"))[:80]
        if isinstance(version_payload, dict) and version_payload.get("version")
        else None
    )
    if not provider_reachable:
        return OllamaProbeResult(
            provider_reachable=False,
            model_available=False,
            warmup_success=False,
            ollama_version=None,
            capabilities=OllamaCapabilities(),
            selected_request_mode=None,
            sanitized_failure_code=version_error,
        )
    tags_status, tags_payload, tags_error = get_json("tags")
    names = (
        {
            str(item.get("name") or item.get("model"))
            for item in tags_payload.get("models", ())
            if isinstance(item, dict) and (item.get("name") or item.get("model"))
        }
        if isinstance(tags_payload, dict)
        else set()
    )
    model_available = tags_status is not None and tags_status < 400 and model in names
    if not model_available:
        return OllamaProbeResult(
            provider_reachable=True,
            model_available=False,
            warmup_success=False,
            ollama_version=version,
            capabilities=OllamaCapabilities(),
            selected_request_mode=None,
            sanitized_failure_code=tags_error or "ollama_model_not_found",
        )

    base_payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                # Name the exact reply. "Return only the requested synthetic JSON"
                # left the shape to the model, and a reasoning model answered a
                # yes/no capability question with 449 tokens -- roughly 18s each,
                # seven times over, on the single llama-server slot the user's turn
                # needs. Nothing here reads the content: every probe below asks only
                # whether the envelope came back decodable, and six tokens answer
                # that as well as four hundred do.
                "content": OLLAMA_PROBE_INSTRUCTION,
            },
            {"role": "user", "content": SYNTHETIC_PROBE_INPUT},
        ],
        "stream": False,
        # A backstop, not the mechanism: the instruction above is what makes the
        # reply short. The cap has to stay well clear of it, because a truncated
        # reply decodes as empty content -- which this probe would then record as
        # the *option* being unsupported rather than the answer being cut off.
        "options": {"temperature": 0, "num_predict": PROBE_NUM_PREDICT},
    }
    warmup_success, failure_code, failure_message, warmup_latency = _probe_chat(
        provider,
        base_payload,
        timeout_seconds=warmup_timeout_seconds,
    )
    if not warmup_success:
        return OllamaProbeResult(
            provider_reachable=True,
            model_available=True,
            warmup_success=False,
            ollama_version=version,
            capabilities=OllamaCapabilities(),
            selected_request_mode=None,
            sanitized_failure_code=failure_code,
            sanitized_error_message=failure_message,
            warmup_latency_ms=warmup_latency,
        )

    # A tiny schema can pass even when Ollama's grammar compiler rejects the
    # complete production schema. Probe the exact schema extraction will use.
    probe_schema = proposal_response_schema()

    def supports(
        update: dict[str, Any],
    ) -> tuple[bool, str | None, str | None]:
        payload = {**base_payload, **update}
        accepted, code, message, _latency = _probe_chat(
            provider,
            payload,
            timeout_seconds=response_timeout_seconds,
        )
        return accepted, code, message

    json_supported, json_code, json_message = supports({"format": "json"})
    schema_supported, schema_code, schema_message = supports({"format": probe_schema})
    think_supported, _think_code, _think_message = (
        supports({"format": "json", "think": False}) if json_supported else (False, None, None)
    )
    # ``supports`` merges at the top level, so an update carrying ``options``
    # replaces the base's wholesale -- each of these has to re-state the cap.
    seed_supported, _seed_code, _seed_message = supports(
        {"options": {"temperature": 0, "seed": 0, "num_predict": PROBE_NUM_PREDICT}}
    )
    num_predict_supported, _num_code, _num_message = supports(
        {"options": {"temperature": 0, "num_predict": PROBE_NUM_PREDICT}}
    )
    keep_alive_supported, _keep_code, _keep_message = supports({"keep_alive": "5m"})
    capabilities = OllamaCapabilities(
        schema_format_supported=schema_supported,
        json_format_supported=json_supported,
        think_field_supported=think_supported,
        seed_option_supported=seed_supported,
        num_predict_option_supported=num_predict_supported,
        keep_alive_supported=keep_alive_supported,
    )
    if requested is OllamaRequestMode.SCHEMA:
        selected = OllamaRequestMode.SCHEMA if schema_supported else None
        selection_error = None if selected else schema_code or "ollama_unsupported_format_schema"
        selection_message = None if selected else schema_message
    elif requested is OllamaRequestMode.JSON:
        selected = OllamaRequestMode.JSON if json_supported else None
        selection_error = None if selected else json_code or "ollama_json_mode_unsupported"
        selection_message = None if selected else json_message
    elif schema_supported:
        selected = OllamaRequestMode.SCHEMA
        selection_error = None
        selection_message = None
    elif json_supported:
        selected = OllamaRequestMode.JSON
        # A successful fallback remains successful, but retaining the safe
        # schema rejection explains why JSON mode was selected.
        selection_error = schema_code or "ollama_unsupported_format_schema"
        selection_message = schema_message
    else:
        selected = None
        selection_error = schema_code or json_code or "ollama_no_supported_structured_output_mode"
        selection_message = schema_message or json_message
    return OllamaProbeResult(
        provider_reachable=True,
        model_available=True,
        warmup_success=True,
        ollama_version=version,
        capabilities=capabilities,
        selected_request_mode=selected,
        sanitized_failure_code=selection_error,
        sanitized_error_message=selection_message,
        warmup_latency_ms=warmup_latency,
    )


def build_extraction_model_provider(
    provider: str | ExtractionProviderKind,
    endpoint: str,
    *,
    model: str,
    connect_timeout_seconds: int = 5,
    response_timeout_seconds: int | None = None,
    timeout_seconds: int | None = None,
    bearer_token: str | None = None,
    ollama_request_mode: OllamaRequestMode | str = OllamaRequestMode.SCHEMA,
    ollama_capabilities: OllamaCapabilities | None = None,
    transport: JsonHttpTransport | None = None,
    two_stage: bool = False,
) -> ExtractionModelProvider:
    try:
        kind = ExtractionProviderKind(provider)
    except ValueError:
        raise ValueError("unsupported_extraction_provider") from None
    common = {
        "model": model,
        "connect_timeout_seconds": connect_timeout_seconds,
        "response_timeout_seconds": response_timeout_seconds,
        "timeout_seconds": timeout_seconds,
        "bearer_token": bearer_token,
        "transport": transport,
    }
    if kind is ExtractionProviderKind.DIRECT_JSON:
        return DirectJsonExtractionProvider(endpoint, **common)
    if kind is ExtractionProviderKind.OLLAMA:
        provider_class = (
            TwoStageOllamaChatExtractionProvider if two_stage else OllamaChatExtractionProvider
        )
        return provider_class(
            endpoint,
            request_mode=ollama_request_mode,
            capabilities=ollama_capabilities,
            **common,
        )
    raise ValueError("unsupported_extraction_provider")
