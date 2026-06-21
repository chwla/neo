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
    ".7z",
    ".avi",
    ".dmg",
    ".doc",
    ".docx",
    ".exe",
    ".gz",
    ".iso",
    ".mp3",
    ".mp4",
    ".ppt",
    ".pptx",
    ".rar",
    ".tar",
    ".webm",
    ".xls",
    ".xlsx",
    ".zip",
)

MIN_EVIDENCE_CHUNK_SCORE = 5.0


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"article", "br", "div", "h1", "h2", "h3", "li", "main", "p", "section", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas", "template"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"article", "div", "li", "main", "p", "section", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        self.parts.append(cleaned)

    @property
    def text(self) -> str:
        return normalize_text(" ".join(self.parts))

    @property
    def title(self) -> str | None:
        title = normalize_text(" ".join(self.title_parts))
        return title or None


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
        return FetchedPage(
            url=current,
            title=parser.title or current,
            domain=urlparse(current).netloc or domain,
            text=text,
            fetched=bool(text),
            content_type=content_type or "text/html",
            error=None if text else "No readable text found.",
        )

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
        time = str(match.get("MatchTime") or "").strip()
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
            f"{name}. Date: {date}. Time: {time} IST. "
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

    listing_chunks = _listing_evidence_chunks(page.text)
    if listing_chunks:
        return listing_chunks

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
        return chunks

    words = page.text.split()
    return [_clean_chunk(" ".join(words[index : index + 85])) for index in range(0, len(words), 65)]


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
    if answer_mode == "fact_lookup" and re.search(
        r"\b(version|price|cost|upcoming match|match|date|time|venue|schedule)\b",
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
    boilerplate_hits = sum(
        1
        for term in (
            "newsletter",
            "sign in",
            "subscribe",
            "privacy notice",
            "terms of use",
            "skip to content",
            "membership benefits",
        )
        if term in normalized_chunk
    )
    evidence_hits = sum(
        1
        for term in ("date ", "category ", "upcoming match", "latest version", "announced", "released", "unveiled")
        if term in normalized_chunk
    )
    return boilerplate_hits >= 3 and evidence_hits == 0


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
        "announced",
        "announces",
        "breaking",
        "current",
        "latest",
        "launched",
        "launches",
        "news",
        "new",
        "recent",
        "release",
        "released",
        "schedule",
        "upcoming",
        "update",
        "updates",
        "version",
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
