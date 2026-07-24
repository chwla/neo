from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.chat import NeoChatService
from app.services.search import core
from app.services.search.citations import validate_citation_markers
from app.services.search.content import extract_release_date
from app.services.search.core import (
    WebSearchDecisionService,
    WebSearchService,
    _run_provider_chain,
    comprehensive_web_search,
    provider_query,
)
from app.services.search.intent import resolve_search_intent
from app.services.search.live_data import (
    FrankfurterClient,
    OpenMeteoClient,
    local_datetime_answer,
)
from app.services.search.providers import WebSearchProvider
from app.services.search.types import (
    EvidenceChunk,
    FetchedPage,
    SearchIntentKind,
    SearchOptions,
    SearchResult,
    WebSearchResponse,
)
from app.services.source_citations import SourceCitation


@pytest.mark.parametrize(
    "statement",
    [
        (
            "I recently graduated from BITS Pilani with a Bachelor of Engineering "
            "in Computer Science."
        ),
        "I lovee playing chess.",
        "I find samurais to be interesting.",
        "Bobby Fischer was my fabourite chess player.",
        "I want to master programming.",
        "I want to get into FAANG in 6 months.",
        "I prioritise working with Python and C++/C.",
        "I am currently playing Ghost of Yotei.",
    ],
)
def test_personal_transcript_statements_never_route_to_live_data(statement: str) -> None:
    intent = resolve_search_intent(statement)

    assert intent.kind == SearchIntentKind.NONE
    assert WebSearchDecisionService().decide(statement).needed is False


def test_currency_intent_carries_pair_and_amount_into_follow_ups() -> None:
    first = resolve_search_intent("What is the current conversion of 1 USD to INR?")
    second = resolve_search_intent("What about 10 USD?", previous=first)
    third = resolve_search_intent(
        "What if I had 10 USD. How much would that be in rupees?",
        previous=first,
    )

    assert first.kind == SearchIntentKind.CURRENCY
    assert (first.amount, first.from_currency, first.to_currency) == (
        Decimal("1"),
        "USD",
        "INR",
    )
    assert (second.amount, second.from_currency, second.to_currency) == (
        Decimal("10"),
        "USD",
        "INR",
    )
    assert (third.amount, third.from_currency, third.to_currency) == (
        Decimal("10"),
        "USD",
        "INR",
    )


def test_weather_intent_carries_operation_into_location_fragment() -> None:
    first = resolve_search_intent("What is the current weather in Bengaluru?")
    follow_up = resolve_search_intent("New Delhi today", previous=first)

    assert first.kind == SearchIntentKind.WEATHER
    assert first.location == "Bengaluru"
    assert follow_up.kind == SearchIntentKind.WEATHER
    assert follow_up.location == "New Delhi"
    assert follow_up.date == "today"


def test_ambiguous_release_pronoun_does_not_trigger_an_operation_without_context() -> None:
    intent = resolve_search_intent("When will it release?")

    assert intent.kind == SearchIntentKind.NONE


def test_unrelated_live_request_does_not_inherit_previous_currency_intent() -> None:
    currency = resolve_search_intent("Convert 1 USD to INR")

    weather = resolve_search_intent(
        "What is the current weather in New Delhi?",
        previous=currency,
    )

    assert weather.kind == SearchIntentKind.WEATHER
    assert weather.location == "New Delhi"


def test_mixed_personal_and_live_turn_extracts_an_entity_specific_search() -> None:
    weather = resolve_search_intent(
        "I live in New Delhi; what is the weather today?",
    )
    release = resolve_search_intent(
        "I am playing Ghost of Yotei, when does it release?",
    )

    assert weather.kind == SearchIntentKind.WEATHER
    assert weather.location == "New Delhi"
    assert release.kind == SearchIntentKind.RELEASE_DATE
    assert release.entity == "Ghost of Yotei"


def test_date_today_is_local_and_never_a_web_search() -> None:
    intent = resolve_search_intent(
        "What is the date today?",
        timezone="Asia/Kolkata",
        locale="en-IN",
    )
    result = local_datetime_answer(
        "What is the date today?",
        browser_timezone="Asia/Kolkata",
        locale="en-IN",
        now=datetime(2026, 7, 23, 20, 30, tzinfo=UTC),
    )

    assert intent.kind == SearchIntentKind.LOCAL_DATETIME
    assert intent.needs_external_data is False
    assert WebSearchDecisionService().decide(intent.original_query).needed is False
    assert result.used_web is False
    assert result.timezone == "Asia/Kolkata"
    assert "Friday, July 24, 2026" in result.answer


def test_invalid_browser_timezone_falls_back_to_profile_timezone() -> None:
    result = local_datetime_answer(
        "What time is it?",
        browser_timezone="../../etc/passwd",
        profile_timezone="Asia/Kolkata",
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )

    assert result.timezone == "Asia/Kolkata"
    assert "5:30 PM" in result.answer


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


def test_frankfurter_conversion_uses_decimal_and_reference_date() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs: object) -> _Response:
        calls.append((url, kwargs))
        return _Response({"date": "2026-07-23", "base": "USD", "rates": {"INR": 86.125}})

    quote = FrankfurterClient(http_get=get).convert(Decimal("10"), "usd", "inr")

    assert quote.rate == Decimal("86.125")
    assert quote.converted_amount == Decimal("861.250")
    assert quote.reference_date == "2026-07-23"
    assert calls[0][1]["params"] == {"base": "USD", "symbols": "INR"}


def test_open_meteo_uses_structured_geocoding_and_current_weather() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs: object) -> _Response:
        calls.append((url, kwargs))
        if "geocoding-api" in url:
            return _Response(
                {
                    "results": [
                        {
                            "name": "New Delhi",
                            "country": "India",
                            "latitude": 28.6139,
                            "longitude": 77.209,
                        }
                    ]
                }
            )
        return _Response(
            {
                "timezone": "Asia/Kolkata",
                "current": {
                    "time": "2026-07-23T18:00",
                    "temperature_2m": 31.4,
                    "apparent_temperature": 37.2,
                    "weather_code": 2,
                    "wind_speed_10m": 9.5,
                },
            }
        )

    report = OpenMeteoClient(http_get=get).current_weather(
        "New Delhi",
        locale="en-IN",
        timezone="Asia/Kolkata",
    )

    assert report.location == "New Delhi"
    assert report.country == "India"
    assert report.temperature_c == Decimal("31.4")
    assert report.condition == "partly cloudy"
    assert report.timezone == "Asia/Kolkata"
    assert calls[0][1]["params"]["name"] == "New Delhi"
    assert calls[1][1]["params"]["current"] == (
        "temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
    )


def test_open_meteo_tomorrow_uses_daily_forecast_not_current_conditions() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs: object) -> _Response:
        calls.append((url, kwargs))
        if "geocoding-api" in url:
            return _Response(
                {
                    "results": [
                        {
                            "name": "New Delhi",
                            "country": "India",
                            "latitude": 28.6139,
                            "longitude": 77.209,
                        }
                    ]
                }
            )
        return _Response(
            {
                "timezone": "Asia/Kolkata",
                "daily": {
                    "time": ["2026-07-23", "2026-07-24"],
                    "weather_code": [2, 61],
                    "temperature_2m_max": [34.0, 30.5],
                    "temperature_2m_min": [26.0, 24.5],
                    "precipitation_probability_max": [20, 80],
                },
            }
        )

    report = OpenMeteoClient(http_get=get).forecast_weather(
        "New Delhi",
        day="tomorrow",
        locale="en-IN",
        timezone="Asia/Kolkata",
    )

    assert report.forecast_date == "2026-07-24"
    assert report.temperature_max_c == Decimal("30.5")
    assert report.temperature_min_c == Decimal("24.5")
    assert report.condition == "slight rain"
    assert report.precipitation_probability_max == Decimal("80")
    assert "current" not in calls[1][1]["params"]
    assert calls[1][1]["params"]["forecast_days"] == 2


def test_chat_tomorrow_weather_uses_forecast_branch(monkeypatch) -> None:
    called: list[str] = []

    class FakeWeather:
        def current_weather(self, *args, **kwargs):
            raise AssertionError("tomorrow intent used current conditions")

        def forecast_weather(self, location, *, day, locale, timezone):
            called.append(day)
            from app.services.search.live_data import WeatherForecast

            return WeatherForecast(
                location=location,
                country="India",
                latitude=28.6139,
                longitude=77.209,
                timezone=timezone,
                forecast_date="2026-07-24",
                temperature_max_c=Decimal("30.5"),
                temperature_min_c=Decimal("24.5"),
                condition="slight rain",
                weather_code=61,
                precipitation_probability_max=Decimal("80"),
            )

    monkeypatch.setattr("app.services.chat.OpenMeteoClient", FakeWeather)
    service = object.__new__(NeoChatService)
    service.settings = SimpleNamespace(default_timezone="UTC")
    service.store = SimpleNamespace(active_profile_by_key=lambda _key: [])
    prompt = "What is the weather in New Delhi tomorrow?"
    intent = resolve_search_intent(prompt)

    answer = service._structured_live_answer(
        prompt,
        intent,
        timezone="Asia/Kolkata",
        locale="en-IN",
    )

    assert called == ["tomorrow"]
    assert answer is not None
    reply, metadata = answer
    assert "forecast" in reply.lower()
    assert "2026-07-24" in reply
    assert metadata["route_name"] == "weather"


def _chunk(
    *,
    index: int,
    title: str,
    url: str,
    text: str,
    relevance: float = 10,
) -> EvidenceChunk:
    return EvidenceChunk(
        source_index=index,
        source_title=title,
        source_url=url,
        source=url.split("/")[2],
        text=text,
        relevance_score=relevance,
    )


def test_release_extractor_rejects_article_publication_date_and_untrusted_claim() -> None:
    query = "When is the next God of War game going to release?"
    chunks = [
        _chunk(
            index=1,
            title="State of Play June 2026 announcements",
            url="https://blog.playstation.com/2026/06/02/state-of-play/",
            text=(
                "Published June 2, 2026. God of War: Sons of Sparta and "
                "God of War Laufey are coming to PlayStation."
            ),
        ),
        _chunk(
            index=2,
            title="God of War Laufey release date",
            url="https://nerdyinfo.com/god-of-war-laufey",
            text="God of War Laufey releases on June 2, 2026.",
        ),
    ]

    assert extract_release_date(query, chunks) is None


def test_release_extractor_accepts_entity_matched_official_release_statement() -> None:
    fact = extract_release_date(
        "When is Spider-Man Brand New Day releasing?",
        [
            _chunk(
                index=3,
                title="Spider-Man: Brand New Day | Marvel",
                url="https://www.marvel.com/movies/spider-man-brand-new-day",
                text="Spider-Man: Brand New Day releases on July 31, 2026.",
            )
        ],
    )

    assert fact is not None
    assert fact.answer == "July 31, 2026"
    assert fact.source_index == 3
    assert fact.match_reason == "official_explicit_release_date"


def test_release_extractor_requires_two_independent_authoritative_nonofficial_sources() -> None:
    query = "When is Example Film releasing in India?"
    one = _chunk(
        index=1,
        title="Example Film India release",
        url="https://in.bookmyshow.com/example-film",
        text="Example Film releases on July 30, 2026.",
    )
    two = _chunk(
        index=2,
        title="Example Film tickets",
        url="https://www.district.in/movies/example-film",
        text="Example Film releases on July 30, 2026.",
    )

    assert extract_release_date(query, [one]) is None
    assert extract_release_date(query, [one, two]) is not None


def test_god_of_war_provider_query_does_not_invent_title_or_year() -> None:
    rewritten = provider_query("When is the next God of War game going to release?")

    assert rewritten == "God of War next game release date official"
    assert "Laufey" not in rewritten
    assert "2026" not in rewritten


def test_citation_validation_strips_generated_sources_and_rejects_unknown_markers() -> None:
    citations = [
        SourceCitation(
            index=1,
            title="Official source",
            url="https://example.com/official",
            source="example.com",
            fetched=True,
        )
    ]
    valid = validate_citation_markers(
        "The verified value is 42 [1].\n\nSources:\n[2] invented",
        citations,
        supported_indices={1},
    )
    invalid = validate_citation_markers(
        "The verified value is 42 [9].",
        citations,
        supported_indices={1},
    )

    assert valid.valid is True
    assert "Sources:" not in valid.answer
    assert invalid.valid is False
    assert any("Unknown citation" in error for error in invalid.errors)


class _StaticProvider(WebSearchProvider):
    def __init__(self, name: str, results: list[SearchResult]) -> None:
        self.name = name
        self.results = results

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
    ) -> WebSearchResponse:
        return WebSearchResponse(query=query, provider=self.name, results=self.results)


def test_provider_chain_continues_after_irrelevant_raw_results(monkeypatch) -> None:
    irrelevant = _StaticProvider(
        "first",
        [
            SearchResult(
                title="Unrelated phone news",
                url="https://example.com/phone",
                snippet="A phone launched today.",
                source="example.com",
                rank=1,
            )
        ],
    )
    useful = _StaticProvider(
        "second",
        [
            SearchResult(
                title="God of War news | PlayStation",
                url="https://blog.playstation.com/god-of-war/",
                snippet="Official news about the next God of War game.",
                source="blog.playstation.com",
                rank=1,
            )
        ],
    )
    monkeypatch.setattr(
        core,
        "get_settings",
        lambda: SimpleNamespace(web_search_enabled=True, web_search_max_results=5),
    )
    monkeypatch.setattr(
        core,
        "ProviderRegistry",
        lambda: SimpleNamespace(chain=lambda: [irrelevant, useful]),
    )

    response = _run_provider_chain(
        "latest news about the next God of War game",
        SearchOptions(max_results=5),
    )

    assert response.provider == "second"
    assert response.attempted_providers["first"].startswith("unusable")
    assert [item["status"] for item in response.provider_attempts] == [
        "ranking_rejected",
        "accepted",
    ]
    assert all(isinstance(item["duration_ms"], int) for item in response.provider_attempts)


def test_search_snippet_cannot_become_evidence_when_page_fetch_fails(monkeypatch) -> None:
    provider = _StaticProvider(
        "snippet-only",
        [
            SearchResult(
                title="Example Film release date",
                url="https://example.com/example-film",
                snippet="Example Film releases on July 30, 2026.",
                source="example.com",
                rank=1,
            )
        ],
    )

    class _FailedFetcher:
        @staticmethod
        def fetch(url: str) -> FetchedPage:
            return FetchedPage(
                url=url,
                title="Example Film release date",
                domain="example.com",
                fetched=False,
                error="Fetch failed.",
            )

    monkeypatch.setattr(
        core,
        "get_settings",
        lambda: SimpleNamespace(
            web_search_enabled=True,
            web_search_max_results=5,
            web_fetch_max_pages=3,
            web_context_max_tokens=1_000,
        ),
    )
    context = WebSearchService(provider=provider, fetcher=_FailedFetcher()).build_context_forced(
        "When is Example Film releasing?"
    )

    assert context.evidence_chunks == []
    assert context.citations == []
    assert context.pages == []
    assert context.warning is not None


def test_provider_fallback_continues_after_ranked_results_have_unusable_fetches(
    monkeypatch,
) -> None:
    first = _StaticProvider(
        "first",
        [
            SearchResult(
                title="Example Film release date rumor",
                url="https://rumor.invalid/example-film",
                snippet="Example Film releases on June 2, 2026.",
                source="rumor.invalid",
                rank=1,
            )
        ],
    )
    second = _StaticProvider(
        "second",
        [
            SearchResult(
                title="Example Film | Marvel",
                url="https://www.marvel.com/movies/example-film",
                snippet="The official Example Film page.",
                source="marvel.com",
                rank=1,
            )
        ],
    )

    def fake_fetch_pages(results: list[SearchResult], _max_pages: int) -> list[FetchedPage]:
        result = results[0]
        if result.url.startswith("https://rumor.invalid"):
            return [
                FetchedPage(
                    url=result.url,
                    title=result.title,
                    domain="rumor.invalid",
                    fetched=False,
                    error="Fetch failed.",
                )
            ]
        return [
            FetchedPage(
                url=result.url,
                title=result.title,
                domain="marvel.com",
                text=(
                    "Example Film releases on July 30, 2026. This official page "
                    "provides production information and the confirmed theatrical "
                    "release announcement for Example Film."
                ),
                fetched=True,
                content_type="text/html",
            )
        ]

    monkeypatch.setattr(
        core,
        "get_settings",
        lambda: SimpleNamespace(
            web_search_enabled=True,
            web_search_max_results=5,
            web_fetch_max_pages=3,
            web_context_max_tokens=1_000,
        ),
    )
    monkeypatch.setattr(
        core,
        "ProviderRegistry",
        lambda: SimpleNamespace(chain=lambda: [first, second]),
    )
    monkeypatch.setattr(core, "fetch_pages", fake_fetch_pages)

    result = comprehensive_web_search(
        "When is Example Film releasing?",
        SearchOptions(max_results=5, max_pages=3),
    )

    assert result.provider_used == "second"
    assert result.debug["attempted_providers"]["first"].startswith("unusable evidence")
    assert [item["status"] for item in result.debug["provider_attempts"]] == [
        "evidence_rejected",
        "accepted",
    ]
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://www.marvel.com/movies/example-film"
    assert result.citations[0].fetched is True
    assert extract_release_date(result.query, result.evidence_chunks) is not None
