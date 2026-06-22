from __future__ import annotations

import re
from urllib.parse import urlparse

from app.services.search.types import FetchedPage, QueryRelevanceProfile, SearchResult


QUERY_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "can",
    "could",
    "current",
    "find",
    "for",
    "from",
    "google",
    "how",
    "is",
    "latest",
    "look",
    "lookup",
    "news",
    "on",
    "online",
    "please",
    "recent",
    "search",
    "the",
    "today",
    "up",
    "updates",
    "version",
    "web",
    "when",
    "where",
    "what",
    "who",
    "you",
}

FRESHNESS_TERMS = {
    "announced",
    "announces",
    "breaking",
    "current",
    "currently",
    "latest",
    "launched",
    "launches",
    "news",
    "new",
    "newest",
    "price",
    "prices",
    "cost",
    "fixture",
    "fixtures",
    "match",
    "schedule",
    "upcoming",
    "recent",
    "recently",
    "release",
    "released",
    "update",
    "updated",
    "updates",
    "version",
    "ranking",
    "rankings",
    "ranked",
    "rated",
    "fide",
    "champion",
    "worldcup",
}

OFFICIAL_DOMAINS = {
    "about.fb.com",
    "bcci.tv",
    "icc-cricket.com",
    "marvel.com",
    "nextjs.org",
    "npmjs.com",
    "openai.com",
    "primevideo.com",
    "registry.npmjs.org",
    "x.ai",
    "www.anthropic.com",
    "www.bcci.tv",
    "www.icc-cricket.com",
    "www.marvel.com",
    "www.npmjs.com",
    "www.primevideo.com",
    "www.x.ai",
}

TRUSTED_NEWS_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "reuters.com",
    "techcrunch.com",
    "theverge.com",
    "www.apnews.com",
    "www.bbc.com",
    "www.reuters.com",
    "www.techcrunch.com",
    "www.theverge.com",
}

INDIA_SOURCE_DOMAINS = {
    "business-standard.com",
    "district.in",
    "economictimes.indiatimes.com",
    "filmibeat.com",
    "gadgets360.com",
    "in.bookmyshow.com",
    "indiatoday.in",
    "news24online.com",
    "thehindu.com",
    "timesnownews.com",
    "www.business-standard.com",
    "www.district.in",
    "www.filmibeat.com",
    "www.gadgets360.com",
    "www.indiatoday.in",
    "www.news24online.com",
    "www.thehindu.com",
    "www.timesnownews.com",
}

MIN_FETCH_RELEVANCE_SCORE = 4.0
MIN_CONTEXT_RELEVANCE_SCORE = 8.0
MIN_READABLE_TEXT_CHARS = 80


def build_relevance_profile(query: str, provider_query: str) -> QueryRelevanceProfile:
    normalized_query = normalize_for_relevance(f"{query} {provider_query}")
    raw_tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+#-]*", normalized_query)
        if token not in QUERY_STOPWORDS and len(token) > 1
    ]
    tokens = list(dict.fromkeys(raw_tokens))
    aliases: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for alias in expanded_aliases(token):
            if alias not in seen:
                aliases.append(alias)
                seen.add(alias)
    requires_freshness = bool(
        re.search(r"\b(latest|current|currently|recent|recently|today|news|updates|version|release|newest|ranking|rankings|ranked|rated|right now|fide|updated|champion|world champion|world cup|upcoming|coming out)\b", query, re.IGNORECASE)
    )
    return QueryRelevanceProfile(
        query=query,
        provider_query=provider_query,
        terms=tokens,
        aliases=aliases,
        requires_freshness=requires_freshness,
    )


def rank_results(profile: QueryRelevanceProfile, results: list[SearchResult]) -> list[SearchResult]:
    scored = [score_result(profile, result) for result in results]
    minimum_term_hits = 2 if len(profile.terms) >= 3 else 1
    relevant = [
        result
        for result in scored
        if result.relevance_score >= MIN_FETCH_RELEVANCE_SCORE
        and has_alias_hit(profile, f"{result.title} {result.url} {result.snippet or ''}")
        and term_hit_count(profile, f"{result.title} {result.url} {result.snippet or ''}") >= minimum_term_hits
        and not is_low_quality_result(result)
        and not (
            is_video_or_social_result(result)
            and not re.search(r"\b(video|youtube|trailer|clip|watch|instagram|reddit|facebook|social|post)\b", profile.query, re.IGNORECASE)
        )
        and (
            not profile.requires_freshness
            or has_freshness_hit(f"{result.title} {result.url} {result.snippet or ''}")
            or is_news_like_url(result.url)
            or bool(result.published_date)
        )
    ]
    return sorted(relevant, key=lambda result: (-result.relevance_score, result.rank))


def score_result(profile: QueryRelevanceProfile, result: SearchResult) -> SearchResult:
    reasons: list[str] = []
    score = 0.0
    if has_alias_hit(profile, result.title):
        score += 4.0
        reasons.append("title")
    if has_alias_hit(profile, f"{result.source} {result.url}"):
        score += 3.0
        reasons.append("url")
    if has_alias_hit(profile, result.snippet or ""):
        score += 4.0
        reasons.append("snippet")
    if has_freshness_hit(f"{result.title} {result.url} {result.snippet or ''}"):
        score += 1.0
        reasons.append("freshness")
    if result.published_date:
        score += 1.0
        reasons.append("date")
    domain = urlparse(result.url).netloc.lower().removeprefix("www.")
    if domain in OFFICIAL_DOMAINS or f"www.{domain}" in OFFICIAL_DOMAINS:
        score += 3.0
        reasons.append("official")
    elif domain in TRUSTED_NEWS_DOMAINS or f"www.{domain}" in TRUSTED_NEWS_DOMAINS:
        score += 1.5
        reasons.append("trusted")
    result_text = f"{result.title} {result.url} {result.snippet or ''}".lower()
    if "india" in profile.terms or "indian" in profile.terms:
        if domain in INDIA_SOURCE_DOMAINS or f"www.{domain}" in INDIA_SOURCE_DOMAINS or re.search(
            r"\b(india|indian|mumbai|delhi|chennai|bengaluru|gurgaon|hindi|tamil|telugu)\b",
            result_text,
        ):
            score += 5.0
            reasons.append("india_source")
        else:
            score -= 2.0
            reasons.append("missing_india_signal")
    if is_video_or_social_result(result) and not re.search(r"\b(video|youtube|trailer|clip|watch)\b", profile.query, re.IGNORECASE):
        score -= 7.0
        reasons.append("video_social")
    if is_low_quality_result(result):
        score -= 6.0
        reasons.append("low_quality")
    return result.model_copy(update={"relevance_score": score, "relevance_reasons": reasons})


def relevant_fetched_page(
    profile: QueryRelevanceProfile,
    result: SearchResult,
    page: FetchedPage,
) -> FetchedPage | None:
    if not page.fetched or not page.text or len(page.text.strip()) < MIN_READABLE_TEXT_CHARS:
        return None
    if is_language_mismatch(profile.query, page.text):
        return None
    if is_low_quality_page(page):
        return None

    page_text = f"{page.title or ''} {page.url} {page.text[:8000]}"
    score = float(result.relevance_score)
    if has_alias_hit(profile, page.title or ""):
        score += 3.0
    if has_alias_hit(profile, page_text):
        score += 4.0
    if has_freshness_hit(page_text) or is_news_like_url(page.url):
        score += 2.0
    if profile.requires_freshness and not (
        has_freshness_hit(page_text) or is_news_like_url(page.url) or result.published_date
    ):
        return None
    if score < MIN_CONTEXT_RELEVANCE_SCORE:
        return None
    return page


def expanded_aliases(token: str) -> list[str]:
    aliases = {
        "anthropic": ["anthropic", "claude"],
        "claude": ["claude", "anthropic"],
        "facebook": ["facebook", "meta"],
        "meta": ["meta", "facebook"],
        "openai": ["openai", "chatgpt"],
        "chatgpt": ["chatgpt", "openai"],
        "nextjs": ["nextjs", "next.js"],
        "spiderman": ["spiderman", "spider-man", "spider man"],
        "npm": ["npm"],
    }
    return aliases.get(token, [token])


def has_alias_hit(profile: QueryRelevanceProfile, value: str) -> bool:
    normalized = normalize_for_relevance(value)
    return any(alias in normalized for alias in profile.aliases)


def term_hit_count(profile: QueryRelevanceProfile, value: str) -> int:
    normalized = normalize_for_relevance(value)
    return sum(1 for term in set(profile.terms) if term in normalized)


def has_freshness_hit(value: str) -> bool:
    normalized = normalize_for_relevance(value)
    if re.search(r"\b20[2-9][0-9]\b", normalized):
        return True
    return any(term in normalized for term in FRESHNESS_TERMS)


def is_news_like_url(url: str) -> bool:
    normalized = normalize_for_relevance(url)
    return any(
        part in normalized
        for part in (
            "/news",
            "newsroom",
            "/blog",
            "/releases",
            "/package/",
            "/fixtures",
            "getupcomingmatches",
            "/articles/",
            "/explore/articles/",
        )
    )


def is_low_quality_result(result: SearchResult) -> bool:
    parsed = urlparse(result.url)
    path = parsed.path.lower().strip("/")
    domain = parsed.netloc.lower()
    if path in {"", "home"} and domain not in {"openai.com", "www.anthropic.com", "nextjs.org", "about.fb.com"}:
        return True
    low_quality_parts = (
        "/account",
        "/help",
        "/login",
        "/r.php",
        "/search",
        "/signin",
        "/signup",
        "wallpapers",
    )
    return any(part in result.url.lower() for part in low_quality_parts)


def is_video_or_social_result(result: SearchResult) -> bool:
    domain = urlparse(result.url).netloc.lower().removeprefix("www.")
    return domain in {
        "facebook.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "instagram.com",
        "reddit.com",
        "x.com",
        "twitter.com",
    }


def is_low_quality_page(page: FetchedPage) -> bool:
    parsed = urlparse(page.url)
    path = parsed.path.lower().strip("/")
    if path in {"", "home"} and not is_news_like_url(page.url):
        return True
    text = normalize_for_relevance(page.text[:1200])
    navigation_terms = sum(
        1
        for term in ("login", "sign up", "subscribe", "privacy policy", "terms", "menu", "follow us")
        if term in text
    )
    has_evidence_signal = has_freshness_hit(page.text) or bool(
        re.search(r"upcoming match|version", page.text, re.IGNORECASE)
    )
    return navigation_terms >= 4 and not has_evidence_signal


def is_language_mismatch(query: str, text: str) -> bool:
    if not query.isascii():
        return False
    sample = text[:2000]
    if not sample:
        return True
    ascii_count = sum(1 for char in sample if char.isascii())
    return ascii_count / len(sample) < 0.65


def normalize_for_relevance(value: str) -> str:
    normalized = value.lower()
    normalized = normalized.replace("next.js", "nextjs")
    normalized = normalized.replace("next js", "nextjs")
    normalized = normalized.replace("open ai", "openai")
    normalized = normalized.replace("spider-man", "spiderman")
    normalized = normalized.replace("spider man", "spiderman")
    normalized = re.sub(r"[^a-z0-9./+#-]+", " ", normalized)
    return normalized
