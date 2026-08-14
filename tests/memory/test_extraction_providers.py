"""Tier 2 — extraction model providers and transport (plan section EXT).

This layer talks to a local Ollama process over HTTP.  It is the part of the
memory system most likely to be broken by something outside the code: Ollama not
running, the model not pulled, a version whose grammar compiler rejects the
schema.  A deployment failure here already happened once — extraction was
pointed at a model that was never installed — so the mapping from each failure
shape to its diagnosis is worth pinning precisely.

Every test injects a scripted transport (``doubles.FakeTransport``).  That fakes
the socket and nothing else: payload construction, envelope decoding, error
classification and message sanitisation all run for real.  No test here needs
Ollama installed, which is the point — a suite that only runs on a machine with
the right model pulled is a suite that does not run.
"""

from __future__ import annotations

import json

import pytest

from app.services.memory.extraction import (
    MAX_PROVIDER_RESPONSE_BYTES,
    MAX_SANITIZED_ERROR_CHARS,
    PROMPT_VERSION,
    SYNTHETIC_PROBE_INPUT,
    DirectJsonExtractionProvider,
    ExtractionModelError,
    ExtractionModelTimeout,
    ExtractionProviderKind,
    OllamaCapabilities,
    OllamaChatExtractionProvider,
    OllamaRequestMode,
    ProviderTransportFailure,
    ProviderTransportTimeout,
    _ollama_failure_code,
    _safe_error_message,
    build_extraction_model_provider,
    probe_ollama_provider,
)
from app.services.memory.extraction_contracts import ExtractionMode, ModelExtractionInput
from tests.memory.doubles import (
    FakeTransport,
    assertion,
    model_output,
    ollama_chat_body,
    scripted_model,
)

ENDPOINT = "http://localhost:11434/api/chat"
MESSAGE = "I use Python for work."


def model_input(message: str = MESSAGE) -> ModelExtractionInput:
    return ModelExtractionInput(
        request_id="request-1",
        conversation_id="c1",
        session_id="s1",
        message_id="m1",
        user_message=message,
        explicit_memory_intent=False,
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        maximum_candidates=4,
    )


def ollama(transport: FakeTransport, **kwargs) -> OllamaChatExtractionProvider:
    kwargs.setdefault("request_mode", OllamaRequestMode.SCHEMA)
    return OllamaChatExtractionProvider(ENDPOINT, model="test-model", transport=transport, **kwargs)


def valid_output() -> dict:
    return model_output(assertions=[assertion(MESSAGE, "Python")])


class TestTheFixtureProvider:
    def test_the_scripted_response_comes_back(self) -> None:
        """EXT-01 — the double the rest of the suite depends on."""

        model = scripted_model({MESSAGE: valid_output()})
        response = model.extract(model_input())
        assert json.loads(json.dumps(response.raw_output))["schema_version"] == 1
        assert model.call_count == 1

    def test_an_unscripted_message_is_an_explicit_failure(self) -> None:
        """EXT-01b — a missing fixture must not look like a model refusal.

        Returning an empty response for an unscripted message would make a
        forgotten fixture indistinguishable from "the model found nothing",
        which is a genuine outcome the coordinator handles differently.
        """

        model = scripted_model({MESSAGE: valid_output()})
        with pytest.raises(ExtractionModelError, match="fixture_not_found"):
            model.extract(model_input("something else entirely"))

    def test_a_sequence_advances_between_calls(self) -> None:
        """EXT-01c — how the retry tests script "bad first, good second"."""

        model = scripted_model({MESSAGE: ["{oops", valid_output()]})
        with pytest.raises(Exception):  # noqa: B017 - parse failure shape is not the subject
            json.loads(model.extract(model_input()).raw_output)
        assert model.extract(model_input()).raw_output["schema_version"] == 1


class TestTransportFailures:
    @pytest.mark.parametrize("stage", ["connect", "read"])
    def test_a_timeout_carries_its_stage(self, stage: str) -> None:
        """EXT-03 / EXT-04 — which timeout it was determines what to do.

        A connect timeout means Ollama is not listening: nothing will fix that
        this turn.  A read timeout means it is thinking too slowly, which a
        smaller model or a longer budget would fix.  Collapsing them into one
        "timeout" loses the distinction that decides the remedy.
        """

        transport = FakeTransport({"/api/chat": ProviderTransportTimeout(stage)})
        with pytest.raises(ExtractionModelTimeout) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "model_timeout"
        assert caught.value.metadata.timeout_stage == stage
        assert caught.value.metadata.sanitized_failure_code == f"provider_{stage}_timeout"

    def test_a_transport_failure_is_not_a_bare_oserror(self) -> None:
        """EXT-05 — the caller catches extraction errors, not socket errors.

        The coordinator catches ``ExtractionModelError``.  A raw ``OSError``
        escaping from here would propagate past it and out through the chat
        turn, which is the one thing extraction must never do.
        """

        transport = FakeTransport({"/api/chat": ProviderTransportFailure("connection refused")})
        with pytest.raises(ExtractionModelError) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "model_transport_failure"
        assert not isinstance(caught.value, OSError)

    def test_an_oversized_response_is_refused_before_parsing(self) -> None:
        """EXT-02 — a bound that holds even if the provider misbehaves.

        Parsing first and checking the size afterwards would mean a hostile or
        broken endpoint could make the process allocate as much as it liked.
        """

        oversized = b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 10)
        transport = FakeTransport({"/api/chat": (200, oversized)})
        with pytest.raises(ExtractionModelError) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "model_output_too_large"
        assert caught.value.metadata.sanitized_failure_code == "provider_response_too_large"

    def test_an_oversized_content_field_is_also_refused(self) -> None:
        """EXT-02b — the same bound one layer in.

        A well-formed envelope can still carry an enormous ``content`` string,
        which the outer byte check does not see.
        """

        body = ollama_chat_body("x" * (MAX_PROVIDER_RESPONSE_BYTES + 10))
        transport = FakeTransport({"/api/chat": (200, body)})
        with pytest.raises(ExtractionModelError, match="model_output_too_large"):
            ollama(transport).extract(model_input())


class TestFailureCodeMapping:
    """Each failure gets its own name, because each has a different remedy."""

    @pytest.mark.parametrize(
        ("status", "error", "code"),
        [
            (404, "model 'llama3' not found", "ollama_model_not_found"),
            (400, "invalid model name", "ollama_invalid_model_name"),
            (400, "unknown field think", "ollama_unknown_field_think"),
            (400, "failed to parse grammar", "ollama_unsupported_format_schema"),
            (400, "format schema unsupported", "ollama_unsupported_format_schema"),
            (400, "invalid option seed", "ollama_invalid_options"),
            (400, "request size too large", "ollama_request_too_large"),
            (500, "out of memory", "ollama_insufficient_memory"),
            (500, "failed to load model", "ollama_model_load_failed"),
            (500, "something unexplained", "ollama_server_error"),
            (400, "something unexplained", "ollama_invalid_request"),
        ],
    )
    def test_each_error_body_maps_to_its_own_code(self, status: int, error: str, code: str) -> None:
        """EXT-08 / EXT-09"""

        assert _ollama_failure_code(status, error) == code

    def test_the_model_not_found_case_end_to_end(self) -> None:
        """EXT-09b — the failure that actually happened in deployment.

        Extraction was configured against a model that was never pulled. The
        symptom was silence; the cause is one specific 404 body. It needs to be
        distinguishable from every other 404 so the fix ("run ollama pull") is
        obvious from the diagnostic alone.
        """

        transport = FakeTransport({"/api/chat": (404, {"error": "model 'test-model' not found"})})
        with pytest.raises(ExtractionModelError) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "ollama_model_not_found"
        assert caught.value.metadata.http_status == 404

    def test_an_error_inside_a_200_envelope_is_still_an_error(self) -> None:
        """EXT-09c — Ollama reports some failures with a 200 status.

        Trusting the status code alone would treat this as a successful call
        with unparseable content, and report the wrong cause.
        """

        body = {"error": "model 'test-model' not found"}
        transport = FakeTransport({"/api/chat": (200, body)})
        with pytest.raises(ExtractionModelError, match="ollama_model_not_found"):
            ollama(transport).extract(model_input())


class TestErrorSanitisation:
    """Provider error text is untrusted and may quote the user's message back."""

    def test_a_long_message_is_dropped_entirely(self) -> None:
        """EXT-06 — truncating could still leak; dropping cannot.

        A truncated error is a partial leak with a false air of completeness.
        Over the limit, the message is discarded and only the code survives.
        """

        assert _safe_error_message("x" * (MAX_SANITIZED_ERROR_CHARS + 1)) is None
        assert _safe_error_message("x" * 10) == "x" * 10

    def test_the_user_message_is_never_echoed_back(self) -> None:
        """EXT-07 — the property this whole function exists for.

        Providers routinely quote the offending input in their error text. That
        text goes into diagnostics, which go into logs. Any error containing the
        user's own words is dropped rather than sanitised.
        """

        assert (
            _safe_error_message(
                "failed on input: I use Python for work.",
                forbidden_texts=(MESSAGE,),
            )
            is None
        )

    @pytest.mark.parametrize(
        "text",
        [
            "your password is hunter2",
            "invalid api key supplied",
            "access token rejected",
            "secret rotated",
        ],
    )
    def test_credential_shaped_errors_are_dropped(self, text: str) -> None:
        """EXT-06b — a second net, for text no forbidden-list would catch."""

        assert _safe_error_message(text) is None

    def test_control_characters_are_refused(self) -> None:
        """EXT-06c — diagnostics end up in logs and terminals.

        An error carrying escape sequences could rewrite what a reader sees.
        """

        assert _safe_error_message("bad \x1b[31mrequest") is None

    def test_a_short_forbidden_fragment_does_not_suppress_everything(self) -> None:
        """EXT-07b — the guard is scoped so it stays useful.

        Matching on fragments under four characters would let a message
        containing "I" or "a" suppress every error the provider ever returns,
        leaving nothing to diagnose with.
        """

        assert _safe_error_message("invalid request", forbidden_texts=("I",)) == "invalid request"

    def test_a_sanitised_message_reaches_the_metadata(self) -> None:
        """EXT-06d — a safe message is kept, not discarded defensively."""

        transport = FakeTransport({"/api/chat": (400, {"error": "invalid request"})})
        provider = ollama(transport)
        with pytest.raises(ExtractionModelError):
            provider.extract(model_input())
        assert provider.last_sanitized_error_message == "invalid request"

    def test_an_unsafe_message_leaves_only_the_code(self) -> None:
        """EXT-07c — the diagnosis survives even when the text cannot."""

        transport = FakeTransport({"/api/chat": (400, {"error": f"failed on input: {MESSAGE}"})})
        provider = ollama(transport)
        with pytest.raises(ExtractionModelError) as caught:
            provider.extract(model_input())
        assert provider.last_sanitized_error_message is None
        assert caught.value.code
        assert MESSAGE not in str(caught.value.metadata)


class TestRequestShape:
    def test_the_direct_provider_posts_the_documented_body(self) -> None:
        """EXT-10"""

        transport = FakeTransport({"/api/chat": (200, valid_output())})
        provider = DirectJsonExtractionProvider(ENDPOINT, model="test-model", transport=transport)
        provider.extract(model_input())
        body = transport.last_json()
        assert body["model"] == "test-model"
        assert body["stream"] is False
        assert body["temperature"] == 0
        assert body["response_format"]["type"] == "json_schema"

    def test_schema_mode_sends_the_json_schema(self) -> None:
        """EXT-12 — the strongest constraint the provider can apply.

        In schema mode Ollama constrains generation to the grammar, so
        malformed output is prevented rather than rejected afterwards.
        """

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        ollama(transport, request_mode=OllamaRequestMode.SCHEMA).extract(model_input())
        body = transport.last_json()
        assert isinstance(body["format"], dict)
        assert "properties" in body["format"]

    def test_json_mode_sends_the_literal_format_json(self) -> None:
        """EXT-11 — the fallback for versions whose grammar compiler refuses."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        ollama(transport, request_mode=OllamaRequestMode.JSON).extract(model_input())
        assert transport.last_json()["format"] == "json"

    def test_json_mode_carries_the_written_contract(self) -> None:
        """EXT-11b — with no grammar to enforce it, the rules go in the prompt."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        ollama(transport, request_mode=OllamaRequestMode.JSON).extract(model_input())
        system = transport.last_json()["messages"][0]["content"]
        assert "JSON mode response contract" in system

    def test_temperature_is_always_zero(self) -> None:
        """EXT-11c — extraction is not a creative task.

        Any temperature above zero means the same message can yield different
        memories on different turns, which makes the store unreproducible.
        """

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        ollama(transport).extract(model_input())
        assert transport.last_json()["options"]["temperature"] == 0

    def test_unsupported_options_are_omitted_not_sent_and_ignored(self) -> None:
        """EXT-13b — the capability probe exists to keep requests minimal.

        Sending an option an older Ollama does not know is not harmless; some
        versions reject the whole request. Every optional field is gated.
        """

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        provider = ollama(
            transport,
            request_mode=OllamaRequestMode.JSON,
            capabilities=OllamaCapabilities(json_format_supported=True),
        )
        provider.extract(model_input())
        body = transport.last_json()
        assert "think" not in body
        assert "keep_alive" not in body
        assert "seed" not in body["options"]
        assert "num_predict" not in body["options"]

    def test_supported_options_are_sent(self) -> None:
        """EXT-13c — the other half, so the gating is provably two-way."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        provider = ollama(
            transport,
            request_mode=OllamaRequestMode.JSON,
            capabilities=OllamaCapabilities(
                json_format_supported=True,
                think_field_supported=True,
                seed_option_supported=True,
                num_predict_option_supported=True,
                keep_alive_supported=True,
            ),
        )
        provider.extract(model_input())
        body = transport.last_json()
        assert body["think"] is False
        assert body["keep_alive"] == "10m"
        assert body["options"]["seed"] == 0
        assert body["options"]["num_predict"] == 2048

    def test_the_connect_and_read_timeouts_are_passed_separately(self) -> None:
        """EXT-03b — two budgets, because they mean different things."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        ollama(transport, connect_timeout_seconds=3, response_timeout_seconds=90).extract(
            model_input()
        )
        assert transport.requests[-1]["connect_timeout_seconds"] == 3
        assert transport.requests[-1]["read_timeout_seconds"] == 90


class TestResponseDecoding:
    def test_a_non_json_body_gets_a_named_failure(self) -> None:
        """EXT-14 — a `JSONDecodeError` escaping here would say nothing useful."""

        transport = FakeTransport({"/api/chat": (200, b"I'm afraid I can't do that")})
        with pytest.raises(ExtractionModelError) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "malformed_provider_envelope"

    def test_a_streamed_response_is_recognised_as_such(self) -> None:
        """EXT-14b — the specific misconfiguration, named.

        Ollama returns newline-delimited JSON when streaming. Every line parses
        on its own, so the failure is only diagnosable if the shape is checked.
        The fix is `"stream": false`, and the code has to say so.
        """

        transport = FakeTransport({"/api/chat": (200, b'{"a":1}\n{"b":2}\n')})
        with pytest.raises(ExtractionModelError) as caught:
            ollama(transport).extract(model_input())
        assert caught.value.code == "streamed_response_rejected"
        assert caught.value.metadata.response_envelope_shape == "ollama_ndjson_stream"

    def test_a_partial_chat_response_is_rejected(self) -> None:
        """EXT-14c — `done: false` means more is coming; this is not the answer."""

        transport = FakeTransport(
            {"/api/chat": (200, ollama_chat_body(valid_output(), done=False))}
        )
        with pytest.raises(ExtractionModelError, match="streamed_response_rejected"):
            ollama(transport).extract(model_input())

    @pytest.mark.parametrize(
        "body",
        [
            [1, 2, 3],
            {"message": "not a dict"},
            {"message": {"role": "user", "content": "{}"}},
            {"message": {"role": "assistant", "content": 42}},
        ],
    )
    def test_an_unrecognised_envelope_is_refused(self, body) -> None:
        """EXT-14d — including one that would let the provider impersonate the user.

        The third case is a response whose message claims `role: user`. Accepting
        it would let provider-authored text enter as though the user had typed
        it, which grounding is built to prevent.
        """

        transport = FakeTransport({"/api/chat": (200, body)})
        with pytest.raises(ExtractionModelError, match="unknown_response_envelope"):
            ollama(transport).extract(model_input())

    def test_empty_content_is_its_own_failure(self) -> None:
        """EXT-14e — "the model returned nothing" is not "the model found nothing"."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body("   "))})
        with pytest.raises(ExtractionModelError, match="empty_model_content"):
            ollama(transport).extract(model_input())

    def test_a_valid_response_is_returned_with_its_metadata(self) -> None:
        """EXT-22 — the success path, and what it records."""

        transport = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        response = ollama(transport).extract(model_input())
        assert json.loads(response.raw_output)["schema_version"] == 1
        assert response.prompt_version == PROMPT_VERSION
        metadata = response.metadata
        assert metadata.provider_kind == ExtractionProviderKind.OLLAMA.value
        assert metadata.http_status == 200
        assert metadata.content_present is True
        assert metadata.content_byte_length > 0
        assert metadata.response_content_hash

    def test_the_reported_model_comes_from_the_response(self) -> None:
        """EXT-22b — so a server silently substituting a model is visible."""

        body = ollama_chat_body(valid_output(), model="some-other-model")
        transport = FakeTransport({"/api/chat": (200, body)})
        response = ollama(transport).extract(model_input())
        assert response.model_version == "some-other-model"

    def test_the_content_hash_covers_the_content_not_the_envelope(self) -> None:
        """EXT-22c — two identical answers hash alike whatever wraps them."""

        first = FakeTransport({"/api/chat": (200, ollama_chat_body(valid_output()))})
        second = FakeTransport(
            {"/api/chat": (200, ollama_chat_body(valid_output(), total_duration=999))}
        )
        assert (
            ollama(first).extract(model_input()).metadata.response_content_hash
            == ollama(second).extract(model_input()).metadata.response_content_hash
        )


class TestTheCapabilityProbe:
    def _script(self, *, chat=None, tags=None, version=None) -> FakeTransport:
        return FakeTransport(
            {
                "/api/version": version or (200, {"version": "0.5.0"}),
                "/api/tags": tags or (200, {"models": [{"name": "test-model"}]}),
                "/api/chat": chat or (200, ollama_chat_body("{}")),
            }
        )

    def test_a_healthy_provider_reports_its_capabilities(self) -> None:
        """EXT-17"""

        result = probe_ollama_provider(ENDPOINT, model="test-model", transport=self._script())
        assert result.provider_reachable is True
        assert result.model_available is True
        assert result.warmup_success is True
        assert result.ollama_version == "0.5.0"
        assert result.successful is True

    def test_an_unreachable_provider_degrades_without_raising(self) -> None:
        """EXT-18 — the probe runs at startup; it must not stop the app booting."""

        transport = FakeTransport({"/api/version": ProviderTransportFailure("refused")})
        result = probe_ollama_provider(ENDPOINT, model="test-model", transport=transport)
        assert result.provider_reachable is False
        assert result.successful is False
        assert result.sanitized_failure_code == "provider_transport_failure"

    def test_a_missing_model_is_reported_as_such(self) -> None:
        """EXT-18b — the deployment failure, caught before the first chat turn.

        `/api/tags` lists what is pulled. If the configured model is not in it,
        every extraction will fail, and saying so at startup is far better than
        discovering it one silent turn at a time.
        """

        transport = self._script(tags=(200, {"models": [{"name": "some-other-model"}]}))
        result = probe_ollama_provider(ENDPOINT, model="test-model", transport=transport)
        assert result.provider_reachable is True
        assert result.model_available is False
        assert result.sanitized_failure_code == "ollama_model_not_found"
        assert result.selected_request_mode is None

    def test_a_failing_warmup_stops_the_probe(self) -> None:
        """EXT-18c — a model that is pulled but cannot load is not usable."""

        transport = self._script(chat=(500, {"error": "failed to load model"}))
        result = probe_ollama_provider(ENDPOINT, model="test-model", transport=transport)
        assert result.model_available is True
        assert result.warmup_success is False
        assert result.sanitized_failure_code == "ollama_model_load_failed"

    def test_auto_mode_selects_schema_when_it_is_supported(self) -> None:
        """EXT-13 — prefer the strongest constraint available."""

        result = probe_ollama_provider(
            ENDPOINT,
            model="test-model",
            requested_mode=OllamaRequestMode.AUTO,
            transport=self._script(),
        )
        assert result.selected_request_mode is OllamaRequestMode.SCHEMA

    def test_the_probe_never_sends_the_users_text(self) -> None:
        """EXT-19 — the probe runs at startup, before any user has typed anything.

        It uses a synthetic constant, so no real message can leak into a warmup
        call, and a candidate can never be built from probe output.
        """

        transport = self._script()
        probe_ollama_provider(ENDPOINT, model="test-model", transport=transport)
        chat_bodies = [
            item["body"].decode()
            for item in transport.requests
            if item["endpoint"].endswith("/api/chat")
        ]
        assert chat_bodies
        assert all(SYNTHETIC_PROBE_INPUT in body for body in chat_bodies)
        assert not any(MESSAGE in body for body in chat_bodies)

    def test_the_probe_derives_its_endpoints_from_the_chat_url(self) -> None:
        """EXT-17b — one configured URL, not three."""

        transport = self._script()
        probe_ollama_provider(ENDPOINT, model="test-model", transport=transport)
        endpoints = {item["endpoint"] for item in transport.requests}
        assert "http://localhost:11434/api/version" in endpoints
        assert "http://localhost:11434/api/tags" in endpoints


class TestTheProviderFactory:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("direct_json", DirectJsonExtractionProvider),
            ("ollama", OllamaChatExtractionProvider),
        ],
    )
    def test_each_provider_name_builds_its_provider(self, name: str, expected: type) -> None:
        """EXT-20"""

        built = build_extraction_model_provider(name, ENDPOINT, model="test-model")
        assert isinstance(built, expected)

    def test_an_unknown_provider_name_is_refused(self) -> None:
        """EXT-21 — a typo in configuration should fail loudly at startup."""

        with pytest.raises(ValueError, match="unsupported_extraction_provider"):
            build_extraction_model_provider("gpt-9", ENDPOINT, model="test-model")

    def test_the_fixture_kind_is_not_buildable_from_configuration(self) -> None:
        """EXT-20b — a test double must not be reachable from a config file.

        `fixture` is a valid `ExtractionProviderKind`, so it passes the enum
        conversion and only the explicit final `raise` stops it. Without that,
        setting `memory_extraction_provider=fixture` would build a provider that
        silently answers from an empty script.
        """

        with pytest.raises(ValueError, match="unsupported_extraction_provider"):
            build_extraction_model_provider("fixture", ENDPOINT, model="test-model")

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"endpoint": "", "model": "m"}, "extraction_endpoint_required"),
            ({"endpoint": ENDPOINT, "model": "  "}, "extraction_model_required"),
        ],
    )
    def test_blank_configuration_is_refused(self, kwargs: dict, message: str) -> None:
        """EXT-21b — an empty endpoint would otherwise fail per-turn, not at boot."""

        with pytest.raises(ValueError, match=message):
            DirectJsonExtractionProvider(kwargs["endpoint"], model=kwargs["model"])

    @pytest.mark.parametrize(
        ("connect", "response", "message"),
        [
            (0, 120, "extraction_connect_timeout_out_of_range"),
            (61, 120, "extraction_connect_timeout_out_of_range"),
            (5, 601, "extraction_response_timeout_out_of_range"),
            (5, -1, "extraction_response_timeout_out_of_range"),
        ],
    )
    def test_timeouts_outside_the_allowed_range_are_refused(
        self, connect: int, response: int, message: str
    ) -> None:
        """EXT-21c — an unbounded read timeout would hang a background worker."""

        with pytest.raises(ValueError, match=message):
            DirectJsonExtractionProvider(
                ENDPOINT,
                model="test-model",
                connect_timeout_seconds=connect,
                response_timeout_seconds=response,
            )

    def test_a_zero_response_timeout_is_refused_like_every_other_bad_value(self) -> None:
        """EXT-21d — fixed. Zero was the one out-of-range value that did not raise.

        The resolution used ``response_timeout_seconds or timeout_seconds or
        120``, and zero is falsy, so an explicit 0 was replaced by the default
        before the 1..600 range check ever saw it. Every other out-of-range
        value, including -1, was caught.

        Reachable from configuration: ``MemorySettings.__post_init__`` validates
        the input-char limit and the three recall limits but neither extraction
        timeout, and ``factory.build_memory_runtime`` passes the setting
        straight into the provider. So 0 — the natural way to write "no limit" —
        quietly meant 120 seconds instead of the configuration error the guard
        was written to produce.

        Now resolved with explicit ``is None`` checks, which distinguish "not
        supplied" from "supplied as zero".
        """

        with pytest.raises(ValueError, match="extraction_response_timeout_out_of_range"):
            DirectJsonExtractionProvider(ENDPOINT, model="test-model", response_timeout_seconds=0)

    def test_an_unset_timeout_still_falls_back_to_the_default(self) -> None:
        """EXT-21e — the other half, so the fix did not break the fallback.

        Distinguishing zero from unset is only correct if unset still works:
        omitting both arguments must still yield 120, and the legacy
        ``timeout_seconds`` alias must still be honoured.
        """

        default = DirectJsonExtractionProvider(ENDPOINT, model="test-model")
        assert default.response_timeout_seconds == 120

        legacy = DirectJsonExtractionProvider(ENDPOINT, model="test-model", timeout_seconds=30)
        assert legacy.response_timeout_seconds == 30

    def test_auto_mode_cannot_be_used_without_probing(self) -> None:
        """EXT-13d — `auto` is a request to decide, not a mode to send.

        Nothing in a request payload corresponds to "auto"; it only means "ask
        the probe". Constructing a provider with it would otherwise produce a
        provider with no resolved format at all.
        """

        with pytest.raises(ValueError, match="ollama_auto_mode_requires_capability_probe"):
            OllamaChatExtractionProvider(
                ENDPOINT, model="test-model", request_mode=OllamaRequestMode.AUTO
            )

    def test_an_unknown_request_mode_is_refused(self) -> None:
        """EXT-13e"""

        with pytest.raises(ValueError, match="unsupported_ollama_request_mode"):
            OllamaChatExtractionProvider(ENDPOINT, model="test-model", request_mode="turbo")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
