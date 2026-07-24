from __future__ import annotations

import pytest

from app.services.chat import (
    NeoChatService,
    _price_query_clarification,
    _strip_llm_sources_block,
    resolve_web_search_query,
)
from app.services.llm import ChatTurn
from app.services.search.content import run_extractors
from app.services.search.core import (
    WebSearchDecisionService,
    _snippet_fallback_pages,
    provider_query,
)
from app.services.search.providers import _DuckDuckGoHTMLParser
from app.services.search.ranking import build_relevance_profile, rank_results
from app.services.search.types import EvidenceChunk, SearchResult, WebContext
from app.services.source_citations import CitationFormatter, SourceCitation


@pytest.mark.parametrize(
    "statement",
    [
        "I am currently building Neo and Playboxd.",
        "I'm developing Atlas.",
        "I am working on a new project.",
        "My name is Soham Chawla.",
        "I am 21 years old.",
        "Call me Jordan Rivera.",
        "I go by Riya.",
        "I currently live in Bengaluru.",
        "I'm based in Mumbai.",
        "I work as a software engineer.",
        "I prefer concise answers.",
        "My goal is to launch this product.",
        "Remember that I use VS Code.",
        "My projects are Neo, Playboxd, and Atlas.",
        "I'm currently reading news about Apple.",
        "I'm now watching the latest trailer.",
    ],
)
def test_personal_memory_statements_do_not_trigger_web_search(statement: str) -> None:
    decision = WebSearchDecisionService().decide(statement)

    assert decision.needed is False


@pytest.mark.parametrize(
    "query",
    [
        "What is the latest iPhone model?",
        "Search the web for today's technology news.",
        "Who is currently the world chess champion?",
        "How many seasons does The Sopranos have?",
        "Explain the latest iPhone lineup.",
        "What is the weather in Bengaluru today?",
        "Who is the current CEO of Apple?",
        "Find the current Python release.",
        "I need to know the latest iPhone price.",
        "I want to find today's weather.",
    ],
)
def test_queries_that_need_verification_still_trigger_search(query: str) -> None:
    assert WebSearchDecisionService().decide(query).needed is True


@pytest.mark.parametrize(
    "query",
    [
        "Who am I?",
        "What is my occupation?",
        "Where do I live?",
        "What projects am I working on?",
        "What do I prefer?",
        "Explain the term latest.",
        "What does the word current mean?",
        "Explain binary search.",
        "What is my current location?",
        "Show my active projects.",
        "Tell me about my current goals.",
        "What is the status of my project Neo?",
    ],
)
def test_personal_recall_and_stable_explanations_do_not_search(query: str) -> None:
    assert WebSearchDecisionService().decide(query).needed is False


def test_personal_statement_with_explicit_follow_up_search_is_not_suppressed() -> None:
    decision = WebSearchDecisionService().decide(
        "I use an iPhone 15; what is the latest iPhone model?"
    )

    assert decision.needed is True


@pytest.mark.parametrize(
    "query",
    [
        "I am currently reading news about Apple; what happened today?",
        "I prefer Android, but what is the latest iPhone?",
        "My project uses Python; find the current stable Python release.",
    ],
)
def test_personal_context_with_explicit_current_question_still_searches(query: str) -> None:
    assert WebSearchDecisionService().decide(query).needed is True


@pytest.mark.parametrize(
    "follow_up",
    [
        "When will it release?",
        "Where can I watch it?",
        "What about India?",
        "How much will it cost?",
        "Tell me more about that.",
    ],
)
def test_contextual_web_follow_ups_keep_the_previous_subject(follow_up: str) -> None:
    history = [
        ChatTurn(
            role="user",
            content="When is the new Spider-Man movie releasing in India?",
        ),
        ChatTurn(
            role="assistant",
            content="Spider-Man: Brand New Day is scheduled for release.",
        ),
    ]

    resolved = resolve_web_search_query(follow_up, history)

    assert "new Spider-Man movie" in resolved
    assert "India" in resolved
    assert follow_up in resolved


def test_contextual_follow_up_does_not_borrow_an_unrelated_personal_question() -> None:
    history = [
        ChatTurn(role="user", content="Who am I?"),
        ChatTurn(role="assistant", content="Your name is Soham."),
    ]

    assert resolve_web_search_query("When will it release?", history) == "When will it release?"


def test_contextual_release_ranking_rejects_an_unrelated_game_release() -> None:
    query = "when is the new spiderman movie releasing in india Follow-up: when will it release?"
    rewritten = provider_query(query)
    profile = build_relevance_profile(query, rewritten)
    ranked = rank_results(
        profile,
        [
            SearchResult(
                title="Grand Theft Auto VI launch date",
                url="https://www.rockstargames.com/newswire/gta-vi-launch",
                snippet="Grand Theft Auto VI launches on November 19, 2026.",
                source="rockstargames.com",
                rank=1,
            ),
            SearchResult(
                title="Spider-Man: Brand New Day in India",
                url="https://www.sonypictures.in/movies/spider-man-brand-new-day",
                snippet="Spider-Man: Brand New Day is in Indian cinemas on July 30, 2026.",
                source="sonypictures.in",
                rank=2,
            ),
        ],
    )

    assert rewritten == "new Spider-Man movie India release date"
    assert profile.terms == ["spiderman", "india"]
    assert [result.source for result in ranked] == ["sonypictures.in"]


def test_regional_release_answer_uses_the_matching_entity_and_source() -> None:
    service = object.__new__(NeoChatService)
    service.citation_formatter = CitationFormatter()
    context = WebContext(
        query="Spider-Man Brand New Day India release date",
        needed=True,
        answer_mode="fact_lookup",
        evidence_chunks=[
            EvidenceChunk(
                source_index=1,
                source_title="Grand Theft Auto VI",
                source_url="https://www.rockstargames.com/newswire/gta-vi-launch",
                source="rockstargames.com",
                text="Grand Theft Auto VI releases on November 19, 2026.",
                relevance_score=8,
            ),
            EvidenceChunk(
                source_index=2,
                source_title="Spider-Man: Brand New Day | Sony Pictures India",
                source_url="https://www.sonypictures.in/movies/spider-man-brand-new-day",
                source="sonypictures.in",
                text=("Spider-Man: Brand New Day releases in Indian cinemas on July 30, 2026."),
                relevance_score=14,
            ),
        ],
        citations=[
            SourceCitation(
                index=1,
                title="Grand Theft Auto VI",
                url="https://www.rockstargames.com/newswire/gta-vi-launch",
                source="rockstargames.com",
                fetched=True,
            ),
            SourceCitation(
                index=2,
                title="Spider-Man: Brand New Day | Sony Pictures India",
                url="https://www.sonypictures.in/movies/spider-man-brand-new-day",
                source="sonypictures.in",
                fetched=True,
            ),
        ],
    )

    reply = service._direct_web_reply(context.query, context)

    assert reply is not None
    assert "July 30, 2026 [2]" in reply
    assert "sonypictures.in" in reply
    assert "Grand Theft Auto" not in reply
    assert "rockstargames.com" not in reply


def test_chat_release_answer_accepts_two_corroborating_authoritative_sources() -> None:
    service = object.__new__(NeoChatService)
    service.citation_formatter = CitationFormatter()
    context = WebContext(
        query="Example Film India release date",
        needed=True,
        answer_mode="fact_lookup",
        evidence_chunks=[
            EvidenceChunk(
                source_index=1,
                source_title="Example Film India release",
                source_url="https://in.bookmyshow.com/example-film",
                source="in.bookmyshow.com",
                text="Example Film releases on July 30, 2026.",
                relevance_score=12,
            ),
            EvidenceChunk(
                source_index=2,
                source_title="Example Film tickets",
                source_url="https://www.district.in/movies/example-film",
                source="district.in",
                text="Example Film releases on July 30, 2026.",
                relevance_score=11,
            ),
        ],
        citations=[
            SourceCitation(
                index=1,
                title="Example Film India release",
                url="https://in.bookmyshow.com/example-film",
                source="in.bookmyshow.com",
                fetched=True,
            ),
            SourceCitation(
                index=2,
                title="Example Film tickets",
                url="https://www.district.in/movies/example-film",
                source="district.in",
                fetched=True,
            ),
        ],
    )

    reply = service._direct_web_reply(context.query, context)

    assert reply is not None
    assert "verified release date is July 30, 2026" in reply
    assert "bookmyshow.com" in reply


def test_named_entity_fact_results_are_not_rejected_for_query_helper_words() -> None:
    query = "how many seasons does the sopranos have"
    rewritten = provider_query(query)
    profile = build_relevance_profile(query, rewritten)
    results = [
        SearchResult(
            title="The Sopranos - Wikipedia",
            url="https://en.wikipedia.org/wiki/The_Sopranos",
            source="en.wikipedia.org",
            rank=1,
        ),
        SearchResult(
            title="List of The Sopranos episodes - Wikipedia",
            url="https://en.wikipedia.org/wiki/List_of_The_Sopranos_episodes",
            source="en.wikipedia.org",
            rank=2,
        ),
    ]

    ranked = rank_results(profile, results)

    assert profile.terms == ["seasons", "sopranos"]
    assert [result.url for result in ranked] == [
        "https://en.wikipedia.org/wiki/The_Sopranos",
        "https://en.wikipedia.org/wiki/List_of_The_Sopranos_episodes",
    ]


def test_duckduckgo_parser_keeps_snippet_with_its_result() -> None:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(
        """
        <a class="result__a" href="https://example.com/show">The Example Show</a>
        <a class="result__snippet">The series ran for six seasons.</a>
        <a class="result__a" href="https://example.com/other">Other Result</a>
        <div class="result__snippet">A second description.</div>
        """
    )
    parser.close()

    assert parser.results == [
        {
            "title": "The Example Show",
            "url": "https://example.com/show",
            "snippet": "The series ran for six seasons.",
        },
        {
            "title": "Other Result",
            "url": "https://example.com/other",
            "snippet": "A second description.",
        },
    ]


def test_current_official_product_page_is_eligible_without_date_in_title() -> None:
    query = "What is the latest iPhone model?"
    profile = build_relevance_profile(query, provider_query(query))
    ranked = rank_results(
        profile,
        [
            SearchResult(
                title="iPhone - Apple",
                url="https://www.apple.com/iphone/",
                source="www.apple.com",
                rank=1,
            )
        ],
    )

    assert len(ranked) == 1
    assert "official" in ranked[0].relevance_reasons


def test_fact_lookup_does_not_promote_provider_snippets_to_evidence() -> None:
    query = "How many seasons does The Sopranos have?"
    profile = build_relevance_profile(query, provider_query(query))
    pages = _snippet_fallback_pages(
        profile,
        "fact_lookup",
        [
            SearchResult(
                title="Episode Guide | The Sopranos Wiki",
                url="https://example.com/sopranos",
                snippet=(
                    "The Sopranos ran for 6 seasons between January 10, 1999 and June 10, 2007."
                ),
                source="example.com",
                rank=1,
                relevance_score=10,
            )
        ],
        3,
    )
    assert pages == []


def test_authoritative_fact_source_beats_a_blog_with_stronger_wording() -> None:
    query = "How many seasons does The Sopranos have?"
    chunks = [
        EvidenceChunk(
            source_index=1,
            source_title="The Sopranos",
            source_url="https://en.wikipedia.org/wiki/The_Sopranos",
            source="en.wikipedia.org",
            text="The series has six seasons.",
            relevance_score=12,
        ),
        EvidenceChunk(
            source_index=2,
            source_title="A television blog",
            source_url="https://example.com/sopranos-seasons",
            source="example.com",
            text="The show ran for 6 seasons.",
            relevance_score=12,
        ),
    ]

    fact = run_extractors(query, chunks)

    assert fact is not None
    assert fact.source_index == 1


def test_weather_snippet_is_not_treated_as_fetched_forecast_evidence() -> None:
    query = "What is the weather in Bengaluru today?"
    profile = build_relevance_profile(query, provider_query(query))
    pages = _snippet_fallback_pages(
        profile,
        "fact_lookup",
        [
            SearchResult(
                title="Today's Weather in Bengaluru",
                url="https://example.com/weather",
                snippet=(
                    "Today weather in Bengaluru Thursday, 23 July 2026 "
                    "31° Sunny. Feels like 22°. Low 21°."
                ),
                source="example.com",
                rank=1,
                relevance_score=10,
            )
        ],
        3,
    )
    assert pages == []


def test_compact_weather_snippet_is_not_treated_as_fetched_evidence() -> None:
    query = "What is the weather in Bengaluru today?"
    profile = build_relevance_profile(query, provider_query(query))
    pages = _snippet_fallback_pages(
        profile,
        "fact_lookup",
        [
            SearchResult(
                title="Bengaluru Weather Today",
                url="https://example.com/weather",
                snippet="Get the latest weather insights: Bangalore 28°C 13 AQI.",
                source="example.com",
                rank=1,
                relevance_score=10,
            )
        ],
        3,
    )
    assert pages == []


def test_underspecified_technology_price_requires_clarification() -> None:
    assert _price_query_clarification("What is the latest iPhone price?") is not None
    assert _price_query_clarification("iPhone 17 price in India") is None


def test_inline_model_generated_sources_block_is_removed_before_backend_citations() -> None:
    reply = "Bengaluru is 31°C and sunny today [2]. [ Sources: [1] Untrusted duplicate source text"

    assert _strip_llm_sources_block(reply) == "Bengaluru is 31°C and sunny today [2]."
