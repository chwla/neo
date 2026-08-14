"""Test doubles for the collaborators the memory layer cannot run for real.

Everything else in this suite runs against the real thing — a real SQLite file,
the real migration ledger, the real mutation kernel.  Three collaborators cannot
be: the local extraction model (an Ollama process that may not be installed, and
whose output is by definition not reproducible), the embedding-backed semantic
duplicate finder (a vector model, same problem), and the HTTP socket underneath
the provider.

All are replaced here with scripted stand-ins rather than mocks.  A mock would
assert that a call happened; these produce *real values of the real contract
type*, so everything downstream — parsing, grounding, taxonomy, the write
kernel — runs exactly as it does in production.  The only thing faked is where
the bytes came from.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from app.services.memory.extraction import (
    ExtractionModelError,
    ExtractionModelResponse,
    FixtureExtractionModel,
    ProviderResponseMetadata,
)
from app.services.memory.extraction_contracts import ModelExtractionInput

# --------------------------------------------------------------------------
# INF-08 — a scripted extraction model
# --------------------------------------------------------------------------


def source_span(message: str, quoted: str, *, message_id: str = "m1") -> dict[str, Any]:
    """Build a span whose offsets genuinely select ``quoted`` inside ``message``.

    Grounding re-derives offsets from the quoted text and refuses anything it
    cannot find, so a helper that computed offsets by hand would produce spans
    that fail for a reason unrelated to what the test is checking.  Raising here
    turns a typo in a fixture into an immediate, obvious error instead of a
    confusing ``span_not_found`` three layers down.
    """

    start = message.find(quoted)
    if start < 0:
        raise AssertionError(f"fixture quote {quoted!r} is not present in {message!r}")
    return {
        "message_id": message_id,
        "start": start,
        "end": start + len(quoted),
        "quoted_text": quoted,
    }


def assertion(
    message: str,
    quoted: str,
    *,
    proposal_id: str = "p1",
    memory_type: str = "knowledge",
    typed_value: Any | None = None,
    display_hint: str | None = None,
    domain_hint: str | None = None,
    slot_hint: str | None = None,
    subject_hint: str = "user",
    durability: str = "durable",
    confidence: float = 0.95,
    sensitivity_hint: str = "normal",
    message_id: str = "m1",
    **extra: Any,
) -> dict[str, Any]:
    """One model assertion, defaulted to the shape that should be accepted.

    Defaults are the *passing* case on every axis a test is not interested in —
    first-person subject, durable, confident enough to skip review.  A test that
    wants a rejection then changes exactly one field, so the failure it asserts
    can only have come from that field.
    """

    return {
        "proposal_id": proposal_id,
        "source_spans": [source_span(message, quoted, message_id=message_id)],
        "subject_hint": subject_hint,
        "memory_type_hint": memory_type,
        "domain_hint": domain_hint,
        "slot_hint": slot_hint,
        "typed_value": quoted if typed_value is None else typed_value,
        "display_hint": display_hint or quoted,
        "durability": durability,
        "confidence": confidence,
        "sensitivity_hint": sensitivity_hint,
        **extra,
    }


def retraction(
    message: str,
    quoted: str,
    *,
    proposal_id: str = "r1",
    old_value_hint: str | None = None,
    confidence: float = 0.95,
    subject_hint: str = "user",
    message_id: str = "m1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "source_spans": [source_span(message, quoted, message_id=message_id)],
        "subject_hint": subject_hint,
        "old_value_hint": old_value_hint or quoted,
        "confidence": confidence,
        **extra,
    }


def model_output(
    *,
    assertions: list[dict[str, Any]] | None = None,
    retractions: list[dict[str, Any]] | None = None,
    exclusions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A complete model response envelope, exactly as the provider returns it."""

    return {
        "schema_version": 1,
        "assertions": assertions or [],
        "retractions": retractions or [],
        "exclusions": exclusions or [],
    }


def scripted_model(fixtures: dict[str, Any], **kwargs: Any) -> FixtureExtractionModel:
    """The app's own fixture provider, keyed by user message.

    ``FixtureExtractionModel`` ships in ``app.services.memory.extraction`` rather
    than in the tests, so the scripted path exercises the same provider contract
    the real one implements.  A value may be a response, an exception (to script
    a failure), or a sequence (to script a retry).
    """

    return FixtureExtractionModel(fixtures, **kwargs)


class RecordingModel:
    """A scripted model that also remembers what it was shown.

    Some properties are about what the model is *not* given — assistant text it
    must not be able to cite, a message the owner does not own.  Those need the
    input captured, which the fixture provider does not do.
    """

    provider_kind = "fixture"

    def __init__(self, response: Any, *, model_version: str = "recording-v1") -> None:
        self._response = response
        self.model_version = model_version
        self.prompt_version = "test-prompt-v1"
        self.call_count = 0
        self.inputs: list[ModelExtractionInput] = []

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        self.call_count += 1
        self.inputs.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return ExtractionModelResponse(
            raw_output=self._response,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            metadata=ProviderResponseMetadata(
                provider_kind="fixture",
                response_envelope_shape="fixture",
                content_present=True,
            ),
        )


class UnavailableModel:
    """A model that always fails, for every degradation path."""

    def __init__(self, code: str = "provider_unreachable", *, timeout: bool = False) -> None:
        self._code = code
        self._timeout = timeout
        self.call_count = 0

    def extract(self, request: ModelExtractionInput) -> ExtractionModelResponse:
        from app.services.memory.extraction import ExtractionModelTimeout

        self.call_count += 1
        error = ExtractionModelTimeout if self._timeout else ExtractionModelError
        raise error(
            self._code,
            metadata=ProviderResponseMetadata(
                provider_kind="fixture",
                response_envelope_shape="unavailable",
                timeout_stage="response" if self._timeout else None,
            ),
        )


# --------------------------------------------------------------------------
# INF-07 — a stand-in for the embedding-backed duplicate finder
# --------------------------------------------------------------------------


class StaticDuplicateFinder:
    """Answer "is this a restatement?" from a fixed script, not a vector model.

    The real finder embeds the candidate text and compares it against the
    embeddings of comparable records.  What the coordinator actually depends on
    is far narrower: a callable that returns the ``memory_id`` of a record this
    candidate restates, or ``None``.  Scripting that answer lets the duplicate
    *policy* be tested — which records are even eligible for comparison, what
    happens to the slot key afterwards — without a model in the loop.
    """

    def __init__(self, answer: UUID | None = None, *, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple[str, frozenset[UUID], float]] = []

    def __call__(
        self,
        display_text: str,
        candidates: frozenset[UUID],
        *,
        threshold: float,
    ) -> UUID | None:
        self.calls.append((display_text, candidates, threshold))
        if self.raises is not None:
            raise self.raises
        if self.answer is not None and self.answer in candidates:
            return self.answer
        return None


class ScoredDuplicateFinder:
    """A duplicate finder that actually applies the threshold it is handed.

    ``StaticDuplicateFinder`` answers from a script and ignores ``threshold``
    entirely.  That is the right shape for testing duplicate *policy* — which
    records are eligible for comparison, what happens to the slot afterwards —
    but it cannot exercise the boundary, because it never compares anything to
    the threshold.  Every test written against it would pass identically if the
    coordinator stopped passing the threshold through at all.

    This one scores each comparable record from a supplied map and returns the
    best match only when it reaches the threshold, using the same ``>=``
    comparison the real finder makes.  A record absent from the map scores zero,
    so a test only has to name the record it cares about.
    """

    def __init__(self, scores: dict[UUID, float] | None = None) -> None:
        self.scores = scores or {}
        self.calls: list[tuple[str, frozenset[UUID], float]] = []

    def __call__(
        self,
        display_text: str,
        candidates: frozenset[UUID],
        *,
        threshold: float,
    ) -> UUID | None:
        self.calls.append((display_text, candidates, threshold))
        best: tuple[UUID, float] | None = None
        for memory_id in candidates:
            score = float(self.scores.get(memory_id, 0.0))
            if best is None or score > best[1]:
                best = (memory_id, score)
        if best is not None and best[1] >= threshold:
            return best[0]
        return None


# --------------------------------------------------------------------------
# INF-07 — a fake embedding provider
# --------------------------------------------------------------------------


class FakeEmbeddingProvider:
    """A deterministic embedding provider with a fixed dimension.

    Real embeddings are a model call: slow, and different on every machine.  The
    vector index does not care what the numbers mean — it stores them, checks
    the dimension against the provider, and ranks by cosine similarity.  So the
    only properties a test needs are *deterministic* and *of the declared
    dimension*.

    Vectors are derived from a hash of the text, which gives two things worth
    having: the same text always embeds identically (so a re-index is a no-op),
    and different text embeds differently (so two memories don't collide).  For
    tests that need a specific geometry — orthogonal, identical, opposite —
    pass an explicit vector via ``script``.
    """

    def __init__(
        self,
        *,
        dimension: int = 8,
        provider_name: str = "fake",
        model_name: str = "fake-embed-v1",
        provider_version: str = "1",
        script: dict[str, list[float]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.dimension = dimension
        self.provider_name = provider_name
        self.model_name = model_name
        self.provider_version = provider_version
        self.script = script or {}
        self.raises = raises
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.raises is not None:
            raise self.raises
        if text in self.script:
            return list(self.script[text])
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(self.dimension)]

    def health(self) -> bool:
        """A plain ``bool``, because that is what the protocol declares.

        This returned a ``ProviderHealth`` model until the parallel session
        found the trap: ``ProviderHealth`` is a pydantic model with no
        ``__bool__``, so it is *always truthy*.  Consumers written against the
        real ``EmbeddingProvider`` protocol — which declares ``health() -> bool``
        — do ``if not provider.health()``, and against the old double that
        branch was unreachable even with ``raises`` set.

        A degradation test using it would still have gone green: ``embed()``
        raises on the next line and an outer handler catches it, so the right
        outcome arrives by the wrong path and the test proves nothing about the
        health check. Nothing in this suite was hitting it yet, which is exactly
        why it was worth fixing before something did.
        """

        return self.raises is None


# --------------------------------------------------------------------------
# A scripted HTTP transport, for the provider tests
# --------------------------------------------------------------------------


class FakeTransport:
    """Answer provider HTTP calls from a script keyed by endpoint suffix.

    ``JsonHttpTransport`` is a Protocol and every provider takes one as a
    constructor argument, so this substitutes at the socket and leaves the
    entire provider — payload construction, envelope decoding, error mapping,
    sanitisation — running for real.

    Keys are matched by endpoint suffix (``/api/chat``, ``/api/tags``) because
    the probe walks several endpoints on one host.  A value is either an
    ``(status, body)`` pair or an exception to raise, which is how the timeout
    and transport-failure paths get exercised without a real network.
    """

    def __init__(self, script: dict[str, Any], *, default: Any = None) -> None:
        self.script = script
        self.default = default
        self.requests: list[dict[str, Any]] = []

    def _resolve(self, endpoint: str) -> Any:
        for suffix, value in self.script.items():
            if endpoint.endswith(suffix):
                return value
        if self.default is None:
            raise AssertionError(f"no scripted response for {endpoint}")
        return self.default

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        body: bytes | None,
        headers: Any,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
    ):
        from app.services.memory.extraction import HttpTransportResponse

        self.requests.append(
            {
                "method": method,
                "endpoint": endpoint,
                "body": body,
                "headers": dict(headers),
                "connect_timeout_seconds": connect_timeout_seconds,
                "read_timeout_seconds": read_timeout_seconds,
            }
        )
        value = self._resolve(endpoint)
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        status, payload = value
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif not isinstance(payload, bytes):
            payload = json.dumps(payload).encode("utf-8")
        return HttpTransportResponse(status=status, body=payload)

    def last_json(self) -> dict[str, Any]:
        """The body of the most recent request, decoded."""

        return json.loads(self.requests[-1]["body"])


def ollama_chat_body(content: Any, **envelope: Any) -> dict[str, Any]:
    """Ollama's ``/api/chat`` envelope wrapping a model response."""

    return {
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": content if isinstance(content, str) else json.dumps(content),
        },
        "done": True,
        **envelope,
    }
