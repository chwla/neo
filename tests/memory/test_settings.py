"""Tier 6 — runtime configuration (plan section RUN-01..11).

`MemorySettings` is a frozen dataclass that validates in `__post_init__`, which
makes misconfiguration a startup failure rather than a runtime one. That choice
is the reason these tests are mostly about refusals: the value of failing in the
constructor is entirely in *which* configurations it refuses to build.

`from_settings` is the other half, and it is where two real deployment failures
live — an extraction model that fell back to an uninstalled default, and an
Ollama endpoint that was never derived. Both are pinned here.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.memory import factory
from app.services.memory.extraction import OllamaRequestMode
from app.services.memory.settings import MemoryConfigurationError, MemorySettings


def _app_settings(**overrides):
    """A real `Settings`, adjusted per test.

    Built from the live defaults rather than a stub so that a field renamed in
    `Settings` breaks these tests instead of silently bypassing them.
    """

    return get_settings().model_copy(update=overrides)


class TestDefaults:
    def test_the_defaults_construct(self) -> None:
        """RUN-01 — the zero-argument case is the one every test fixture uses."""

        flags = MemorySettings()

        assert flags.enabled is True
        assert flags.live_extraction_model_enabled is False

    def test_the_settings_are_frozen(self) -> None:
        """Configuration must not drift during a request."""

        flags = MemorySettings()

        with pytest.raises(Exception):
            flags.enabled = False


class TestLiveExtractionGuards:
    """Live extraction is the only path that reaches out to a model process."""

    def test_live_extraction_without_an_endpoint_is_refused(self) -> None:
        """RUN-02"""

        with pytest.raises(MemoryConfigurationError) as error:
            MemorySettings(
                live_extraction_model_enabled=True,
                extraction_provider="ollama",
                extraction_endpoint="   ",
            )

        assert str(error.value) == "memory_live_extraction_requires_endpoint"

    def test_live_extraction_with_an_unknown_provider_is_refused(self) -> None:
        """RUN-03 — an unrecognised provider name must not default to something.

        Guessing here would mean posting the user's message to whatever the
        fallback happened to be.
        """

        with pytest.raises(MemoryConfigurationError) as error:
            MemorySettings(
                live_extraction_model_enabled=True,
                extraction_endpoint="http://localhost:11434/api/chat",
                extraction_provider="mystery_provider",
            )

        assert str(error.value) == "memory_live_extraction_requires_explicit_provider"

    @pytest.mark.parametrize("provider", ["direct_json", "ollama"])
    def test_the_supported_providers_are_accepted(self, provider: str) -> None:
        flags = MemorySettings(
            live_extraction_model_enabled=True,
            extraction_endpoint="http://localhost:11434/api/chat",
            extraction_provider=provider,
        )

        assert flags.extraction_provider == provider

    def test_the_guards_do_not_apply_when_live_extraction_is_off(self) -> None:
        """The default deployment has no endpoint and must still start."""

        assert MemorySettings(live_extraction_model_enabled=False).extraction_endpoint == ""

    @pytest.mark.parametrize("mode", ["auto", "ollama_schema", "ollama_json"])
    def test_each_request_mode_is_accepted(self, mode: str) -> None:
        assert MemorySettings(ollama_request_mode=mode).ollama_request_mode == mode

    def test_an_invalid_request_mode_is_refused(self) -> None:
        """RUN-04"""

        with pytest.raises(MemoryConfigurationError) as error:
            MemorySettings(ollama_request_mode="ollama_yaml")

        assert str(error.value) == "memory_ollama_request_mode_invalid"


class TestNumericBounds:
    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            ("extraction_max_input_chars", 499, "memory_extraction_input_limit_out_of_range"),
            ("extraction_max_input_chars", 50_001, "memory_extraction_input_limit_out_of_range"),
            ("recall_max_records", 0, "memory_recall_record_limit_out_of_range"),
            ("recall_max_records", 21, "memory_recall_record_limit_out_of_range"),
            ("recall_max_chars", 199, "memory_recall_char_limit_out_of_range"),
            ("recall_max_chars", 12_001, "memory_recall_char_limit_out_of_range"),
            ("recall_min_score", -0.01, "memory_recall_score_out_of_range"),
            ("recall_min_score", 1.01, "memory_recall_score_out_of_range"),
        ],
    )
    def test_each_bound_is_refused_at_both_ends(
        self, field: str, value: object, code: str
    ) -> None:
        """RUN-05 — and each refusal names itself, so the operator can fix it."""

        with pytest.raises(MemoryConfigurationError) as error:
            MemorySettings(**{field: value})

        assert str(error.value) == code

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("extraction_max_input_chars", 500),
            ("extraction_max_input_chars", 50_000),
            ("recall_max_records", 1),
            ("recall_max_records", 20),
            ("recall_max_chars", 200),
            ("recall_max_chars", 12_000),
            ("recall_min_score", 0.0),
            ("recall_min_score", 1.0),
        ],
    )
    def test_each_bound_accepts_its_extremes(self, field: str, value: object) -> None:
        """RUN-06 — inclusive at both ends, so a `<` written for `<=` is caught."""

        assert getattr(MemorySettings(**{field: value}), field) == value


class TestFromSettings:
    def test_the_ollama_endpoint_is_derived_when_unset(self) -> None:
        """RUN-07 — configuring `ollama_url` alone is enough to run extraction."""

        flags = MemorySettings.from_settings(
            _app_settings(
                memory_extraction_provider="ollama",
                memory_extraction_endpoint="",
                ollama_url="http://example:11434/",
            )
        )

        assert flags.extraction_endpoint == "http://example:11434/api/chat"

    def test_an_explicit_endpoint_is_not_overwritten(self) -> None:
        """The derivation is a fallback, not a rewrite."""

        flags = MemorySettings.from_settings(
            _app_settings(
                memory_extraction_provider="ollama",
                memory_extraction_endpoint="http://custom/api/chat",
                ollama_url="http://example:11434",
            )
        )

        assert flags.extraction_endpoint == "http://custom/api/chat"

    def test_a_non_ollama_provider_must_supply_its_own_endpoint(self) -> None:
        """Only Ollama has a derivation, so anything else fails closed at startup.

        Found by writing this test expecting an empty endpoint. It does not
        return one — it raises, because `from_settings` ties
        `live_extraction_model_enabled` to `memory_extraction_enabled`, which is
        on by default. So a `direct_json` deployment that forgets its endpoint
        cannot start, which is the right outcome and better than the silent
        empty string I assumed.
        """

        with pytest.raises(MemoryConfigurationError) as error:
            MemorySettings.from_settings(
                _app_settings(
                    memory_extraction_provider="direct_json",
                    memory_extraction_endpoint="",
                )
            )

        assert str(error.value) == "memory_live_extraction_requires_endpoint"

    def test_the_shipped_defaults_produce_a_valid_configuration(self) -> None:
        """The derivation is load-bearing, not a convenience.

        The default config is `provider="ollama"` with an *empty* endpoint and
        extraction enabled. Only the `ollama_url` derivation makes that valid —
        without it the shipped defaults would fail `__post_init__` and the
        service would not start at all.
        """

        flags = MemorySettings.from_settings(get_settings())

        assert flags.extraction_provider == "ollama"
        assert flags.extraction_endpoint.endswith("/api/chat")

    def test_the_extraction_model_falls_back_to_the_default_model(self) -> None:
        """RUN-08 — the deployment regression, pinned.

        Falling back to `chat_model` left the container pointing extraction at an
        uninstalled built-in default, and every model-backed extraction failed
        with "model not found". `default_model` is what NEO_DEFAULT_MODEL
        populates, and it mirrors `chat_model` when only that is set.
        """

        flags = MemorySettings.from_settings(
            _app_settings(memory_extraction_model="", default_model="llama3.2:3b")
        )

        assert flags.extraction_model == "llama3.2:3b"

    def test_an_explicit_extraction_model_wins(self) -> None:
        flags = MemorySettings.from_settings(
            _app_settings(
                memory_extraction_model="qwen2.5:7b",
                default_model="llama3.2:3b",
            )
        )

        assert flags.extraction_model == "qwen2.5:7b"

    @pytest.mark.parametrize(
        ("worker", "semantic", "expected"),
        [
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ],
    )
    def test_the_vector_index_requires_both_switches(
        self, worker: bool, semantic: bool, expected: bool
    ) -> None:
        """RUN-09 — a vector index with no worker to fill it is worse than none.

        All four combinations, because the interesting cases are the two where
        the switches disagree; testing only the diagonal would pass for a plain
        alias of either flag.
        """

        flags = MemorySettings.from_settings(
            _app_settings(
                memory_index_worker_enabled=worker,
                memory_semantic_recall_enabled=semantic,
            )
        )

        assert flags.vector_index_enabled is expected

    def test_reconciliation_and_fts_follow_the_worker(self) -> None:
        flags = MemorySettings.from_settings(
            _app_settings(memory_index_worker_enabled=False)
        )

        assert flags.fts_index_enabled is False
        assert flags.reconciliation_enabled is False


class TestOwnerGate:
    @pytest.mark.parametrize(
        ("enabled", "owner", "expected"),
        [
            (True, "11111111-1111-4111-8111-111111111111", True),
            (True, "", False),
            (False, "11111111-1111-4111-8111-111111111111", False),
            (False, "", False),
        ],
    )
    def test_owner_is_enabled(self, enabled: bool, owner: str, expected: bool) -> None:
        """RUN-10 — a blank owner is never enabled, whatever the flag says.

        This is the guard that keeps an unbound request from reading a store: an
        empty owner would otherwise scope every query to nothing in particular.
        """

        assert MemorySettings(enabled=enabled).owner_is_enabled(owner) is expected


class TestRequestModeNegotiation:
    """RUN-11 — `auto` must actually probe, which it once did not.

    Collapsing `auto` to `ollama_schema` without probing meant a server that
    rejects JSON-schema `format` failed *every* model-backed extraction, silently.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """The negotiated mode is cached in module state, keyed by endpoint+model.

        Without clearing it, whichever test ran first would decide the answer for
        all the others — a cross-test dependency that is invisible until the
        order changes.
        """

        factory._negotiated_ollama_modes.clear()
        yield
        factory._negotiated_ollama_modes.clear()

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("ollama_json", OllamaRequestMode.JSON),
            ("ollama_schema", OllamaRequestMode.SCHEMA),
        ],
    )
    def test_an_explicit_mode_is_honoured_without_probing(
        self, configured: str, expected: OllamaRequestMode, monkeypatch
    ) -> None:
        def _fail(*args, **kwargs):
            raise AssertionError("probe must not run for an explicit mode")

        monkeypatch.setattr(factory, "probe_ollama_provider", _fail)

        mode, capabilities = factory._resolve_ollama_request_mode(
            MemorySettings(ollama_request_mode=configured)
        )

        assert mode is expected
        assert capabilities is None

    def test_auto_resolves_from_the_probe(self, monkeypatch) -> None:
        class Capabilities:
            schema_format_supported = True
            json_format_supported = True

        class Probe:
            selected_request_mode = OllamaRequestMode.SCHEMA
            capabilities = Capabilities()
            sanitized_failure_code = None

        monkeypatch.setattr(factory, "probe_ollama_provider", lambda *a, **k: Probe())

        mode, capabilities = factory._resolve_ollama_request_mode(
            MemorySettings(
                ollama_request_mode="auto",
                live_extraction_model_enabled=True,
                extraction_provider="ollama",
                extraction_endpoint="http://localhost:11434/api/chat",
                extraction_model="llama3.2:3b",
            )
        )

        assert mode is OllamaRequestMode.SCHEMA
        assert capabilities is not None

    def test_the_probe_result_is_cached_per_endpoint_and_model(self, monkeypatch) -> None:
        """Probing on every extraction would add a round trip to every turn."""

        calls: list[tuple] = []

        class Probe:
            selected_request_mode = OllamaRequestMode.JSON
            capabilities = type(
                "Capabilities",
                (),
                {"schema_format_supported": False, "json_format_supported": True},
            )()
            sanitized_failure_code = None

        def _probe(*args, **kwargs):
            calls.append(args)
            return Probe()

        monkeypatch.setattr(factory, "probe_ollama_provider", _probe)
        flags = MemorySettings(
            ollama_request_mode="auto",
            live_extraction_model_enabled=True,
            extraction_provider="ollama",
            extraction_endpoint="http://localhost:11434/api/chat",
            extraction_model="llama3.2:3b",
        )

        first = factory._resolve_ollama_request_mode(flags)
        second = factory._resolve_ollama_request_mode(flags)

        assert first == second
        assert len(calls) == 1

    def test_an_unreachable_provider_falls_back_without_caching(self, monkeypatch) -> None:
        """A transient outage must not pin the mode for the process lifetime.

        This is why the failure path returns without writing the cache — asserted
        by probing twice and expecting two attempts.
        """

        calls: list[int] = []

        def _explode(*args, **kwargs):
            calls.append(1)
            raise ConnectionError("connection refused")

        monkeypatch.setattr(factory, "probe_ollama_provider", _explode)
        flags = MemorySettings(
            ollama_request_mode="auto",
            live_extraction_model_enabled=True,
            extraction_provider="ollama",
            extraction_endpoint="http://localhost:11434/api/chat",
            extraction_model="llama3.2:3b",
        )

        mode, capabilities = factory._resolve_ollama_request_mode(flags)
        factory._resolve_ollama_request_mode(flags)

        assert mode is OllamaRequestMode.JSON
        assert capabilities is None
        assert len(calls) == 2

    def test_an_inconclusive_probe_falls_back_to_json(self, monkeypatch) -> None:
        """JSON is the safer default: schema support is the newer capability."""

        class Probe:
            selected_request_mode = None
            capabilities = None
            sanitized_failure_code = "probe_inconclusive"

        monkeypatch.setattr(factory, "probe_ollama_provider", lambda *a, **k: Probe())

        mode, capabilities = factory._resolve_ollama_request_mode(
            MemorySettings(
                ollama_request_mode="auto",
                live_extraction_model_enabled=True,
                extraction_provider="ollama",
                extraction_endpoint="http://localhost:11434/api/chat",
                extraction_model="llama3.2:3b",
            )
        )

        assert mode is OllamaRequestMode.JSON
        assert capabilities is None

    def test_a_non_ollama_provider_does_not_probe(self, monkeypatch) -> None:
        def _fail(*args, **kwargs):
            raise AssertionError("probe must not run for a non-ollama provider")

        monkeypatch.setattr(factory, "probe_ollama_provider", _fail)

        mode, capabilities = factory._resolve_ollama_request_mode(
            MemorySettings(ollama_request_mode="auto", extraction_provider="direct_json")
        )

        assert mode is OllamaRequestMode.SCHEMA
        assert capabilities is None
