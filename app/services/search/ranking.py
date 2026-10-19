from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from app.services.search.types import FetchedPage, QueryRelevanceProfile, SearchResult

#: Words that never say what a page is about. Function words matter most: term
#: matching is by substring, so a query term like "of" matched almost any title
#: and let off-topic results through the relevance filter. Evaluative and
#: comparison words ("best", "good", "vs") say how to choose or relate things,
#: not what about: a search engine that answers only the first word returns
#: pages that match them and nothing else.
QUERY_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "best",
    "better",
    "between",
    "by",
    "can",
    "compare",
    "compared",
    "comparison",
    "could",
    "count",
    "current",
    "date",
    "did",
    "do",
    "does",
    "film",
    "find",
    "follow-up",
    "for",
    "from",
    "give",
    "good",
    "google",
    "great",
    "has",
    "have",
    "how",
    "if",
    "im",
    "in",
    "into",
    "is",
    "it",
    "its",
    "know",
    "latest",
    "look",
    "lookup",
    "many",
    "me",
    "movie",
    "much",
    "my",
    "new",
    "news",
    "nice",
    "now",
    "of",
    "on",
    "online",
    "or",
    "please",
    "recent",
    "release",
    "released",
    "releasing",
    "right",
    "search",
    "should",
    "so",
    "some",
    "tell",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "this",
    "those",
    "to",
    "today",
    "top",
    "up",
    "updates",
    "version",
    "versus",
    "vs",
    "was",
    "web",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
}

#: Generic words that mark a page as reporting something recent. Only time and
#: change words: no topic, competition, product or site appears here.
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
}

#: The question words and verbs a sentence opens with, which are capitalised for
#: grammar rather than because they name something.
_SENTENCE_OPENERS = {
    "what",
    "whats",
    "who",
    "whos",
    "how",
    "when",
    "where",
    "which",
    "why",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "will",
    "tell",
    "show",
    "give",
    "find",
    "search",
    "please",
    "explain",
    "compare",
    "list",
    "i",
    "im",
    "my",
    "the",
    "a",
    "an",
    "any",
    "and",
    "or",
    "if",
    "in",
    "on",
    "for",
    "get",
    "help",
    "look",
    "suggest",
    "recommend",
    "best",
    "latest",
    "current",
    "new",
}

#: URL shapes of video and short-post pages, which rarely carry readable
#: evidence. A shape, not a list of sites.
_VIDEO_OR_POST_URL = re.compile(
    r"/(?:watch|shorts|reel|reels|status|statuses|video|videos|clip|clips)(?:/|\?|$)|[?&]v=",
    re.IGNORECASE,
)

NON_DISCRIMINATIVE_TERMS = {
    "added",
    "announced",
    "change",
    "changed",
    "changes",
    "current",
    "currently",
    "difference",
    "differences",
    "improved",
    "improvement",
    "improvements",
    "info",
    "information",
    "latest",
    "new",
    "newest",
    "news",
    "official",
    "officially",
    "recent",
    "recently",
    "release",
    "released",
    "releases",
    "releasing",
    "update",
    "updated",
    "updates",
    "upcoming",
    "version",
    "versions",
}

MIN_FETCH_RELEVANCE_SCORE = 4.0
MIN_CONTEXT_RELEVANCE_SCORE = 8.0
MIN_READABLE_TEXT_CHARS = 80


def compact(value: str) -> str:
    """Lowercase letters and digits only: one form for every spelling of a name."""

    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def host_group(host: str) -> str:
    """Registrable-domain approximation, shared with the research pipeline.

    Imported when called: the research package imports the search types, so a
    module-level import here would be circular.
    """

    from app.services.research.textutil import host_group as research_host_group

    return research_host_group(host)


def public_suffix_class(host: str) -> str | None:
    """Public-sector or academic naming convention of a host; a weak signal."""

    from app.services.research.authority import public_suffix_class as research_suffix_class

    return research_suffix_class(host)


def build_relevance_profile(
    query: str,
    provider_query: str,
    *,
    subject_terms: list[str] | None = None,
    requires_freshness: bool | None = None,
) -> QueryRelevanceProfile:
    """What a relevant result must mention, derived from the query alone.

    ``requires_freshness`` comes from the search plan when there is one (a time
    window or a news request); without a plan only generic time words in the
    query decide it. ``entity_terms`` are the names the user wrote with a capital
    letter or with digits or symbols in them, used to recognise a source that
    belongs to the subject itself.
    """

    normalized_query = normalize_for_relevance(f"{query} {provider_query}")
    raw_tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+#-]*", normalized_query)
        if token not in QUERY_STOPWORDS and len(token) > 1
    ]
    tokens = list(dict.fromkeys(raw_tokens))
    subject = [term for term in (subject_terms or []) if term in tokens]
    aliases: list[str] = []
    seen: set[str] = set()
    # A term the plan names as the subject is one even when it is a year.
    for token in dict.fromkeys([*_topic_terms(tokens), *subject]):
        for alias in expanded_aliases(token):
            if alias not in seen:
                aliases.append(alias)
                seen.add(alias)
    if requires_freshness is None:
        requires_freshness = bool(
            re.search(
                r"\b(latest|current|currently|recent|recently|today|news|updates|"
                r"newest|right now|upcoming|this week)\b",
                query,
                re.IGNORECASE,
            )
        )
    return QueryRelevanceProfile(
        query=query,
        provider_query=provider_query,
        terms=tokens,
        aliases=aliases,
        requires_freshness=requires_freshness,
        subject_terms=subject,
        entity_terms=_entity_terms(query),
    )


def _entity_terms(query: str) -> list[str]:
    """Names in the user's own wording: capitalised words, or words with digits
    or symbols, other than the word a sentence happens to start with."""

    names: list[str] = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#'-]*", query or ""):
        bare = compact(word)
        if len(bare) < 3 or bare in _SENTENCE_OPENERS:
            continue
        if word[0].isupper() or re.search(r"[0-9.+#]", word[1:]):
            if bare not in names:
                names.append(bare)
    return names


def minimum_term_hits_for(profile: QueryRelevanceProfile) -> int:
    """How many query terms a result must contain to be considered on topic.

    Only *discriminative* terms count towards the threshold. "What changed in the
    latest release of <software>?" is about one thing -- the software -- so
    demanding two matches would reject every correct result, because no page
    about it says "changed" or "official" in its title.
    """
    counted = _topic_terms(
        profile.subject_terms
        or [term for term in profile.terms if term not in NON_DISCRIMINATIVE_TERMS]
    )
    # Names the user wrote are part of the subject even when the plan left them
    # out: for "population of <city> vs <city>" a plan naming only "population"
    # let a world-population page through on that one word.
    names = [
        term
        for term in _topic_terms(profile.entity_terms)
        if term in profile.terms and term not in counted
    ]
    return 2 if len(counted) + len(names) >= 3 else 1


def rank_results(profile: QueryRelevanceProfile, results: list[SearchResult]) -> list[SearchResult]:
    scored = [score_result(profile, result) for result in results]
    minimum_term_hits = minimum_term_hits_for(profile)
    relevant = [
        result
        for result in scored
        if result.relevance_score >= MIN_FETCH_RELEVANCE_SCORE
        and has_alias_hit(profile, f"{result.title} {result.url} {result.snippet or ''}")
        and mentions_subject(profile, f"{result.title} {result.url} {result.snippet or ''}")
        and term_hit_count(profile, f"{result.title} {result.url} {result.snippet or ''}")
        >= minimum_term_hits
        and not is_low_quality_result(result, profile)
        and not (is_video_or_social_result(result) and not _asks_for_video(profile.query))
        and (
            not profile.requires_freshness
            or has_freshness_hit(f"{result.title} {result.url} {result.snippet or ''}")
            or is_news_like_url(result.url)
            or is_primary_source(profile, result.url)
            or bool(result.published_date)
        )
    ]
    return sorted(relevant, key=lambda result: (-result.relevance_score, result.rank))


def score_result(profile: QueryRelevanceProfile, result: SearchResult) -> SearchResult:
    """General source-quality score: relevance first, then provenance signals.

    No site, brand or country gets a bonus for its name. A source scores higher
    when it is the subject's own site (its name matches a name the user wrote),
    when it is dated -- and recent, if the question needs recent information --
    and, weakly, when it is a public-sector or academic site. Video pages and
    thin or navigational pages score lower.
    """

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
        age_days = _age_in_days(result.published_date)
        if profile.requires_freshness and age_days is not None:
            if age_days <= 365:
                score += 1.5
                reasons.append("recent")
            elif age_days > 730:
                score -= 1.5
                reasons.append("stale")
    if is_primary_source(profile, result.url):
        score += 3.0
        reasons.append("primary_source")
    host = urlparse(result.url).hostname or ""
    if public_suffix_class(host) is not None:
        score += 0.5
        reasons.append("institutional")
    if is_video_or_social_result(result) and not _asks_for_video(profile.query):
        score -= 7.0
        reasons.append("video_or_post")
    if is_low_quality_result(result, profile):
        score -= 6.0
        reasons.append("low_quality")
    return result.model_copy(update={"relevance_score": score, "relevance_reasons": reasons})


def _asks_for_video(query: str) -> bool:
    return bool(re.search(r"\b(video|videos|trailer|clip|clips|watch)\b", query, re.IGNORECASE))


def _age_in_days(published: str) -> int | None:
    """Days since a published date in common formats, or ``None`` if unreadable."""

    text = (published or "").strip()
    if not text:
        return None
    candidates = [text[:10], text[:19], text]
    for candidate in candidates:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
            try:
                published_at = datetime.strptime(candidate, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
            return max(0, (datetime.now(UTC) - published_at).days)
    return None


def relevant_fetched_page(
    profile: QueryRelevanceProfile,
    result: SearchResult,
    page: FetchedPage,
) -> FetchedPage | None:
    if not page.fetched or not page.text or len(page.text.strip()) < MIN_READABLE_TEXT_CHARS:
        return None
    if is_language_mismatch(profile.query, page.text):
        return None
    if is_low_quality_page(page, profile):
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
        has_freshness_hit(page_text)
        or is_news_like_url(page.url)
        or is_primary_source(profile, page.url)
        or result.published_date
    ):
        return None
    if score < MIN_CONTEXT_RELEVANCE_SCORE:
        return None
    return page


def expanded_aliases(token: str) -> list[str]:
    """A term and its singular, for a plain English plural.

    Matching is by substring, so the singular also finds the plural: asked for
    "phones", a page titled "the best cheap phone" is on topic. Separator
    differences are handled in ``has_alias_hit`` rather than by a list of
    known spellings.
    """

    if len(token) >= 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return [token, token[:-1]]
    return [token]


def has_alias_hit(profile: QueryRelevanceProfile, value: str) -> bool:
    normalized = normalize_for_relevance(value)
    if any(alias in normalized for alias in profile.aliases):
        return True
    # A hyphenated, a spaced and a run-together spelling of one name are the
    # same name, and so are a dotted and an undotted one. Compare with
    # separators removed.
    squashed = compact(value)
    return any(
        len(form) >= 4 and form in squashed for form in (compact(a) for a in profile.aliases)
    )


def term_hit_count(profile: QueryRelevanceProfile, value: str) -> int:
    normalized = normalize_for_relevance(value)
    counted = {*_topic_terms(profile.terms), *profile.subject_terms}
    return sum(1 for term in counted if any(form in normalized for form in expanded_aliases(term)))


def mentions_subject(profile: QueryRelevanceProfile, value: str) -> bool:
    """Whether a result names the subject the search plan identified.

    The plan's subject terms are the words a correct result's title would
    contain. Without this a result could reach the hit threshold on side words
    alone: "mobile" from "best budget mobile phones" let an identity-card page
    about updating a registered mobile number through.
    """

    if not profile.subject_terms:
        return True
    normalized = normalize_for_relevance(value)
    squashed = compact(value)
    for term in profile.subject_terms:
        for form in expanded_aliases(term):
            if form in normalized or (len(form) >= 4 and compact(form) in squashed):
                return True
    return False


_YEAR_TERM = re.compile(r"(?:19|20)\d{2}")


def _topic_terms(terms: list[str]) -> list[str]:
    """The terms that say what a result is about, leaving out bare years.

    A year says when, not what: nearly every page published this year mentions
    it, so it counted as a topic match for any question that named the year --
    an identity-card page passed for "best budget mobile phones 2026" on
    "mobile" and "2026". A question whose only subject is a year keeps it.
    """

    topical = [term for term in terms if not _YEAR_TERM.fullmatch(term)]
    return topical or list(terms)


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
            "/articles/",
            "/explore/articles/",
        )
    )


def is_primary_source(profile: QueryRelevanceProfile, url: str) -> bool:
    """Whether ``url`` belongs to the subject itself: its site name contains a
    name the user wrote. Compared with separators removed, so a hyphenated or
    dotted site name still matches. No list of sites is involved."""

    if not profile.entity_terms:
        return False
    host = urlparse(url).hostname or ""
    site = compact(host_group(host.lower().removeprefix("www.")))
    return any(len(term) >= 3 and term in site for term in profile.entity_terms)


def is_low_quality_result(
    result: SearchResult, profile: QueryRelevanceProfile | None = None
) -> bool:
    """Navigation, account and search pages; a bare home page unless it is the
    subject's own site, where the home page often is the answer."""

    parsed = urlparse(result.url)
    path = parsed.path.lower().strip("/")
    if path in {"", "home"} and not (
        profile is not None and is_primary_source(profile, result.url)
    ):
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
    """A video or short-post page, recognised by the shape of its URL."""

    return bool(_VIDEO_OR_POST_URL.search(result.url))


def is_low_quality_page(page: FetchedPage, profile: QueryRelevanceProfile | None = None) -> bool:
    """A bare home page or a page that is mostly navigation with nothing that
    looks like evidence (no dates, figures or versions, nothing recent)."""

    parsed = urlparse(page.url)
    path = parsed.path.lower().strip("/")
    if path in {"", "home"} and not is_news_like_url(page.url):
        if not (profile is not None and is_primary_source(profile, page.url)):
            return True
    text = normalize_for_relevance(page.text[:1200])
    navigation_terms = sum(
        1
        for term in (
            "login",
            "sign up",
            "subscribe",
            "privacy policy",
            "terms",
            "menu",
            "follow us",
        )
        if term in text
    )
    has_evidence_signal = has_freshness_hit(page.text) or bool(
        re.search(r"\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)+\b", page.text[:4000])
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
    normalized = re.sub(r"[^a-z0-9./+#-]+", " ", normalized)
    return normalized
