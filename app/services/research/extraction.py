"""Turn fetched pages into content chunks, dropping page chrome and detecting copies.

Boilerplate is recognised by how it is built, not by what it says: blocks that are mostly
link text, long runs of capitalised words with no sentence punctuation, and blocks that
repeat across several pages of the same site. Nothing here knows about any particular site.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.services.research.textutil import normalize_ws, shingles, text_hash
from app.services.research.types import Chunk, RejectionReason
from app.services.search.types import FetchedPage, PageBlock

TARGET_WORDS = 150
MAX_WORDS = 230
MIN_CHUNK_WORDS = 4
MIN_CONTENT_WORDS = 50
LINK_DENSITY_LIMIT = 0.6
DUPLICATE_CONTAINMENT = 0.8
MIN_SHINGLES_FOR_DUPLICATE = 25

_SENTENCE_PUNCTUATION = re.compile(r"[.!?;:,]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class PageExtraction:
    chunks: list[Chunk] = field(default_factory=list)
    quality: float = 0.0
    content_words: int = 0
    boilerplate_blocks: int = 0
    rejection: RejectionReason | None = None
    fingerprint: set[int] = field(default_factory=set)


def page_blocks(page: FetchedPage) -> list[PageBlock]:
    if page.blocks:
        return page.blocks
    text = page.text or ""
    parts = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    if len(parts) <= 1:
        sentences = _SENTENCE_SPLIT.split(normalize_ws(text))
        parts = [" ".join(sentences[i : i + 5]) for i in range(0, len(sentences), 5)]
    return [PageBlock(kind="paragraph", text=part) for part in parts if part]


def repeated_block_hashes(pages: list[FetchedPage]) -> set[str]:
    """Hashes of blocks that appear on two or more pages of one site: site-wide chrome."""
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update(
            {
                text_hash(block.text)
                for block in page_blocks(page)
                if block.kind != "heading" and len(block.text) >= 20
            }
        )
    return {digest for digest, count in counts.items() if count >= 2}


def is_menu_like(block: PageBlock) -> bool:
    if block.kind != "paragraph":
        return False
    words = block.text.split()
    if len(words) < 8 or _SENTENCE_PUNCTUATION.search(block.text):
        return False
    capitalised = sum(1 for word in words if word[:1].isupper() or word[:1].isdigit())
    return capitalised / len(words) >= 0.7


def is_short_line_list(text: str) -> bool:
    """A chunk made mostly of very short, unpunctuated lines: menus and link lists.

    Lines with digits or table separators are not counted as short, so spec and
    price tables survive.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 5:
        return False
    short = sum(
        1
        for line in lines
        if len(line.split()) <= 3
        and not _SENTENCE_PUNCTUATION.search(line)
        and "|" not in line
        and not any(char.isdigit() for char in line)
    )
    return short / len(lines) >= 0.7


def block_role(block: PageBlock, repeated: set[str]) -> str:
    if block.kind == "heading":
        return "heading"
    if text_hash(block.text) in repeated:
        return "boilerplate"
    if block.text and block.link_chars / len(block.text) >= LINK_DENSITY_LIMIT:
        return "boilerplate"
    if is_menu_like(block):
        return "boilerplate"
    return "content"


def extract_chunks(source_id: str, page: FetchedPage, repeated: set[str]) -> PageExtraction:
    blocks = page_blocks(page)
    result = PageExtraction()
    content_chars = 0
    boilerplate_chars = 0
    pending: list[tuple[int, str]] = []
    pending_words = 0
    pending_path: list[str] | None = None

    def flush() -> None:
        nonlocal pending, pending_words
        if not pending:
            return
        text = "\n".join(piece for _, piece in pending)
        words = len(text.split())
        previous = result.chunks[-1] if result.chunks else None
        if (
            words < 12
            and previous is not None
            and previous.heading_path == (pending_path or [])
            and len(previous.text.split()) + words <= MAX_WORDS + 40
        ):
            previous.text = f"{previous.text}\n{text}"
            previous.block_end = pending[-1][0]
            previous.text_hash = text_hash(previous.text)
        elif words >= MIN_CHUNK_WORDS:
            result.chunks.append(
                Chunk(
                    id=f"{source_id}-c{len(result.chunks) + 1}",
                    source_id=source_id,
                    text=text,
                    heading_path=list(pending_path or []),
                    block_start=pending[0][0],
                    block_end=pending[-1][0],
                    text_hash=text_hash(text),
                )
            )
        pending = []
        pending_words = 0

    for index, block in enumerate(blocks):
        role = block_role(block, repeated)
        if role == "heading":
            flush()
            continue
        if role == "boilerplate":
            boilerplate_chars += len(block.text)
            result.boilerplate_blocks += 1
            continue
        content_chars += len(block.text)
        result.content_words += len(block.text.split())
        if pending and block.heading_path != pending_path:
            flush()
        pending_path = block.heading_path
        for piece in _split_long(block.text):
            words = len(piece.split())
            if pending and pending_words + words > MAX_WORDS:
                flush()
                pending_path = block.heading_path
            pending.append((index, piece))
            pending_words += words
            if pending_words >= TARGET_WORDS:
                flush()
                pending_path = block.heading_path
    flush()

    kept: list[Chunk] = []
    for chunk in result.chunks:
        if is_short_line_list(chunk.text):
            result.boilerplate_blocks += 1
            boilerplate_chars += len(chunk.text)
            content_chars -= len(chunk.text)
            result.content_words -= len(chunk.text.split())
            continue
        kept.append(chunk)
    result.chunks = kept
    result.quality = round(content_chars / max(1, content_chars + boilerplate_chars), 3)
    if result.content_words < MIN_CONTENT_WORDS:
        result.rejection = RejectionReason.INSUFFICIENT_CONTENT
    elif result.quality < 0.2 and result.content_words < 200:
        result.rejection = RejectionReason.POOR_EXTRACTION
    result.fingerprint = shingles(" ".join(chunk.text for chunk in result.chunks))
    return result


def near_duplicate_pairs(fingerprints: dict[str, set[int]]) -> list[tuple[str, str, float]]:
    """Pairs of sources where one is largely a copy of the other (mirrors, syndication)."""
    ids = [key for key, value in fingerprints.items() if len(value) >= MIN_SHINGLES_FOR_DUPLICATE]
    pairs: list[tuple[str, str, float]] = []
    for position, left in enumerate(ids):
        for right in ids[position + 1 :]:
            a, b = fingerprints[left], fingerprints[right]
            containment = len(a & b) / min(len(a), len(b))
            if containment >= DUPLICATE_CONTAINMENT:
                pairs.append((left, right, round(containment, 3)))
    return pairs


def _split_long(text: str) -> list[str]:
    words = text.split()
    if len(words) <= MAX_WORDS:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in _SENTENCE_SPLIT.split(text):
        size = len(sentence.split())
        if current and count + size > TARGET_WORDS:
            pieces.append(" ".join(current))
            current, count = [], 0
        if size > MAX_WORDS:
            sentence_words = sentence.split()
            for start in range(0, len(sentence_words), TARGET_WORDS):
                pieces.append(" ".join(sentence_words[start : start + TARGET_WORDS]))
            continue
        current.append(sentence)
        count += size
    if current:
        pieces.append(" ".join(current))
    return pieces
