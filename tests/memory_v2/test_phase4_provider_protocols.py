from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import scripts.manual_memory_v2_phase4 as manual_phase4
from app.services.memory_v2.extraction import (
    SYNTHETIC_PROBE_INPUT,
    DirectJsonExtractionProvider,
    ExtractionModelError,
    HttpTransportResponse,
    OllamaCapabilities,
    OllamaChatExtractionProvider,
    OllamaRequestMode,
    ProviderTransportTimeout,
    StdlibJsonHttpTransport,
    build_extraction_model_provider,
    probe_ollama_provider,
)
from app.services.memory_v2.extraction_contracts import (
    ExtractionMode,
    ExtractionStatus,
    ModelExtractionInput,
)
from app.services.memory_v2.model_schema import parse_model_output
from tests.memory_v2.phase4_helpers import (
    extraction_input,
    phase4_harness,
    run_text,
    sql_state,
)


@contextmanager
def _transport(monkeypatch, body: bytes | str | dict, *, status: int = 200):
    if isinstance(body, dict):
        encoded = json.dumps(body).encode()
    elif isinstance(body, str):
        encoded = body.encode()
    else:
        encoded = body
    requests: list[dict] = []

    def _request(
        _self,
        method,
        endpoint,
        *,
        body,
        headers,
        connect_timeout_seconds,
        read_timeout_seconds,
    ):
        assert method == "POST"
        assert endpoint
        assert headers["Content-Type"] == "application/json"
        assert connect_timeout_seconds > 0
        assert read_timeout_seconds > 0
        requests.append(json.loads(body))
        return HttpTransportResponse(status=status, body=encoded)

    monkeypatch.setattr(StdlibJsonHttpTransport, "request", _request)
    try:
        yield requests, "http://provider.test/extract"
    finally:
        pass


def _schema(*assertions, retractions=(), exclusions=()) -> dict:
    return {
        "schema_version": 1,
        "assertions": list(assertions),
        "retractions": list(retractions),
        "exclusions": list(exclusions),
    }


def _assertion(text: str, value: str, message_id: str, *, sensitive=False) -> dict:
    start = text.index(value)
    return {
        "proposal_id": "provider-proposal",
        "source_spans": [
            {
                "message_id": message_id,
                "start": start,
                "end": start + len(value),
                "quoted_text": value,
            }
        ],
        "subject_hint": "user",
        "memory_type_hint": "knowledge",
        "domain_hint": "software_development",
        "typed_value": value,
        "display_hint": value,
        "durability": "durable",
        "confidence": 0.96,
        "sensitivity_hint": "sensitive" if sensitive else "normal",
    }


def _ollama_envelope(content: str, *, done=True) -> dict:
    return {
        "model": "phase4-test-model",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": done,
        "done_reason": "stop",
    }


def _provider_result(monkeypatch, tmp_path, body, *, status=200, text="I use Python for work."):
    with _transport(monkeypatch, body, status=status) as (requests, endpoint):
        provider = OllamaChatExtractionProvider(
            endpoint,
            model="phase4-test-model",
            timeout_seconds=2,
        )
        harness, extraction, diagnostics = phase4_harness(tmp_path, model=provider)
        result = run_text(
            extraction,
            harness,
            text,
            message_id="provider-input",
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
        )
        return result, diagnostics.snapshot()[-1], requests, provider.call_count


def test_valid_ollama_chat_envelope_uses_nested_assistant_content_and_schema_mode(
    monkeypatch,
    tmp_path,
) -> None:
    text = "I use Python for work."
    schema = _schema(_assertion(text, "I use Python", "provider-input"))
    result, diagnostic, requests, calls = _provider_result(
        monkeypatch, tmp_path, _ollama_envelope(json.dumps(schema)), text=text
    )
    assert result.status is ExtractionStatus.APPLIED
    assert calls == 1
    assert diagnostic.provider_kind == "ollama"
    assert diagnostic.http_status == 200
    assert diagnostic.response_envelope_shape == "ollama_chat_v1"
    assert diagnostic.json_parse_result == "parsed"
    assert diagnostic.schema_validation_result == "valid"
    payload = requests[0]
    assert payload["stream"] is False
    assert "think" not in payload
    assert payload["options"]["temperature"] == 0
    assert "seed" not in payload["options"]
    assert payload["options"]["num_predict"] == 2048
    assert isinstance(payload["format"], dict)
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert "owner_id" not in payload["messages"][1]["content"]
    assert "canonical memory IDs" in payload["messages"][0]["content"]


def test_valid_direct_json_provider_response_is_the_schema_body(monkeypatch, tmp_path) -> None:
    text = "I use Python for work."
    schema = _schema(_assertion(text, "I use Python", "direct-input"))
    with _transport(monkeypatch, schema) as (requests, endpoint):
        provider = DirectJsonExtractionProvider(
            endpoint, model="direct-test-model", timeout_seconds=2
        )
        harness, _extraction, _diagnostics = phase4_harness(tmp_path)
        request, _context = extraction_input(
            harness,
            text,
            message_id="direct-input",
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
        )
        response = provider.extract(ModelExtractionInput.from_trusted_request(request))
        parsed = parse_model_output(response.raw_output)
        assert len(parsed.assertions) == 1
        assert response.metadata.response_envelope_shape == "direct_schema_body_v1"
        assert requests[0]["stream"] is False


def test_ollama_supports_one_exact_json_fence(monkeypatch, tmp_path) -> None:
    text = "I use Python for work."
    schema = _schema(_assertion(text, "I use Python", "provider-input"))
    content = f"```json\n{json.dumps(schema)}\n```"
    result, diagnostic, _requests, _calls = _provider_result(
        monkeypatch, tmp_path, _ollama_envelope(content), text=text
    )
    assert result.status is ExtractionStatus.APPLIED
    assert diagnostic.schema_validation_result == "valid"


@pytest.mark.parametrize(
    ("content", "reason", "json_result"),
    [
        ("Here is the JSON: {}", "malformed_model_json", "invalid"),
        ("not-json", "malformed_model_json", "invalid"),
        (
            json.dumps({"schema_version": 1, "assertions": [], "unknown": True}),
            "invalid_model_schema",
            "parsed",
        ),
    ],
)
def test_ollama_rejects_prose_invalid_json_and_invalid_schema(
    monkeypatch, tmp_path, content, reason, json_result
) -> None:
    result, diagnostic, _requests, calls = _provider_result(
        monkeypatch, tmp_path, _ollama_envelope(content)
    )
    assert result.status is ExtractionStatus.FAILED
    assert result.diagnostic.reason_codes == (reason,)
    assert diagnostic.json_parse_result == json_result
    assert diagnostic.schema_validation_result == (
        "invalid" if reason == "invalid_model_schema" else "not_attempted"
    )
    assert calls == 2
    assert result.model_summary.raw_output_hash == hashlib.sha256(content.encode()).hexdigest()
    assert diagnostic.response_content_hash == result.model_summary.raw_output_hash
    if reason == "invalid_model_schema":
        assert "schema_unknown_field" in diagnostic.schema_error_codes


def test_empty_content_and_unknown_outer_envelope_fail_closed(monkeypatch, tmp_path) -> None:
    empty, empty_diagnostic, _requests, _calls = _provider_result(
        monkeypatch, tmp_path / "empty", _ollama_envelope("   ")
    )
    unknown, unknown_diagnostic, _requests, _calls = _provider_result(
        monkeypatch,
        tmp_path / "unknown",
        {"model": "phase4-test-model", "output": {}},
    )
    assert empty.diagnostic.reason_codes == ("empty_model_content",)
    assert empty_diagnostic.content_present is True
    assert unknown.diagnostic.reason_codes == ("unknown_response_envelope",)
    assert unknown_diagnostic.response_envelope_shape == "unknown_response_envelope"


def test_truncated_outer_envelope_and_outer_error_fail_closed(monkeypatch, tmp_path) -> None:
    truncated, truncated_diagnostic, _requests, _calls = _provider_result(
        monkeypatch,
        tmp_path / "truncated",
        b'{"message":{"role":"assistant","content":"unfinished"',
    )
    outer_error, error_diagnostic, _requests, _calls = _provider_result(
        monkeypatch,
        tmp_path / "outer-error",
        {"error": "provider rejected request"},
    )
    assert truncated.diagnostic.reason_codes == ("malformed_provider_envelope",)
    assert truncated_diagnostic.json_parse_result == "not_attempted"
    assert outer_error.diagnostic.reason_codes == ("ollama_invalid_request",)
    assert error_diagnostic.response_envelope_shape == "ollama_error_v1"


@pytest.mark.parametrize("status", [400, 404, 500])
def test_http_errors_are_typed_without_parsing_partial_content(
    monkeypatch, tmp_path, status
) -> None:
    result, diagnostic, _requests, calls = _provider_result(
        monkeypatch, tmp_path, {"error": "request rejected"}, status=status
    )
    assert result.status is ExtractionStatus.FAILED
    expected = "ollama_server_error" if status == 500 else "ollama_invalid_request"
    assert result.diagnostic.reason_codes == (expected,)
    assert diagnostic.sanitized_provider_error_code == expected
    assert diagnostic.http_status == status
    assert diagnostic.response_envelope_shape == "ollama_error_v1"
    assert diagnostic.json_parse_result == "not_attempted"
    assert calls == 1


def test_model_not_found_response_has_stable_code(monkeypatch, tmp_path) -> None:
    result, diagnostic, _requests, _calls = _provider_result(
        monkeypatch,
        tmp_path,
        {"error": "model 'missing-test-model' not found"},
        status=404,
    )
    assert result.diagnostic.reason_codes == ("ollama_model_not_found",)
    assert diagnostic.sanitized_provider_error_code == "ollama_model_not_found"
    assert diagnostic.http_status == 404


def test_timeout_is_typed(monkeypatch, tmp_path) -> None:
    def _timeout(*_args, **_kwargs):
        raise ProviderTransportTimeout("read")

    monkeypatch.setattr(StdlibJsonHttpTransport, "request", _timeout)
    provider = OllamaChatExtractionProvider(
        "http://127.0.0.1:1/api/chat",
        model="phase4-test-model",
        timeout_seconds=1,
    )
    harness, extraction, diagnostics = phase4_harness(tmp_path, model=provider)
    result = run_text(
        extraction,
        harness,
        "I use Python for work.",
        message_id="timeout-provider",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.diagnostic.reason_codes == ("model_timeout",)
    diagnostic = diagnostics.snapshot()[-1]
    assert diagnostic.provider_kind == "ollama"
    assert diagnostic.sanitized_provider_error_code == "provider_read_timeout"
    assert diagnostic.provider_timeout_stage == "read"


def test_sensitive_provider_response_has_no_diagnostic_or_artifact_leakage(
    monkeypatch, tmp_path
) -> None:
    sentinel = "P4-LIVE-SENSITIVE-88A7D41"
    text = f"My diagnosis is {sentinel}."
    value = f"diagnosis is {sentinel}"
    schema = _schema(_assertion(text, value, "provider-input", sensitive=True))
    result, diagnostic, _requests, _calls = _provider_result(
        monkeypatch, tmp_path, _ollama_envelope(json.dumps(schema)), text=text
    )
    # Re-run with explicit authorization because sensitive memory is opt-in only.
    with _transport(monkeypatch, _ollama_envelope(json.dumps(schema))) as (
        _requests,
        endpoint,
    ):
        provider = OllamaChatExtractionProvider(
            endpoint, model="phase4-test-model", timeout_seconds=2
        )
        harness, extraction, diagnostics = phase4_harness(tmp_path / "explicit", model=provider)
        explicit = run_text(
            extraction,
            harness,
            text,
            message_id="provider-input",
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
            explicit=True,
        )
        rendered = json.dumps(explicit.model_dump(mode="json"), default=str)
        diagnostic_rendered = json.dumps(
            diagnostics.snapshot()[-1].model_dump(mode="json"), default=str
        )
        assert explicit.status is ExtractionStatus.APPLIED
        assert explicit.model_summary.raw_output_hash is None
        assert diagnostics.snapshot()[-1].response_content_hash is None
        assert sentinel not in rendered
        assert sentinel not in diagnostic_rendered
        assert sentinel.encode() not in harness.database_path.read_bytes()
    assert result.status is ExtractionStatus.REJECTED
    assert diagnostic.response_content_hash is None


def test_prohibited_input_causes_zero_provider_call(monkeypatch, tmp_path) -> None:
    with _transport(monkeypatch, _ollama_envelope(json.dumps(_schema()))) as (
        _requests,
        endpoint,
    ):
        provider = OllamaChatExtractionProvider(
            endpoint, model="phase4-test-model", timeout_seconds=2
        )
        harness, extraction, _diagnostics = phase4_harness(tmp_path, model=provider)
        result = run_text(
            extraction,
            harness,
            "My password is P4-PROHIBITED-PROVIDER-4A1.",
            message_id="prohibited-provider",
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
            explicit=True,
        )
        assert result.status is ExtractionStatus.REJECTED
        assert provider.call_count == 0
        assert sql_state(harness.database_path)["candidates"] == []


def test_provider_selection_is_explicit_and_fail_closed() -> None:
    assert isinstance(
        build_extraction_model_provider(
            "ollama",
            "http://127.0.0.1:11434/api/chat",
            model="test",
            timeout_seconds=2,
        ),
        OllamaChatExtractionProvider,
    )
    assert isinstance(
        build_extraction_model_provider(
            "direct_json",
            "http://127.0.0.1:9000/extract",
            model="test",
            timeout_seconds=2,
        ),
        DirectJsonExtractionProvider,
    )
    with pytest.raises(ValueError, match="unsupported_extraction_provider"):
        build_extraction_model_provider(
            "auto",
            "http://127.0.0.1:11434/api/chat",
            model="test",
            timeout_seconds=2,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"message":{"role":"assistant","content":"{}"},"done":false}',
        (
            b'{"message":{"role":"assistant","content":"{"},"done":false}\n'
            b'{"message":{"role":"assistant","content":"}"},"done":true}\n'
        ),
    ],
)
def test_streamed_or_partial_ollama_output_is_never_partially_parsed(
    monkeypatch, tmp_path, body
) -> None:
    result, diagnostic, _requests, _calls = _provider_result(monkeypatch, tmp_path, body)
    assert result.diagnostic.reason_codes == ("streamed_response_rejected",)
    assert diagnostic.json_parse_result == "not_attempted"


def _live_manual_transport(monkeypatch, *, always_invalid: bool = False) -> None:
    def _request(
        _self,
        method,
        endpoint,
        *,
        body,
        headers,
        connect_timeout_seconds,
        read_timeout_seconds,
    ):
        assert connect_timeout_seconds > 0
        assert read_timeout_seconds > 8
        if method == "GET" and endpoint.endswith("/api/version"):
            return HttpTransportResponse(200, b'{"version":"0.32.5"}')
        if method == "GET" and endpoint.endswith("/api/tags"):
            return HttpTransportResponse(
                200,
                b'{"models":[{"name":"phase4-test-model"}]}',
            )
        assert method == "POST"
        payload = json.loads(body)
        if payload["messages"][1]["content"] == SYNTHETIC_PROBE_INPUT:
            return HttpTransportResponse(
                200,
                json.dumps(_ollama_envelope('{"ok":true}')).encode(),
            )
        visible = json.loads(payload["messages"][1]["content"])
        text = visible["user_message"]
        message_id = visible["message_id"]
        if always_invalid:
            content = json.dumps({"schema_version": 1, "unexpected": True})
        elif text == "I use Python for work.":
            content = json.dumps(_schema(_assertion(text, "I use Python", message_id)))
        elif text == "Now I want to create travel videos.":
            assertion = _assertion(text, "create travel videos", message_id)
            assertion.update(
                {
                    "memory_type_hint": "goal",
                    "domain_hint": "video_creation",
                    "slot_hint": "current_primary_goal",
                }
            )
            content = json.dumps(_schema(assertion))
        elif text.startswith("My diagnosis is "):
            value = text.removeprefix("My ").removesuffix(".")
            assertion = _assertion(text, value, message_id, sensitive=True)
            assertion["domain_hint"] = "health_fitness"
            content = json.dumps(_schema(assertion))
        else:
            raise AssertionError("unexpected live provider call")
        response_body = json.dumps(_ollama_envelope(content)).encode()
        return HttpTransportResponse(200, response_body)

    monkeypatch.setattr(StdlibJsonHttpTransport, "request", _request)


def _live_args():
    return SimpleNamespace(
        live_model=True,
        confirm_disposable_live_model=True,
        endpoint="http://provider.test/api/chat",
        model="phase4-test-model",
        provider="ollama",
        token_env="PHASE4_TEST_TOKEN_UNSET",
        connect_timeout_seconds=5,
        model_timeout_seconds=120,
        warmup_timeout_seconds=300,
        ollama_request_mode="auto",
    )


def test_live_manual_validator_reports_pass_only_for_functioning_provider(
    monkeypatch, tmp_path, capsys
) -> None:
    _live_manual_transport(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: ":quit")
    validation = manual_phase4._run_interactive(tmp_path, _live_args())
    rendered = capsys.readouterr().out
    assert validation.passed
    assert "live_model_call_count=2" in rendered
    assert "live_model_transport_success_count=2" in rendered
    assert "live_model_valid_schema_count=2" in rendered
    assert "live_model_invalid_schema_count=0" in rendered
    assert "live_model_transport_failure_count=0" in rendered
    assert "live_model_review_count=0" in rendered
    assert "deterministic_applied_count=" in rendered
    assert "phase4_live_model_validation=PASS" in rendered


def test_live_manual_validator_fails_when_every_live_response_is_schema_invalid(
    monkeypatch, tmp_path, capsys
) -> None:
    _live_manual_transport(monkeypatch, always_invalid=True)
    validation = manual_phase4._run_interactive(tmp_path, _live_args())
    rendered = capsys.readouterr().out
    assert not validation.passed
    assert "live_model_valid_schema_count=0" in rendered
    assert "live_model_invalid_schema_count=2" in rendered
    assert "live_model_applied_count=0" in rendered
    assert "phase4_live_model_validation=FAIL" in rendered


class _CapabilityTransport:
    def __init__(
        self,
        *,
        schema_supported: bool = True,
        json_supported: bool = True,
        think_supported: bool = True,
        model_available: bool = True,
    ) -> None:
        self.schema_supported = schema_supported
        self.json_supported = json_supported
        self.think_supported = think_supported
        self.model_available = model_available
        self.requests: list[tuple[str, str, dict | None, int, int]] = []

    def request(
        self,
        method,
        endpoint,
        *,
        body,
        headers,
        connect_timeout_seconds,
        read_timeout_seconds,
    ):
        del headers
        payload = json.loads(body) if body else None
        self.requests.append(
            (
                method,
                endpoint,
                payload,
                connect_timeout_seconds,
                read_timeout_seconds,
            )
        )
        if method == "GET" and endpoint.endswith("/api/version"):
            return HttpTransportResponse(200, b'{"version":"0.32.5"}')
        if method == "GET" and endpoint.endswith("/api/tags"):
            models = [{"name": "phase4-test-model"}] if self.model_available else []
            return HttpTransportResponse(200, json.dumps({"models": models}).encode())
        assert payload is not None
        if isinstance(payload.get("format"), dict) and not self.schema_supported:
            return HttpTransportResponse(
                400,
                b'{"error":"unsupported format JSON schema object"}',
            )
        if payload.get("format") == "json" and not self.json_supported:
            return HttpTransportResponse(400, b'{"error":"unsupported format json"}')
        if "think" in payload and not self.think_supported:
            return HttpTransportResponse(400, b'{"error":"unknown field think"}')
        return HttpTransportResponse(
            200,
            json.dumps(_ollama_envelope('{"ok":true}')).encode(),
        )


def test_configured_read_timeout_allows_cold_model_beyond_eight_seconds(tmp_path) -> None:
    class ColdModelTransport:
        def __init__(self) -> None:
            self.read_timeout = None

        def request(self, *_args, read_timeout_seconds, **_kwargs):
            self.read_timeout = read_timeout_seconds
            if read_timeout_seconds <= 8:
                raise ProviderTransportTimeout("read")
            return HttpTransportResponse(
                200,
                json.dumps(_ollama_envelope(json.dumps(_schema()))).encode(),
            )

    transport = ColdModelTransport()
    provider = OllamaChatExtractionProvider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        response_timeout_seconds=120,
        transport=transport,
    )
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=provider)
    result = run_text(
        extraction,
        harness,
        "This is not a durable fact.",
        message_id="cold-model",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert transport.read_timeout == 120
    assert result.diagnostic.http_status == 200


def test_read_timeout_below_simulated_cold_load_fails_at_configured_stage(tmp_path) -> None:
    class ColdModelTransport:
        def request(self, *_args, read_timeout_seconds, **_kwargs):
            assert read_timeout_seconds == 7
            raise ProviderTransportTimeout("read")

    provider = OllamaChatExtractionProvider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        response_timeout_seconds=7,
        transport=ColdModelTransport(),
    )
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=provider)
    result = run_text(
        extraction,
        harness,
        "I use Python for work.",
        message_id="short-cold-timeout",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.diagnostic.reason_codes == ("model_timeout",)
    assert result.diagnostic.provider_timeout_stage == "read"


def test_probe_selects_schema_mode_and_audits_optional_fields() -> None:
    transport = _CapabilityTransport()
    probe = probe_ollama_provider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        requested_mode="auto",
        transport=transport,
    )
    assert probe.successful
    assert probe.ollama_version == "0.32.5"
    assert probe.selected_request_mode is OllamaRequestMode.SCHEMA
    assert probe.capabilities == OllamaCapabilities(
        schema_format_supported=True,
        json_format_supported=True,
        think_field_supported=True,
        seed_option_supported=True,
        num_predict_option_supported=True,
        keep_alive_supported=True,
    )


def test_probe_selects_json_fallback_and_preserves_schema_rejection_cause() -> None:
    transport = _CapabilityTransport(schema_supported=False, think_supported=False)
    probe = probe_ollama_provider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        requested_mode="auto",
        transport=transport,
    )
    assert probe.successful
    assert probe.selected_request_mode is OllamaRequestMode.JSON
    assert not probe.capabilities.schema_format_supported
    assert probe.capabilities.json_format_supported
    assert not probe.capabilities.think_field_supported
    assert probe.sanitized_failure_code == "ollama_unsupported_format_schema"
    assert probe.sanitized_error_message == "unsupported format JSON schema object"


def test_explicit_schema_mode_fails_closed_when_schema_objects_are_unsupported() -> None:
    probe = probe_ollama_provider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        requested_mode="ollama_schema",
        transport=_CapabilityTransport(schema_supported=False),
    )
    assert not probe.successful
    assert probe.selected_request_mode is None
    assert probe.sanitized_failure_code == "ollama_unsupported_format_schema"


def test_model_unavailable_probe_has_stable_code() -> None:
    probe = probe_ollama_provider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        transport=_CapabilityTransport(model_available=False),
    )
    assert not probe.model_available
    assert not probe.warmup_success
    assert probe.sanitized_failure_code == "ollama_model_not_found"


def test_invalid_model_name_and_safe_400_messages_are_typed(tmp_path) -> None:
    text = "I use Python for work."
    responses = iter(
        (
            HttpTransportResponse(400, b'{"error":"invalid model name"}'),
            HttpTransportResponse(400, b'{"error":"unsupported format JSON schema object"}'),
        )
    )

    class ErrorTransport:
        def request(self, *_args, **_kwargs):
            return next(responses)

    provider = OllamaChatExtractionProvider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        transport=ErrorTransport(),
    )
    harness, _extraction, _diagnostics = phase4_harness(tmp_path)
    request, _context = extraction_input(
        harness,
        text,
        message_id="safe-error",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    visible = ModelExtractionInput.from_trusted_request(request)
    with pytest.raises(ExtractionModelError) as invalid:
        provider.extract(visible)
    assert invalid.value.code == "ollama_invalid_model_name"
    assert invalid.value.metadata.sanitized_error_message == "invalid model name"
    with pytest.raises(ExtractionModelError) as unsupported:
        provider.extract(visible)
    assert unsupported.value.code == "ollama_unsupported_format_schema"
    assert (
        unsupported.value.metadata.sanitized_error_message
        == "unsupported format JSON schema object"
    )


def test_json_mode_fallback_still_rejects_unknown_schema_fields(tmp_path) -> None:
    content = json.dumps({"schema_version": 1, "assertions": [], "unknown": True})

    class JsonTransport:
        def __init__(self) -> None:
            self.payloads = []

        def request(self, *_args, body, **_kwargs):
            self.payloads.append(json.loads(body))
            return HttpTransportResponse(
                200,
                json.dumps(_ollama_envelope(content)).encode(),
            )

    transport = JsonTransport()
    provider = OllamaChatExtractionProvider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        request_mode="ollama_json",
        capabilities=OllamaCapabilities(json_format_supported=True),
        transport=transport,
    )
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=provider)
    result = run_text(
        extraction,
        harness,
        "I use Python for work.",
        message_id="json-fallback",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert all(payload["format"] == "json" for payload in transport.payloads)
    assert result.status is ExtractionStatus.FAILED
    assert result.diagnostic.reason_codes == ("invalid_model_schema",)
    assert "schema_unknown_field" in result.diagnostic.schema_error_codes


def test_error_diagnostics_never_store_user_or_sensitive_plaintext(tmp_path) -> None:
    text = "My diagnosis is P4-PRIVATE-ERROR-CONTENT."

    class SensitiveErrorTransport:
        def request(self, *_args, **_kwargs):
            return HttpTransportResponse(
                400,
                json.dumps({"error": text}).encode(),
            )

    provider = OllamaChatExtractionProvider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        transport=SensitiveErrorTransport(),
    )
    harness, extraction, diagnostics = phase4_harness(tmp_path, model=provider)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="sensitive-error",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )
    rendered = json.dumps(diagnostics.snapshot()[-1].model_dump(mode="json"))
    assert text not in rendered
    assert "P4-PRIVATE-ERROR-CONTENT" not in rendered
    assert result.diagnostic.sanitized_provider_error_code == "ollama_invalid_request"


def test_synthetic_probe_leaves_all_persisted_sql_state_unchanged(tmp_path) -> None:
    harness, _extraction, _diagnostics = phase4_harness(tmp_path)
    before = sql_state(harness.database_path)
    transport = _CapabilityTransport(schema_supported=False, think_supported=False)
    probe = probe_ollama_provider(
        "http://provider.test/api/chat",
        model="phase4-test-model",
        transport=transport,
    )
    after = sql_state(harness.database_path)
    assert probe.successful
    assert before == after
    assert all(not rows for rows in after.values())
    post_payloads = [item[2] for item in transport.requests if item[0] == "POST"]
    assert post_payloads
    assert all(
        payload["messages"][1]["content"] == SYNTHETIC_PROBE_INPUT for payload in post_payloads
    )
