from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from app.core.config import get_settings
from app.services.search.security import is_public_http_url
from app.services.search.types import EvidenceChunk, FetchedPage, QueryRelevanceProfile, SearchResult


SUPPORTED_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}

UNSUPPORTED_EXTENSIONS = (
    ".7z", ".avi", ".dmg", ".doc", ".docx", ".exe", ".gz", ".iso",
    ".mp3", ".mp4", ".ppt", ".pptx", ".rar", ".tar", ".webm",
    ".xls", ".xlsx", ".zip",
)

MIN_EVIDENCE_CHUNK_SCORE = 5.0

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})
_JUNK_TAGS = frozenset({"nav", "header", "footer", "aside"})
_JUNK_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "menu", "menubar"})
_JUNK_CLASSES = re.compile(
    r"\b(nav|navbar|sidebar|side-bar|header|footer|menu|breadcrumb|cookie|"
    r"social|share|related|advertisement|ad-|ads-|promo|popup|modal|"
    r"skip-link|site-header|site-footer|site-nav|masthead|"
    r"toc|table-of-contents|mw-navigation|mw-panel|mw-header|mw-footer|"
    r"catlinks|printfooter|noprint|navbox|sidebar|infobox-below|"
    r"top-nav|bottom-nav|main-nav|global-nav|page-header|page-footer)\b",
    re.IGNORECASE,
)
_CONTENT_TAGS = frozenset({"main", "article"})
_CONTENT_CLASSES = re.compile(
    r"\b(mw-body-content|mw-parser-output|mw-content-text|"
    r"article-body|post-content|entry-content|content-area|"
    r"infobox|wikitable)\b",
    re.IGNORECASE,
)


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description: str | None = None
        self.og_title: str | None = None
        self.og_description: str | None = None
        self._skip_depth = 0
        self._junk_depth = 0
        self._content_depth = 0
        self._in_title = False
        self._content_parts: list[str] = []
        self._infobox_parts: list[str] = []
        self._infobox_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_dict = dict(attrs) if attrs else {}

        if tag == "meta":
            name = (attr_dict.get("name") or "").lower()
            prop = (attr_dict.get("property") or "").lower()
            content = attr_dict.get("content", "")
            if name == "description" and content:
                self.meta_description = content
            if prop == "og:title" and content:
                self.og_title = content
            if prop == "og:description" and content:
                self.og_description = content
            return

        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return

        role = (attr_dict.get("role") or "").lower()
        cls = attr_dict.get("class") or ""
        aria_label = (attr_dict.get("aria-label") or "").lower()

        is_junk = (
            tag in _JUNK_TAGS
            or role in _JUNK_ROLES
            or _JUNK_CLASSES.search(cls)
            or aria_label in {"navigation", "site navigation", "main navigation", "footer", "sidebar"}
        )
        if is_junk:
            self._junk_depth += 1
            return

        is_content = tag in _CONTENT_TAGS or _CONTENT_CLASSES.search(cls)
        if is_content:
            self._content_depth += 1

        if "infobox" in cls.lower():
            self._infobox_depth += 1

        if tag == "title":
            self._in_title = True
        if tag in {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "main", "p", "section", "tr", "td", "th", "dt", "dd"}:
            target = self._active_list()
            target.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in _JUNK_TAGS:
            if self._junk_depth:
                self._junk_depth -= 1
            return
        attr_cls = ""
        if tag in _CONTENT_TAGS:
            if self._content_depth:
                self._content_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"article", "div", "li", "main", "p", "section", "tr", "table"}:
            target = self._active_list()
            target.append("\n")
        if self._infobox_depth and tag in {"table", "div"}:
            self._infobox_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._junk_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        target = self._active_list()
        target.append(cleaned)
        if self._infobox_depth:
            self._infobox_parts.append(cleaned)

    def _active_list(self) -> list[str]:
        if self._content_depth:
            return self._content_parts
        return self.parts

    @property
    def text(self) -> str:
        primary = self._content_parts if self._content_parts else self.parts
        raw = normalize_text(" ".join(primary))
        if self._infobox_parts:
            infobox = normalize_text(" ".join(self._infobox_parts))
            if infobox and infobox not in raw:
                raw = f"INFOBOX: {infobox}\n{raw}"
        return raw

    @property
    def title(self) -> str | None:
        for candidate in [self.og_title, " ".join(self.title_parts)]:
            title = normalize_text(candidate or "")
            if title:
                return title
        return None

    @property
    def description(self) -> str | None:
        for candidate in [self.og_description, self.meta_description]:
            desc = normalize_text(candidate or "")
            if desc:
                return desc
        return None


def normalize_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def untrusted_context_message(content: str, source: str) -> str:
    return (
        f"<untrusted_web_context source={source!r}>\n"
        f"{content}\n"
        "</untrusted_web_context>"
    )


# ---------------------------------------------------------------------------
# Source-specific content cleaning
# ---------------------------------------------------------------------------

def _clean_wikipedia_text(text: str, page: FetchedPage) -> str:
    """Extract useful content from Wikipedia pages."""
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^(References|External links|See also|Further reading|Notes)\s*$", stripped):
            break
        if re.search(r"\b(edit|Edit)\s*\]", stripped):
            stripped = re.sub(r"\[\s*edit\s*\]", "", stripped, flags=re.IGNORECASE).strip()
        if len(stripped) < 10:
            continue
        if re.search(r"(Wikipedia|Wikimedia|Creative Commons|GNU Free|Retrieved from)", stripped):
            continue
        cleaned.append(stripped)
    result = " ".join(cleaned)
    infobox_match = re.search(r"INFOBOX:\s*(.+?)(?:\n|$)", text)
    if infobox_match:
        result = f"INFOBOX: {infobox_match.group(1).strip()}\n{result}"
    return result


def _clean_fide_text(text: str, page: FetchedPage) -> str:
    """Extract useful content from FIDE pages, stripping menus and sidebar."""
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) < 15:
            continue
        if re.search(r"\b(PARNTERS|CONTACTS|MAIN/NEWS|RATINGS|CHAMPIONSHIP|CALENDAR|HANDBOOK|Documents)\b", stripped):
            continue
        if re.search(r"\b(Top Federations|Main Page|Download|Financial Reports|Clean Sport)\b", stripped):
            continue
        cleaned.append(stripped)
    return " ".join(cleaned)


def _apply_source_cleanup(text: str, page: FetchedPage) -> str:
    """Apply source-specific text cleaning based on domain."""
    domain = page.domain.lower().removeprefix("www.")
    if "wikipedia.org" in domain:
        return _clean_wikipedia_text(text, page)
    if "fide.com" in domain:
        return _clean_fide_text(text, page)
    return text


# ---------------------------------------------------------------------------
# Page fetcher
# ---------------------------------------------------------------------------

class WebPageFetcher:
    def __init__(
        self,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.max_bytes = max_bytes or settings.web_fetch_max_bytes
        self.user_agent = user_agent or settings.web_search_user_agent

    def fetch(self, url: str) -> FetchedPage:
        parsed = urlparse(url)
        domain = parsed.netloc or "unknown"
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return FetchedPage(url=url, domain=domain, error="Invalid or unsupported URL.")
        if not self._allowed_url(url):
            return FetchedPage(url=url, domain=domain, error="Blocked unsafe or unsupported URL.")

        current = url
        response: requests.Response | None = None
        try:
            for _ in range(6):
                if not self._allowed_url(current):
                    return FetchedPage(url=current, domain=urlparse(current).netloc or domain, error="Blocked unsafe redirect.")
                response = requests.get(
                    current,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
                    },
                    stream=True,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
                if getattr(response, "status_code", 200) not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                response.close()
                if not location:
                    break
                current = urljoin(current, location)
            else:
                return FetchedPage(url=current, domain=urlparse(current).netloc or domain, error="Too many redirects.")
            if response is None:
                return FetchedPage(url=url, domain=domain, error="Fetch failed.")
            response.raise_for_status()
        except requests.Timeout:
            return FetchedPage(url=current, domain=urlparse(current).netloc or domain, error="Fetch timed out.")
        except requests.RequestException as exc:
            return FetchedPage(url=current, domain=urlparse(current).netloc or domain, error=f"Fetch failed: {exc}")

        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
        if content_type and content_type not in SUPPORTED_CONTENT_TYPES:
            response.close()
            return FetchedPage(
                url=current,
                domain=urlparse(current).netloc or domain,
                content_type=content_type,
                error=f"Unsupported content type: {content_type}",
            )

        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    return FetchedPage(
                        url=current,
                        domain=urlparse(current).netloc or domain,
                        content_type=content_type,
                        error="Page exceeded configured maximum size.",
                    )
        finally:
            response.close()

        encoding = response.encoding or "utf-8"
        raw = bytes(body).decode(encoding, errors="replace")
        if content_type in {"application/json", "text/plain"}:
            text = normalize_text(raw)
            return FetchedPage(
                url=current,
                title=current,
                domain=urlparse(current).netloc or domain,
                text=text,
                fetched=bool(text),
                content_type=content_type,
                error=None if text else "No readable text found.",
            )

        parser = _ReadableHTMLParser()
        parser.feed(raw)
        text = parser.text
        page = FetchedPage(
            url=current,
            title=parser.title or current,
            domain=urlparse(current).netloc or domain,
            text=text,
            fetched=bool(text),
            content_type=content_type or "text/html",
            error=None if text else "No readable text found.",
        )
        if text:
            cleaned = _apply_source_cleanup(text, page)
            if cleaned:
                page = page.model_copy(update={"text": cleaned})
            if parser.description:
                page = page.model_copy(update={"text": f"META: {parser.description}\n{page.text}"})
        return page

    def _allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        path = parsed.path.lower()
        if path.endswith(UNSUPPORTED_EXTENSIONS):
            return False
        return is_public_http_url(url)


def fetch_pages(results: list[SearchResult], max_pages: int) -> list[FetchedPage]:
    fetcher = WebPageFetcher()
    pages: list[FetchedPage] = []
    with ThreadPoolExecutor(max_workers=min(max_pages or 1, 4)) as executor:
        future_to_result = {
            executor.submit(fetcher.fetch, result.url): result
            for result in results[: max(max_pages * 3, max_pages)]
        }
        for future in as_completed(future_to_result):
            result = future_to_result[future]
            try:
                page = future.result()
            except Exception as exc:
                page = FetchedPage(url=result.url, title=result.title, domain=result.source, error=str(exc))
            if not page.title:
                page.title = result.title
            pages.append(page)
            if sum(1 for item in pages if item.fetched and item.text) >= max_pages:
                break
    return pages


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def extract_evidence_chunks(
    profile: QueryRelevanceProfile,
    answer_mode: str,
    pages: list[FetchedPage],
) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    for page in pages:
        if not page.fetched or not page.text:
            continue
        scored: list[EvidenceChunk] = []
        seen: set[str] = set()
        for candidate in _candidate_chunks(page):
            fingerprint = _normalize_for_relevance(candidate[:220])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            score = _score_chunk(profile, answer_mode, candidate)
            if score < MIN_EVIDENCE_CHUNK_SCORE:
                continue
            scored.append(
                EvidenceChunk(
                    source_title=page.title or page.url,
                    source_url=page.url,
                    source=page.domain,
                    text=candidate,
                    relevance_score=score,
                )
            )
        chunks.extend(sorted(scored, key=lambda chunk: chunk.relevance_score, reverse=True)[:3])
    return sorted(chunks, key=lambda chunk: chunk.relevance_score, reverse=True)[:8]


def augment_page(query: str, page: FetchedPage) -> FetchedPage:
    if "bcci.tv" not in page.domain or "fixtures" not in page.url:
        return page
    settings = get_settings()
    try:
        response = requests.get(
            "https://scores2.bcci.tv/getUpcomingMatches",
            params={
                "platform": "international",
                "previousMatchesCount": 0,
                "filterType": "all",
                "loadMore": "false",
            },
            headers={
                "Accept": "application/json",
                "User-Agent": settings.web_search_user_agent,
                "Referer": page.url,
            },
            timeout=settings.web_fetch_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return page

    matches = payload.get("upcomingMatches", [])
    if not isinstance(matches, list) or not matches:
        return page

    lines = [page.text, "Extracted BCCI upcoming fixtures:"]
    for match in matches[:8]:
        if not isinstance(match, dict):
            continue
        name = str(match.get("MatchName") or "").strip()
        date = str(match.get("MatchDateNew") or match.get("MatchDate") or "").strip()
        time_ = str(match.get("MatchTime") or "").strip()
        venue = ", ".join(
            part
            for part in [
                str(match.get("GroundName") or "").strip(),
                str(match.get("city") or "").strip(),
            ]
            if part
        )
        competition = str(match.get("CompetitionName") or "").strip()
        match_type = str(match.get("MatchTypeName") or match.get("MatchType") or "").strip()
        team_type = str(match.get("TeamType") or "").strip()
        lines.append(
            "Upcoming match: "
            f"{name}. Date: {date}. Time: {time_} IST. "
            f"Venue: {venue}. Competition: {competition}. Format: {match_type}. Team type: {team_type}."
        )
    return page.model_copy(update={"text": "\n".join(line for line in lines if line)})


def _candidate_chunks(page: FetchedPage) -> list[str]:
    if "json" in (page.content_type or "") or page.text.lstrip().startswith("{"):
        structured = _json_evidence_chunks(page.text)
        if structured:
            return structured

    line_chunks = [_clean_chunk(line) for line in re.split(r"[\r\n]+", page.text) if _clean_chunk(line)]
    if any("Upcoming match:" in chunk for chunk in line_chunks):
        return [chunk for chunk in line_chunks if chunk.startswith("Upcoming match:")]

    meta_chunks = _meta_and_infobox_chunks(page.text)

    listing_chunks = _listing_evidence_chunks(page.text)
    if listing_chunks:
        return meta_chunks + listing_chunks

    sentences = [
        _clean_chunk(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", page.text)
        if _clean_chunk(sentence)
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for sentence in sentences:
        words = sentence.split()
        if current and current_words + len(words) > 85:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sentence)
        current_words += len(words)
    if current:
        chunks.append(" ".join(current))
    if chunks:
        return meta_chunks + chunks

    words = page.text.split()
    return meta_chunks + [_clean_chunk(" ".join(words[index : index + 85])) for index in range(0, len(words), 65)]


def _meta_and_infobox_chunks(text: str) -> list[str]:
    """Extract META description and INFOBOX lines as high-priority chunks."""
    chunks: list[str] = []
    meta_match = re.match(r"^META:\s*(.+?)(?:\n|$)", text)
    if meta_match:
        chunks.append(f"Page description: {meta_match.group(1).strip()}")
    infobox_match = re.search(r"INFOBOX:\s*(.+?)(?:\n|$)", text)
    if infobox_match:
        chunks.append(f"Infobox: {infobox_match.group(1).strip()}")
    return chunks


def _json_evidence_chunks(text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    name = payload.get("name")
    version = payload.get("version")
    description = payload.get("description")
    homepage = payload.get("homepage")
    if name and version:
        return [
            f"Package {name} latest version: {version}. "
            f"Description: {description or 'No description provided'}. "
            f"Homepage: {homepage or 'No homepage provided'}."
        ]
    return []


def _listing_evidence_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    openai_pattern = re.compile(
        r"(?P<title>[A-Z][^.!?]{20,160}?)\s+"
        r"(?P<section>Product|Research|Company|Applied AI|AI Adoption|Safety|Engineering|Security|Global Affairs)\s+"
        r"(?P<date>[A-Z][a-z]{2}\s+\d{1,2},\s+20\d{2})"
    )
    for match in openai_pattern.finditer(text):
        chunks.append(
            f"{match.group('title').strip()}. Category: {match.group('section')}. Date: {match.group('date')}."
        )
    return chunks


def _score_chunk(profile: QueryRelevanceProfile, answer_mode: str, chunk: str) -> float:
    normalized = _normalize_for_relevance(chunk)
    if _is_boilerplate_chunk(normalized):
        return 0.0
    term_hits = _term_hit_count(profile, chunk)
    alias_hit = _has_alias_hit(profile, chunk)
    score = float(term_hits * 2)
    if alias_hit:
        score += 3.0
    if _has_freshness_hit(chunk):
        score += 2.0
    if re.search(r"\b(?:\d{1,2}\s+[a-z]{3,9}\s+20\d{2}|20\d{2}-\d{2}-\d{2})\b", normalized):
        score += 3.0
    if chunk.startswith("Page description:") or chunk.startswith("Infobox:"):
        score += 4.0
    if answer_mode == "fact_lookup" and re.search(
        r"\b(version|price|cost|upcoming match|match|date|time|venue|schedule|"
        r"season|seasons|episode|episodes|directed|created|written|"
        r"champion|winner|ranking|rated|rating|world chess)\b",
        normalized,
    ):
        score += 3.0
    if answer_mode == "news_summary" and re.search(
        r"\b(news|announced|announces|launch|launched|trailer|release|released|coming|update)\b",
        normalized,
    ):
        score += 2.0
    return score


def _is_boilerplate_chunk(normalized_chunk: str) -> bool:
    boilerplate_terms = (
        "newsletter", "sign in", "subscribe", "privacy notice", "terms of use",
        "skip to content", "membership benefits", "jump to content", "main menu",
        "move to sidebar", "create account", "log in", "personal tools",
        "navigation main page", "community portal", "recent changes",
        "upload file", "special pages", "printable version", "download as pdf",
        "cookie policy", "hide navigation", "search search", "accept all cookies",
        "manage preferences", "site map", "contact us", "about us",
        "follow us", "social media", "share this",
    )
    boilerplate_hits = sum(1 for term in boilerplate_terms if term in normalized_chunk)
    evidence_terms = (
        "date ", "category ", "upcoming match", "latest version", "announced",
        "released", "unveiled", "season", "episode", "premiere", "directed",
        "created", "champion", "winner", "ranking", "rating", "price",
    )
    evidence_hits = sum(1 for term in evidence_terms if term in normalized_chunk)
    return boilerplate_hits >= 2 and evidence_hits == 0


def _clean_chunk(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _has_alias_hit(profile: QueryRelevanceProfile, value: str) -> bool:
    normalized = _normalize_for_relevance(value)
    return any(alias in normalized for alias in profile.aliases)


def _term_hit_count(profile: QueryRelevanceProfile, value: str) -> int:
    normalized = _normalize_for_relevance(value)
    return sum(1 for term in set(profile.terms) if term in normalized)


def _has_freshness_hit(value: str) -> bool:
    normalized = _normalize_for_relevance(value)
    if re.search(r"\b20[2-9][0-9]\b", normalized):
        return True
    freshness_terms = {
        "announced", "announces", "breaking", "current", "latest",
        "launched", "launches", "news", "new", "recent", "release",
        "released", "schedule", "upcoming", "update", "updates", "version",
    }
    return any(term in normalized for term in freshness_terms)


def _normalize_for_relevance(value: str) -> str:
    normalized = value.lower()
    normalized = normalized.replace("next.js", "nextjs")
    normalized = normalized.replace("next js", "nextjs")
    normalized = normalized.replace("open ai", "openai")
    normalized = normalized.replace("spider-man", "spiderman")
    normalized = normalized.replace("spider man", "spiderman")
    normalized = re.sub(r"[^a-z0-9./+#-]+", " ", normalized)
    return normalized


# ---------------------------------------------------------------------------
# Structured fact extractors
# ---------------------------------------------------------------------------

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


class FactResult:
    __slots__ = ("answer", "support_text", "source_index", "confidence", "match_reason")

    def __init__(self, answer: str, support_text: str, source_index: int, confidence: float, match_reason: str):
        self.answer = answer
        self.support_text = support_text
        self.source_index = source_index
        self.confidence = confidence
        self.match_reason = match_reason


def _num(text: str) -> int | None:
    if text.isdigit():
        val = int(text)
        return val if 0 < val < 1000 else None
    return _WORD_TO_NUM.get(text.lower())


def extract_season_count(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(season|seasons|how many)\b", query, re.IGNORECASE):
        return None
    if re.search(r"\b(episode|episodes)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (r"\b(?:consists of|has|have|had|ran for|spanned|comprises|featuring|with)\s+(?P<n>\d{1,2}|" + "|".join(_WORD_TO_NUM) + r")\s+seasons?\b", "verb+count"),
            (r"\b(?P<n>\d{1,2}|" + "|".join(_WORD_TO_NUM) + r")\s+seasons?\b", "count+seasons"),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            n = _num(match.group("n"))
            if n is None or n > 50:
                continue
            context_start = max(0, match.start() - 60)
            support = text[context_start:match.end() + 60].strip()
            confidence = 0.8 if reason == "verb+count" else 0.6
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=f"{n} season{'s' if n != 1 else ''}",
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


def extract_episode_count(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(episode|episodes|how many)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        if re.search(r"\b(first|last|next|remaining|one more|final)\s+\w+\s+episodes\b", text, re.IGNORECASE):
            continue
        for pattern, reason in [
            (r"\b(?:consists of|has|have|with|contains|includes|featuring|totaling)\s+(?P<n>\d{1,3}|" + "|".join(_WORD_TO_NUM) + r")\s+episodes?\b", "verb+count"),
            (r"\b(?P<n>\d{1,3}|" + "|".join(_WORD_TO_NUM) + r")\s+episodes?\b", "count+episodes"),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            n = _num(match.group("n"))
            if n is None or n > 500:
                continue
            context_start = max(0, match.start() - 60)
            support = text[context_start:match.end() + 60].strip()
            confidence = 0.8 if reason == "verb+count" else 0.6
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=f"{n} episode{'s' if n != 1 else ''}",
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


_CHAMPION_TITLE_QUERY = re.compile(
    r"\b(world chess champion|chess world champion|classical.{0,10}champion|"
    r"current.{0,10}champion|reigning.{0,10}champion|undisputed.{0,10}champion)\b",
    re.IGNORECASE,
)
_RATING_QUERY = re.compile(
    r"\b(highest rated|top rated|ranking|rankings|rated|rating|"
    r"world number|number one|no\.\s*1|fide rating|live rating|"
    r"current fide|top chess player)\b",
    re.IGNORECASE,
)
_CHAMPION_TITLE_EVIDENCE = re.compile(
    r"(world chess champion|world champion|current champion|reigning champion|"
    r"undisputed champion|classical champion|became.{0,15}champion|"
    r"won.{0,15}championship|defeated.{0,15}world champion|"
    r"claimed.{0,15}world.{0,10}title|new world champion)",
    re.IGNORECASE,
)
_RATING_ONLY_EVIDENCE = re.compile(
    r"\b(rating|rated|ranking|ranked|elo|live rating|standard rating|"
    r"world ranking|fide rating|classical rating)\b",
    re.IGNORECASE,
)


def extract_champion_or_ranking(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    is_champion_query = bool(_CHAMPION_TITLE_QUERY.search(query))
    is_rating_query = bool(_RATING_QUERY.search(query))
    if not is_champion_query and not is_rating_query:
        if not re.search(r"\b(champion|fide|world chess)\b", query, re.IGNORECASE):
            return None

    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"

        champion_patterns = [
            (r"(?:world chess champion|world champion|current champion|reigning champion|undisputed champion)\s*(?:is|:)?\s*(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", "champion_title"),
            (r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*(?:is the|became|won|defeated|claimed)\s*(?:the\s+)?(?:world|chess)\s*champion", "champion_context"),
            (r"(?:World\s+(?:Chess\s+)?Champion(?:ship)?.*?(?:won by|winner|champion)\s*:?\s*)(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", "championship_winner"),
        ]
        rating_patterns = [
            (r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*\(\s*(?:\d{4}\s*(?:rating|Elo|FIDE)?\s*:?\s*)?\s*(?P<rating>\d{4})\s*\)", "name_with_rating"),
            (r"#1\s+(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", "ranked_number_one"),
            (r"(?:highest[- ]rated|top[- ]rated|number one|#1)[^.]{0,40}(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", "highest_rated"),
        ]

        if is_champion_query:
            patterns = champion_patterns
        elif is_rating_query:
            patterns = rating_patterns + champion_patterns
        else:
            patterns = champion_patterns + rating_patterns

        for pattern, reason in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            name = match.group("name").strip()
            if len(name) < 4 or len(name) > 60:
                continue
            context_start = max(0, match.start() - 100)
            context_end = min(len(text), match.end() + 100)
            support = text[context_start:context_end].strip()

            is_champion_evidence = bool(_CHAMPION_TITLE_EVIDENCE.search(support))
            is_rating_evidence = bool(_RATING_ONLY_EVIDENCE.search(support))

            if is_champion_query and not is_champion_evidence:
                continue
            if is_rating_query and reason in ("champion_title", "champion_context", "championship_winner"):
                if not is_rating_evidence:
                    pass

            confidence = 0.9 if is_champion_evidence else 0.7
            if is_champion_query and not is_champion_evidence:
                continue
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=name,
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


def extract_release_date(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(release|released|releasing|premiere|when|date|coming out|launch)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (r"(?:release(?:d|s)?\s+(?:date|on)?|premiere(?:d|s)?(?:\s+on)?|(?:coming|came)\s+out\s+(?:on)?|launch(?:ed|es)?\s+(?:on)?)\s*:?\s*(?P<date>(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}|20\d{2}-\d{2}-\d{2})", "explicit_date"),
            (r"(?P<date>(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2})", "month_date"),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            date = match.group("date").strip()
            context_start = max(0, match.start() - 60)
            support = text[context_start:match.end() + 60].strip()
            confidence = 0.85 if reason == "explicit_date" else 0.6
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=date,
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


def extract_software_version(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(version|latest|current|newest)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (r"(?:latest version|current version|newest version|version)\s*:?\s*(?:is\s+)?(?:v)?(?P<ver>\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.]+)?)", "explicit_version"),
            (r"Package \S+ latest version:\s*(?P<ver>\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.]+)?)", "npm_version"),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            ver = match.group("ver").strip()
            context_start = max(0, match.start() - 40)
            support = text[context_start:match.end() + 40].strip()
            confidence = 0.9 if reason == "npm_version" else 0.7
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=ver,
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


_PRICE_REJECT_CONTEXT = re.compile(
    r"\b(emi|per month|monthly|cashback|discount|save |exchange|"
    r"down payment|trade.?in|bank offer|instant off|deposit|"
    r"coupon|voucher|no.cost|interest free|installment)\b",
    re.IGNORECASE,
)
_PRICE_ACCEPT_CONTEXT = re.compile(
    r"\b(price|starts?\s+at|starting\s+at|MRP|buy|product price|"
    r"from \u20b9|from \$|costs?|priced at|retail|MSRP|"
    r"MacBook|iPhone|iPad|Galaxy|laptop|desktop|Pro|Air)\b",
    re.IGNORECASE,
)


def _parse_price_amount(price_str: str) -> float | None:
    """Extract numeric value from a price string like '$1,299' or '₹1,34,900'."""
    cleaned = re.sub(r"[^\d.]", "", price_str.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_price(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(price|cost|how much|pricing|starts at|starting at)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (r"(?:price|priced at|starts?\s+at|starting\s+at|from|costs?|MRP)\s*:?\s*(?P<price>[$\u20b9\u00a3\u20ac][\d,]+(?:\.\d{2})?|[\d,]+(?:\.\d{2})?\s*(?:USD|INR|GBP|EUR))", "explicit_price"),
            (r"(?P<price>[$\u20b9\u00a3\u20ac][\d,]+(?:\.\d{2})?)", "currency_symbol"),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            price = match.group("price").strip()
            context_start = max(0, match.start() - 80)
            context_end = min(len(text), match.end() + 80)
            support = text[context_start:context_end].strip()

            if _PRICE_REJECT_CONTEXT.search(support):
                continue

            amount = _parse_price_amount(price)
            if amount is not None:
                is_inr = "\u20b9" in price or "INR" in price
                if is_inr and amount < 20000:
                    continue
                if not is_inr and amount < 50:
                    continue

            has_product_context = bool(_PRICE_ACCEPT_CONTEXT.search(support))
            if reason == "currency_symbol" and not has_product_context:
                continue

            confidence = 0.8 if reason == "explicit_price" and has_product_context else 0.6 if reason == "explicit_price" else 0.4
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=price,
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


ALL_EXTRACTORS = [
    extract_season_count,
    extract_episode_count,
    extract_champion_or_ranking,
    extract_release_date,
    extract_software_version,
    extract_price,
]


def run_extractors(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    """Run all structured extractors and return the best result."""
    best: FactResult | None = None
    for extractor in ALL_EXTRACTORS:
        result = extractor(query, chunks)
        if result is not None and (best is None or result.confidence > best.confidence):
            best = result
    return best
