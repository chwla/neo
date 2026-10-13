from __future__ import annotations

import io
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from dateutil import parser as date_parser

from app.core.config import get_settings
from app.services.search.security import public_http_addresses
from app.services.search.types import (
    EvidenceChunk,
    FetchedPage,
    PageBlock,
    PageLink,
    QueryRelevanceProfile,
    SearchResult,
)
from app.services.tools.security import pin_addresses

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

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})
_JUNK_TAGS = frozenset({"nav", "header", "footer", "aside"})
_JUNK_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "menu", "menubar"})
_JUNK_CLASSES = re.compile(
    r"(?:^|\s)(nav|navbar|sidebar|side-bar|breadcrumb|cookie-|"
    r"social-share|share-bar|advertisement|ad-container|ads-|promo-|popup|modal|"
    r"skip-link|site-header|site-footer|site-nav|masthead|"
    r"table-of-contents|mw-navigation|mw-panel|mw-header|mw-footer|"
    r"catlinks|printfooter|noprint|navbox|"
    r"top-nav|bottom-nav|main-nav|global-nav|page-header|page-footer)(?:\s|$)",
    re.IGNORECASE,
)
_CONTENT_TAGS = frozenset({"main", "article"})
_CONTENT_CLASSES = re.compile(
    r"\b(mw-body-content|mw-parser-output|mw-content-text|"
    r"article-body|post-content|entry-content|content-area|"
    r"infobox|wikitable)\b",
    re.IGNORECASE,
)


_BLOCK_KINDS = {
    "h1": ("heading", 1),
    "h2": ("heading", 2),
    "h3": ("heading", 3),
    "h4": ("heading", 4),
    "h5": ("heading", 5),
    "h6": ("heading", 6),
    "li": ("list_item", 0),
    "dt": ("list_item", 0),
    "dd": ("list_item", 0),
    "tr": ("table_row", 0),
}
_BLOCK_BOUNDARY_TAGS = frozenset(
    {
        *_BLOCK_KINDS,
        "p",
        "div",
        "section",
        "article",
        "main",
        "blockquote",
        "pre",
        "figcaption",
        "caption",
        "table",
        "ul",
        "ol",
        "dl",
    }
)
_MAX_BLOCKS = 4_000
_MAX_LINKS = 500
_NON_NAVIGABLE_HREF = re.compile(r"^\s*(?:#|javascript:|mailto:|tel:|data:)", re.IGNORECASE)
_PUBLISHED_META = frozenset(
    {
        "article:published_time",
        "og:published_time",
        "datepublished",
        "publish-date",
        "publish_date",
        "pubdate",
        "date",
        "dc.date",
        "dc.date.issued",
        "citation_publication_date",
        "citation_date",
    }
)
_MODIFIED_META = frozenset({"article:modified_time", "og:updated_time", "datemodified"})
_AUTHOR_META = frozenset({"author", "article:author", "citation_author", "dc.creator"})
_SITE_NAME_META = frozenset({"og:site_name", "application-name"})


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
        self._junk_tag_stack: list[str] = []
        self._skip_tag_stack: list[str] = []
        self._content_tag_stack: list[str] = []
        # Structure and metadata, collected alongside the flattened text.
        self.canonical_href: str | None = None
        self.site_name: str | None = None
        self.published: str | None = None
        self.modified: str | None = None
        self.author: str | None = None
        self.lang: str | None = None
        self.citation_meta: dict[str, str] = {}
        self.time_values: list[str] = []
        self.jsonld_sources: list[str] = []
        self._jsonld_active = False
        self._jsonld_parts: list[str] = []
        self._raw_blocks: list[dict] = []
        self._block_parts: list[str] = []
        self._block_kind = "paragraph"
        self._block_level = 0
        self._block_link_chars = 0
        self._link_depth = 0
        self._headings: list[tuple[int, str]] = []
        # Links in readable content: the anchor being read, and the ones in the current block.
        self._raw_links: list[dict] = []
        self._open_link: dict | None = None
        self._block_link_ids: list[int] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_dict = dict(attrs) if attrs else {}

        if tag == "html" and attr_dict.get("lang"):
            self.lang = str(attr_dict.get("lang"))[:20]
        if tag == "link" and "canonical" in (attr_dict.get("rel") or "").lower().split():
            self.canonical_href = attr_dict.get("href") or self.canonical_href
            return
        if tag == "time" and attr_dict.get("datetime"):
            self.time_values.append(str(attr_dict.get("datetime")))

        if tag == "meta":
            name = (attr_dict.get("name") or "").lower()
            prop = (attr_dict.get("property") or "").lower()
            itemprop = (attr_dict.get("itemprop") or "").lower()
            content = attr_dict.get("content", "")
            if name == "description" and content:
                self.meta_description = content
            if prop == "og:title" and content:
                self.og_title = content
            if prop == "og:description" and content:
                self.og_description = content
            if content:
                self._record_meta({name, prop, itemprop} - {""}, content)
            return

        if tag == "script" and "ld+json" in (attr_dict.get("type") or "").lower():
            self._jsonld_active = True
            self._jsonld_parts = []

        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            self._skip_tag_stack.append(tag)
            return

        role = (attr_dict.get("role") or "").lower()
        cls = attr_dict.get("class") or ""
        aria_label = (attr_dict.get("aria-label") or "").lower()

        is_junk = (
            tag in _JUNK_TAGS
            or role in _JUNK_ROLES
            or _JUNK_CLASSES.search(cls)
            or aria_label
            in {"navigation", "site navigation", "main navigation", "footer", "sidebar"}
        )
        if is_junk:
            if not self._junk_depth:
                self._flush_block()
            self._junk_depth += 1
            self._junk_tag_stack.append(tag)
            return

        is_content = tag in _CONTENT_TAGS or _CONTENT_CLASSES.search(cls)
        if is_content:
            self._content_depth += 1
            self._content_tag_stack.append(tag)

        if "infobox" in cls.lower():
            self._infobox_depth += 1

        if tag == "title":
            self._in_title = True
        if not self._junk_depth and not self._skip_depth:
            if tag == "a":
                self._link_depth += 1
                href = attr_dict.get("href") or ""
                if href and not _NON_NAVIGABLE_HREF.match(href) and self._open_link is None:
                    self._open_link = {"href": href.strip(), "text": []}
            if tag in _BLOCK_BOUNDARY_TAGS:
                self._flush_block()
                self._block_kind, self._block_level = _BLOCK_KINDS.get(tag, ("paragraph", 0))
            elif tag in {"td", "th"} and self._block_parts:
                self._block_parts.append("|")
        if tag in {
            "article",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "li",
            "main",
            "p",
            "section",
            "tr",
            "td",
            "th",
            "dt",
            "dd",
        }:
            target = self._active_list()
            target.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._jsonld_active:
            self._jsonld_active = False
            if len(self.jsonld_sources) < 10:
                self.jsonld_sources.append("".join(self._jsonld_parts)[:200_000])
            self._jsonld_parts = []
        if self._skip_depth and self._skip_tag_stack and self._skip_tag_stack[-1] == tag:
            self._skip_depth -= 1
            self._skip_tag_stack.pop()
            return
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._junk_depth and self._junk_tag_stack and self._junk_tag_stack[-1] == tag:
            self._junk_depth -= 1
            self._junk_tag_stack.pop()
            return
        if not self._junk_depth and not self._skip_depth:
            if tag == "a" and self._link_depth:
                self._link_depth -= 1
                self._close_link()
            if tag in _BLOCK_BOUNDARY_TAGS:
                self._flush_block()
        if self._content_depth and self._content_tag_stack and self._content_tag_stack[-1] == tag:
            self._content_depth -= 1
            self._content_tag_stack.pop()
        if tag == "title":
            self._in_title = False
        if tag in {"article", "div", "li", "main", "p", "section", "tr", "table"}:
            target = self._active_list()
            target.append("\n")
        if self._infobox_depth and tag in {"table", "div"}:
            self._infobox_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._jsonld_active:
            self._jsonld_parts.append(data)
            return
        if self._skip_depth or self._junk_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
            return_title_only = True
        else:
            return_title_only = False
        target = self._active_list()
        target.append(cleaned)
        if self._infobox_depth:
            self._infobox_parts.append(cleaned)
        if not return_title_only:
            self._block_parts.append(cleaned)
            if self._link_depth:
                self._block_link_chars += len(cleaned) + 1
            if self._open_link is not None:
                self._open_link["text"].append(cleaned)

    def close(self) -> None:
        super().close()
        self._flush_block()

    def _record_meta(self, keys: set[str], content: str) -> None:
        value = normalize_text(content)[:500]
        if not value:
            return
        for key in keys:
            if key in _SITE_NAME_META and not self.site_name:
                self.site_name = value
            elif key in _PUBLISHED_META and not self.published:
                self.published = value
            elif key in _MODIFIED_META and not self.modified:
                self.modified = value
            elif key in _AUTHOR_META and not self.author:
                self.author = value
            if key.startswith("citation_") and key not in self.citation_meta:
                self.citation_meta[key] = value

    def _close_link(self) -> None:
        link, self._open_link = self._open_link, None
        if link is None or len(self._raw_links) >= _MAX_LINKS:
            return
        self._raw_links.append(
            {"href": link["href"], "text": normalize_text(" ".join(link["text"]))[:200]}
        )
        self._block_link_ids.append(len(self._raw_links) - 1)

    def _flush_block(self) -> None:
        link_ids, self._block_link_ids = self._block_link_ids, []
        if not self._block_parts:
            return
        text = normalize_text(" ".join(self._block_parts))
        link_chars = self._block_link_chars
        self._block_parts = []
        self._block_link_chars = 0
        kind, level = self._block_kind, self._block_level
        self._block_kind, self._block_level = "paragraph", 0
        if not text or len(self._raw_blocks) >= _MAX_BLOCKS:
            return
        for link_id in link_ids:
            self._raw_links[link_id]["ordinal"] = len(self._raw_blocks)
        if kind == "heading":
            self._headings = [item for item in self._headings if item[0] < level]
            path = [item[1] for item in self._headings]
            self._headings.append((level, text[:200]))
        else:
            path = [item[1] for item in self._headings]
        self._raw_blocks.append(
            {
                "kind": kind,
                "text": text,
                "level": level,
                "heading_path": path,
                "link_chars": min(link_chars, len(text)),
                "in_content": bool(self._content_depth),
            }
        )

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

    def _chosen_ordinals(self) -> list[int]:
        """Raw blocks kept as the page's blocks: the content region when it has substance."""
        content = [i for i, block in enumerate(self._raw_blocks) if block["in_content"]]
        if sum(len(self._raw_blocks[i]["text"]) for i in content) >= 200:
            return content
        return list(range(len(self._raw_blocks)))

    def links(self, base_url: str) -> list[PageLink]:
        position = {ordinal: index for index, ordinal in enumerate(self._chosen_ordinals())}
        links: list[PageLink] = []
        seen: set[tuple[str, int | None]] = set()
        for raw in self._raw_links:
            href = urljoin(base_url, raw["href"])
            if urlparse(href).scheme not in {"http", "https"}:
                continue
            block_index = position.get(raw.get("ordinal"))
            if (href, block_index) in seen:
                continue
            seen.add((href, block_index))
            links.append(PageLink(href=href, text=raw["text"], block_index=block_index))
        return links

    @property
    def blocks(self) -> list[PageBlock]:
        chosen = [self._raw_blocks[i] for i in self._chosen_ordinals()]
        return [
            PageBlock(
                kind=block["kind"],
                text=block["text"],
                level=block["level"],
                heading_path=block["heading_path"],
                link_chars=block["link_chars"],
            )
            for block in chosen
        ]

    def structured_metadata(self) -> dict:
        """Schema.org JSON-LD facts: types, organization/site names, dates, author."""
        types: list[str] = []
        organizations: list[str] = []
        found: dict[str, str] = {}

        def text_of(value) -> str | None:
            if isinstance(value, str):
                return normalize_text(value)[:300] or None
            if isinstance(value, dict) and isinstance(value.get("name"), str):
                return normalize_text(value["name"])[:300] or None
            if isinstance(value, list) and value:
                return text_of(value[0])
            return None

        def walk(node, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, list):
                for item in node[:50]:
                    walk(item, depth + 1)
                return
            if not isinstance(node, dict):
                return
            raw_type = node.get("@type")
            names = [item for item in (raw_type if isinstance(raw_type, list) else [raw_type])]
            names = [str(item) for item in names if isinstance(item, str)]
            types.extend(names)
            if any("Organization" in item or item == "WebSite" for item in names):
                name = text_of(node.get("name"))
                if name:
                    organizations.append(name)
            publisher = text_of(node.get("publisher"))
            if publisher:
                organizations.append(publisher)
            for key in ("datePublished", "dateModified", "author"):
                value = text_of(node.get(key))
                if value and key not in found:
                    found[key] = value
            for key in ("@graph", "mainEntity", "isPartOf", "hasPart"):
                if key in node:
                    walk(node[key], depth + 1)

        for source in self.jsonld_sources:
            try:
                walk(json.loads(source.strip()))
            except (ValueError, TypeError, RecursionError):
                continue
        return {
            "types": list(dict.fromkeys(types))[:20],
            "organizations": list(dict.fromkeys(organizations))[:10],
            "published": found.get("datePublished"),
            "modified": found.get("dateModified"),
            "author": found.get("author"),
        }


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.year < 1000 or parsed.year > datetime.now().year + 1:
        return None
    return parsed.date().isoformat()


def normalize_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def untrusted_context_message(content: str, source: str) -> str:
    return f"<untrusted_web_context source={source!r}>\n{content}\n</untrusted_web_context>"


# ---------------------------------------------------------------------------
# Source-specific content cleaning
# ---------------------------------------------------------------------------


def _clean_article_text(text: str, page: FetchedPage) -> str:
    """Keep the body of an encyclopedia-style article.

    Drops everything from the first reference-style heading on, the "[edit]"
    markers next to section titles, very short lines, and licence boilerplate.
    """
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
        if re.search(r"(Creative Commons|GNU Free|Retrieved from)", stripped):
            continue
        cleaned.append(stripped)
    result = " ".join(cleaned)
    infobox_match = re.search(r"INFOBOX:\s*(.+?)(?:\n|$)", text)
    if infobox_match:
        result = f"INFOBOX: {infobox_match.group(1).strip()}\n{result}"
    return result


def _apply_source_cleanup(text: str, page: FetchedPage) -> str:
    """Clean pages by their structure, never by which site they come from.

    Article pages with "[edit]" section markers get their reference sections
    and markers removed; every other page is left as extracted.
    """
    if re.search(r"\[\s*edit\s*\]", text, re.IGNORECASE):
        return _clean_article_text(text, page)
    return text


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

PDF_CONTENT_TYPE = "application/pdf"
_OCTET_STREAM = frozenset({"application/octet-stream", "binary/octet-stream"})
_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})?(\d{2})?")
_PAGE_NUMBER_LINE = re.compile(r"^\W*\d{1,4}\W*$")


def parse_pdf_document(
    url: str,
    data: bytes,
    *,
    max_pages: int = 60,
    max_seconds: float = 20.0,
    http_status: int | None = None,
) -> FetchedPage:
    """Readable text of a PDF as page blocks, with its metadata and link annotations.

    Lines that repeat on several pages are running headers or footers and are dropped, as are
    bare page numbers. Reading stops at ``max_pages`` or ``max_seconds``; the page records
    that it was cut short. A PDF with no text layer (a scan) is reported as unreadable.
    """
    from pypdf import PdfReader

    domain = urlparse(url).netloc or "unknown"
    base = {"url": url, "domain": domain, "content_type": PDF_CONTENT_TYPE, "document_kind": "pdf"}
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = list(reader.pages)
    except Exception as exc:  # malformed or protected documents are one page's problem
        return FetchedPage(**base, error=f"Unreadable PDF ({exc.__class__.__name__}).")

    started = time.monotonic()
    page_lines: list[list[str]] = []
    links: list[PageLink] = []
    truncated = len(pages) > max_pages
    for page in pages[:max_pages]:
        if time.monotonic() - started > max_seconds:
            truncated = True
            break
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
        raw = re.sub(r"(\w)-\n(\w)", r"\1\2", raw)
        page_lines.append([normalize_text(line) for line in raw.split("\n") if line.strip()])
        links.extend(_pdf_links(page))

    repeated = Counter(line for lines in page_lines for line in set(lines) if len(line) < 120)
    running = {line for line, count in repeated.items() if count >= 3}
    blocks: list[PageBlock] = []
    for lines in page_lines:
        kept = [line for line in lines if line not in running and not _PAGE_NUMBER_LINE.match(line)]
        text = normalize_text(" ".join(kept))
        if text:
            blocks.append(PageBlock(kind="paragraph", text=text))

    metadata = _pdf_metadata(reader)
    first_line = next((line for lines in page_lines for line in lines if len(line) > 8), "")
    text = "\n".join(block.text for block in blocks)
    return FetchedPage(
        **base,
        title=metadata.get("title") or first_line[:200] or url,
        text=text,
        fetched=bool(text),
        error=None if text else "No extractable text in PDF (it may be a scanned image).",
        http_status=http_status,
        blocks=blocks,
        author=metadata.get("author"),
        published_date=metadata.get("created"),
        modified_date=metadata.get("modified"),
        links=links[:_MAX_LINKS],
        truncated=truncated,
    )


def _pdf_metadata(reader) -> dict[str, str]:
    try:
        info = reader.metadata or {}
    except Exception:
        return {}
    found: dict[str, str] = {}
    for key, name in (("/Title", "title"), ("/Author", "author")):
        value = normalize_text(str(info.get(key) or ""))[:300]
        if value:
            found[name] = value
    for key, name in (("/CreationDate", "created"), ("/ModDate", "modified")):
        match = _PDF_DATE.match(str(info.get(key) or ""))
        if match:
            year, month, day = match.group(1), match.group(2) or "01", match.group(3) or "01"
            if (date := _normalize_date(f"{year}-{month}-{day}")) is not None:
                found[name] = date
    return found


def _pdf_links(page) -> list[PageLink]:
    links: list[PageLink] = []
    try:
        annotations = page.get("/Annots") or []
        for annotation in annotations:
            action = (annotation.get_object() or {}).get("/A") or {}
            uri = action.get("/URI") if hasattr(action, "get") else None
            if isinstance(uri, str) and urlparse(uri).scheme in {"http", "https"}:
                links.append(PageLink(href=uri))
    except Exception:
        return links
    return links


# ---------------------------------------------------------------------------
# Page fetcher
# ---------------------------------------------------------------------------


class WebPageFetcher:
    def __init__(
        self,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
        user_agent: str | None = None,
        *,
        read_documents: bool = False,
        max_document_bytes: int = 20_000_000,
        max_document_pages: int = 60,
        truncate_oversized: bool = False,
    ) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.max_bytes = max_bytes or settings.web_fetch_max_bytes
        self.user_agent = user_agent or settings.web_search_user_agent
        #: Read PDFs as well as web pages. Research needs this: original papers, filings,
        #: statutes and datasheets are often published only as PDFs.
        self.read_documents = read_documents
        self.max_document_bytes = max_document_bytes
        self.max_document_pages = max_document_pages
        #: Keep the beginning of an over-long page instead of discarding the whole page.
        self.truncate_oversized = truncate_oversized

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
                approved = self._approved_addresses(current)
                if approved is None:
                    return FetchedPage(
                        url=current,
                        domain=urlparse(current).netloc or domain,
                        error="Blocked unsafe redirect.",
                    )
                # Search results are attacker-influenced, so this path is at least as
                # exposed to DNS rebinding as user-configured connectors: connect only
                # to the addresses this URL was just validated against.
                host = (urlparse(current).hostname or "").rstrip(".").lower()
                with pin_addresses(host, approved):
                    response = requests.get(
                        current,
                        headers={
                            "User-Agent": self.user_agent,
                            "Accept": (
                                "text/html,application/xhtml+xml,text/plain,application/json"
                                + (",application/pdf" if self.read_documents else "")
                            ),
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
                return FetchedPage(
                    url=current,
                    domain=urlparse(current).netloc or domain,
                    error="Too many redirects.",
                )
            if response is None:
                return FetchedPage(url=url, domain=domain, error="Fetch failed.")
            response.raise_for_status()
        except requests.Timeout:
            return FetchedPage(
                url=current, domain=urlparse(current).netloc or domain, error="Fetch timed out."
            )
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return FetchedPage(
                url=current,
                domain=urlparse(current).netloc or domain,
                error=f"Fetch failed: {exc}",
                http_status=status if isinstance(status, int) else None,
            )

        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
        is_pdf = self.read_documents and (
            content_type == PDF_CONTENT_TYPE
            or (content_type in _OCTET_STREAM and urlparse(current).path.lower().endswith(".pdf"))
        )
        if content_type and content_type not in SUPPORTED_CONTENT_TYPES and not is_pdf:
            response.close()
            return FetchedPage(
                url=current,
                domain=urlparse(current).netloc or domain,
                content_type=content_type,
                error=f"Unsupported content type: {content_type}",
            )

        body = bytearray()
        truncated = False
        limit = self.max_document_bytes if is_pdf else self.max_bytes
        try:
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > limit:
                    # A cut-off PDF cannot be parsed; a cut-off web page still reads fine.
                    if is_pdf or not self.truncate_oversized:
                        return FetchedPage(
                            url=current,
                            domain=urlparse(current).netloc or domain,
                            content_type=content_type,
                            error="Page exceeded configured maximum size.",
                        )
                    del body[limit:]
                    truncated = True
                    break
        finally:
            response.close()

        if is_pdf:
            return parse_pdf_document(
                current,
                bytes(body),
                max_pages=self.max_document_pages,
                http_status=getattr(response, "status_code", None),
            )

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

        page = parse_html_page(
            current,
            raw,
            content_type=content_type or "text/html",
            http_status=getattr(response, "status_code", None),
        )
        return page.model_copy(update={"truncated": True}) if truncated else page

    def _allowed_url(self, url: str) -> bool:
        return self._approved_addresses(url) is not None

    def _approved_addresses(self, url: str):
        """Validated public addresses for ``url``, or ``None`` when it must not be fetched.

        One resolution serves both the decision and the pin, so there is no window in
        which the address that was approved differs from the one connected to.
        """

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        path = parsed.path.lower()
        if path.endswith(UNSUPPORTED_EXTENSIONS):
            return None
        return public_http_addresses(url)


def parse_html_page(
    url: str,
    raw: str,
    *,
    content_type: str = "text/html",
    http_status: int | None = None,
) -> FetchedPage:
    """Build a page from HTML already fetched: flattened text plus structure and metadata."""
    domain = urlparse(url).netloc or "unknown"
    parser = _ReadableHTMLParser()
    parser.feed(raw)
    parser.close()
    text = parser.text
    structured = parser.structured_metadata()
    canonical = urljoin(url, parser.canonical_href) if parser.canonical_href else None
    page = FetchedPage(
        url=url,
        title=parser.title or url,
        domain=domain,
        text=text,
        fetched=bool(text),
        content_type=content_type,
        error=None if text else "No readable text found.",
        http_status=http_status,
        blocks=parser.blocks,
        canonical_url=canonical,
        site_name=parser.site_name,
        organization_names=structured["organizations"],
        schema_types=structured["types"],
        scholarly=bool(parser.citation_meta)
        or any("ScholarlyArticle" in item for item in structured["types"]),
        published_date=_normalize_date(
            parser.published
            or structured["published"]
            or (parser.time_values[0] if parser.time_values else None)
        ),
        modified_date=_normalize_date(parser.modified or structured["modified"]),
        author=parser.author or structured["author"],
        lang=parser.lang,
        links=parser.links(url),
        citation_meta={
            key.removeprefix("citation_"): value for key, value in parser.citation_meta.items()
        },
    )
    if text:
        cleaned = _apply_source_cleanup(text, page)
        if cleaned:
            page = page.model_copy(update={"text": cleaned})
        if parser.description:
            page = page.model_copy(update={"text": f"META: {parser.description}\n{page.text}"})
    return page


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
                page = FetchedPage(
                    url=result.url, title=result.title, domain=result.source, error=str(exc)
                )
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


def _candidate_chunks(page: FetchedPage) -> list[str]:
    if "json" in (page.content_type or "") or page.text.lstrip().startswith("{"):
        structured = _json_evidence_chunks(page.text)
        if structured:
            return structured

    line_chunks = [
        _clean_chunk(line) for line in re.split(r"[\r\n]+", page.text) if _clean_chunk(line)
    ]
    if any("Upcoming match:" in chunk for chunk in line_chunks):
        return [chunk for chunk in line_chunks if chunk.startswith("Upcoming match:")]

    meta_chunks = _meta_and_infobox_chunks(page.text)

    listing_chunks: list[str] = []
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
    return meta_chunks + [
        _clean_chunk(" ".join(words[index : index + 85])) for index in range(0, len(words), 65)
    ]


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


#: What evidence looks like regardless of topic: a figure, a quantity with a
#: unit, a dotted version number, a year or a date.
_EVIDENCE_SHAPE = re.compile(
    r"(?:[$\u20ac\u00a3\u20b9\u00a5]\s?\d|\b\d+(?:\.\d+)+\b|\b\d{1,3}(?:,\d{3})+\b|"
    r"\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?\s?(?:%|percent|km|kg|mph|gb|tb|mb|hours?|"
    r"minutes?|days?|weeks?|months?|years?|million|billion)\b|"
    r"\b\d{1,2}\s+[a-z]{3,9}\s+20\d{2}\b|\b20\d{2}-\d{2}-\d{2}\b)",
    re.IGNORECASE,
)


def _score_chunk(profile: QueryRelevanceProfile, answer_mode: str, chunk: str) -> float:
    normalized = _normalize_for_relevance(chunk)
    term_hits = _term_hit_count(profile, chunk)
    if _is_boilerplate_chunk(normalized, term_hits=term_hits):
        return 0.0
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
    # A single fact is usually a figure, a quantity, a version or a date, in
    # whatever domain; a chunk that carries one is more likely to answer it.
    if answer_mode == "fact_lookup" and _EVIDENCE_SHAPE.search(chunk):
        score += 3.0
    if answer_mode == "news_summary" and re.search(
        r"\b(news|announced|announces|launch|launched|release|released|coming|update)\b",
        normalized,
    ):
        score += 2.0
    return score


def _is_boilerplate_chunk(normalized_chunk: str, *, term_hits: int = 0) -> bool:
    boilerplate_terms = (
        "newsletter",
        "sign in",
        "subscribe",
        "privacy notice",
        "terms of use",
        "skip to content",
        "membership benefits",
        "jump to content",
        "main menu",
        "move to sidebar",
        "create account",
        "log in",
        "personal tools",
        "navigation main page",
        "community portal",
        "recent changes",
        "upload file",
        "special pages",
        "printable version",
        "download as pdf",
        "cookie policy",
        "hide navigation",
        "search search",
        "accept all cookies",
        "manage preferences",
        "site map",
        "contact us",
        "about us",
        "follow us",
        "social media",
        "share this",
    )
    boilerplate_hits = sum(1 for term in boilerplate_terms if term in normalized_chunk)
    # Navigation text that still names the subject or carries a figure or date
    # may hold the answer; only navigation with neither is discarded.
    has_evidence = term_hits > 0 or bool(_EVIDENCE_SHAPE.search(normalized_chunk))
    return boilerplate_hits >= 2 and not has_evidence


def _clean_chunk(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _has_alias_hit(profile: QueryRelevanceProfile, value: str) -> bool:
    normalized = _normalize_for_relevance(value)
    if any(alias in normalized for alias in profile.aliases):
        return True
    squashed = _compact(value)
    return any(
        len(form) >= 4 and form in squashed for form in (_compact(a) for a in profile.aliases)
    )


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


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
    normalized = re.sub(r"[^a-z0-9./+#-]+", " ", normalized)
    return normalized


# ---------------------------------------------------------------------------
# Structured fact extractors
# ---------------------------------------------------------------------------


class FactResult:
    __slots__ = ("answer", "support_text", "source_index", "confidence", "match_reason")

    def __init__(
        self,
        answer: str,
        support_text: str,
        source_index: int,
        confidence: float,
        match_reason: str,
    ):
        self.answer = answer
        self.support_text = support_text
        self.source_index = source_index
        self.confidence = confidence
        self.match_reason = match_reason


def extract_release_date(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(
        r"\b(release|released|releasing|premiere|when|date|coming out|launch)\b",
        query,
        re.IGNORECASE,
    ):
        return None
    candidates: list[tuple[str, str, EvidenceChunk, str]] = []
    for chunk in chunks:
        text = chunk.text
        pattern = (
            r"(?:"
            r"(?:is\s+|will\s+be\s+|scheduled\s+to\s+be\s+)?"
            r"release(?:d|s|ing)?(?:\s+date)?(?:\s+on|\s+in)?|"
            r"(?:is\s+|will\s+be\s+)?premiere(?:d|s|ing)?(?:\s+on|\s+in)?|"
            r"(?:is\s+)?(?:coming|came)\s+out(?:\s+on|\s+in)?|"
            r"(?:is\s+|will\s+be\s+)?launch(?:ed|es|ing)?(?:\s+on|\s+in)?|"
            r"(?:in|into)\s+(?:[A-Z][a-z]+\s+)?(?:theatres|theaters|cinemas)(?:\s+on)?"
            r")\s*:?\s*"
            r"(?P<date>"
            r"(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},?\s+20\d{2}|"
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+20\d{2}|"
            r"20\d{2}-\d{2}-\d{2}"
            r")"
        )
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if _is_publication_date_match(text, match.start(), match.start("date")):
                continue
            context_start = max(0, match.start() - 100)
            support = text[context_start : match.end() + 80].strip()
            if not _release_entity_matches(query, f"{chunk.source_title} {support}"):
                continue
            date = match.group("date").strip()
            candidates.append((date, _normalized_release_date(date), chunk, support))

    official = [
        candidate for candidate in candidates if _is_primary_release_source(query, candidate[2])
    ]
    if official:
        date, _, chunk, support = max(official, key=lambda item: item[2].relevance_score)
        return FactResult(
            answer=date,
            support_text=support,
            source_index=chunk.source_index or 1,
            confidence=0.98,
            match_reason="official_explicit_release_date",
        )

    # Otherwise a date counts only when independent sites -- different
    # registrable domains -- state the same one.
    by_date: dict[str, list[tuple[str, str, EvidenceChunk, str]]] = {}
    for candidate in candidates:
        by_date.setdefault(candidate[1], []).append(candidate)
    corroborated = [
        rows for rows in by_date.values() if len({_site_of(row[2].source_url) for row in rows}) >= 2
    ]
    if not corroborated:
        return None
    rows = max(corroborated, key=lambda items: max(item[2].relevance_score for item in items))
    date, _, chunk, support = max(rows, key=lambda item: item[2].relevance_score)
    return FactResult(
        answer=date,
        support_text=support,
        source_index=chunk.source_index or 1,
        confidence=0.92,
        match_reason="corroborated_release_date",
    )


def _normalized_release_date(value: str) -> str:
    normalized = re.sub(r"[\s,]+", " ", value).strip()
    for date_format in ("%B %d %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, date_format).date().isoformat()
        except ValueError:
            continue
    return normalized.lower()


def _is_publication_date_match(text: str, trigger_start: int, date_start: int) -> bool:
    """Reject page/article metadata that merely resembles release syntax."""

    before_trigger = text[max(0, trigger_start - 60) : trigger_start].lower()
    before_date = text[max(0, date_start - 80) : date_start].lower()
    if re.search(
        r"\b(?:news|press|media|article|blog|post|announcement|update)\s+$",
        before_trigger,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:published|posted|updated|publication date|article date)\b",
            before_date,
        )
    )


def _release_entity_matches(query: str, evidence: str) -> bool:
    normalized_query = query.lower()
    normalized_evidence = evidence.lower()
    squashed_evidence = re.sub(r"[^a-z0-9]+", "", normalized_evidence)
    ignored = {
        "are",
        "coming",
        "date",
        "did",
        "does",
        "game",
        "going",
        "has",
        "have",
        "how",
        "is",
        "launch",
        "movie",
        "next",
        "official",
        "premiere",
        "release",
        "released",
        "releasing",
        "scheduled",
        "set",
        "the",
        "what",
        "when",
        "which",
        "will",
        "was",
        "were",
    }
    terms = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9'-]*", normalized_query)
        if len(token) > 2 and token not in ignored
    ]
    if not terms:
        return False
    required = 2 if len(terms) >= 2 else 1
    return (
        sum(
            1
            for term in set(terms)
            if term in normalized_evidence or re.sub(r"[^a-z0-9]+", "", term) in squashed_evidence
        )
        >= required
    )


def _site_of(url: str) -> str:
    from app.services.research.textutil import host_group, host_of

    return host_group(host_of(url))


def _is_primary_release_source(query: str, chunk: EvidenceChunk) -> bool:
    """The subject's own site: its name contains a name the user wrote."""

    from app.services.search.ranking import _entity_terms

    site = re.sub(r"[^a-z0-9]+", "", _site_of(chunk.source_url))
    return any(len(term) >= 3 and term in site for term in _entity_terms(query))


def extract_software_version(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    if not re.search(r"\b(version|latest|current|newest)\b", query, re.IGNORECASE):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (
                r"(?:latest version|current version|newest version|version)"
                r"\s*:?\s*(?:is\s+)?(?:v)?"
                r"(?P<ver>\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.]+)?)",
                "explicit_version",
            ),
            (
                r"Package \S+ latest version:\s*(?P<ver>\d+\.\d+(?:\.\d+)?(?:-[a-z0-9.]+)?)",
                "npm_version",
            ),
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            ver = match.group("ver").strip()
            context_start = max(0, match.start() - 40)
            support = text[context_start : match.end() + 40].strip()
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
    r"from \u20b9|from \$|costs?|priced at|retail|MSRP|sells? for|listed at)\b",
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
    if not re.search(
        r"\b(price|cost|how much|pricing|starts at|starting at)\b", query, re.IGNORECASE
    ):
        return None
    best: FactResult | None = None
    for chunk in chunks:
        text = f"{chunk.source_title}. {chunk.text}"
        for pattern, reason in [
            (
                r"(?:price|priced at|starts?\s+at|starting\s+at|from|costs?|"
                r"MRP)\s*:?\s*(?P<price>[$\u20b9\u00a3\u20ac][\d,]+"
                r"(?:\.\d{2})?|[\d,]+(?:\.\d{2})?\s*(?:USD|INR|GBP|EUR))",
                "explicit_price",
            ),
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
            if amount is None or amount <= 0:
                continue

            has_product_context = bool(_PRICE_ACCEPT_CONTEXT.search(support))
            if reason == "currency_symbol" and not has_product_context:
                continue

            confidence = (
                0.8
                if reason == "explicit_price" and has_product_context
                else 0.6
                if reason == "explicit_price"
                else 0.4
            )
            if best is None or confidence > best.confidence:
                best = FactResult(
                    answer=price,
                    support_text=support,
                    source_index=chunk.source_index or 1,
                    confidence=confidence,
                    match_reason=reason,
                )
    return best


def extract_weather(query: str, chunks: list[EvidenceChunk]) -> FactResult | None:
    """Extract a current temperature only from an explicit location forecast sentence."""

    if not re.search(
        r"\b(weather|forecast|temperature|how hot|how cold)\b",
        query,
        re.IGNORECASE,
    ):
        return None
    location_match = re.search(
        r"\b(?:weather|forecast|temperature)\s+(?:in|for|at)\s+"
        r"(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)(?:\s+(?:today|now|tomorrow))?[?.!]*$",
        query.strip(),
        re.IGNORECASE,
    )
    if location_match is None:
        return None
    location = location_match.group("location").strip()
    location_aliases = [location]
    if location.lower() == "bengaluru":
        location_aliases.append("Bangalore")
    condition_pattern = (
        r"partly\s+cloudy|mostly\s+cloudy|mostly\s+sunny|"
        r"sunny|clear|cloudy|overcast|rain(?:y)?|showers?|"
        r"thunderstorms?|snow(?:y)?|fog(?:gy)?|haze|windy"
    )
    for chunk in chunks:
        text = re.sub(r"\s+", " ", f"{chunk.source_title}. {chunk.text}")
        matched_alias = next(
            (alias for alias in location_aliases if alias.lower() in text.lower()),
            None,
        )
        if matched_alias is None:
            continue
        match = re.search(
            rf"\b(?:today(?:'s)?\s+weather\s+in\s+{re.escape(matched_alias)}|"
            rf"weather\s+(?:today\s+)?in\s+{re.escape(matched_alias)}|"
            rf"{re.escape(matched_alias)}(?:'s)?\s+weather)"
            rf".{{0,100}}?(?P<temperature>-?\d{{1,2}})\s*°\s*(?:[CF]\s*)?"
            rf"(?P<condition>{condition_pattern})\b",
            text,
            re.IGNORECASE,
        )
        if match is not None:
            temperature = int(match.group("temperature"))
            if not -60 <= temperature <= 60:
                continue
            condition = re.sub(r"\s+", " ", match.group("condition")).lower()
            answer = f"{location} is {temperature}°C and {condition} today"
            low_match = re.search(
                r"\bLow\s+(?P<low>-?\d{1,2})\s*°",
                text[match.start() : match.end() + 120],
                re.IGNORECASE,
            )
            if low_match is not None:
                answer += f", with a reported low of {int(low_match.group('low'))}°C"
            return FactResult(
                answer=answer,
                support_text=match.group(0),
                source_index=chunk.source_index or 1,
                confidence=0.92,
                match_reason="current_weather",
            )
        compact_match = re.search(
            rf"\b(?:{'|'.join(re.escape(alias) for alias in location_aliases)})\s+"
            r"(?P<temperature>-?\d{1,2})\s*°\s*C\b"
            r"(?:\s+(?P<aqi>\d{1,3})\s+AQI\b)?",
            text,
            re.IGNORECASE,
        )
        if compact_match is None:
            continue
        temperature = int(compact_match.group("temperature"))
        if not -60 <= temperature <= 60:
            continue
        answer = f"{location} is {temperature}°C today"
        if compact_match.group("aqi"):
            answer += f", with a reported AQI of {int(compact_match.group('aqi'))}"
        return FactResult(
            answer=answer,
            support_text=compact_match.group(0),
            source_index=chunk.source_index or 1,
            confidence=0.86,
            match_reason="current_weather_compact",
        )
    return None


#: Typed extractors for single facts that look the same in any domain: a
#: temperature, a release date, a version number, a price.
ALL_EXTRACTORS = [
    extract_weather,
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
