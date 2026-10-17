"""What the note list needs to know about a note, without reading the note.

A row in the list shows a one-line preview, a gallery card shows a few lines, and
smart folders ask whether a note has checklists, pictures or links. All of that
is derived here once, when content is written, and stored beside the note, so a
list of two thousand notes never loads two thousand bodies.

Two sources are understood. A rich note's document is the editor's own JSON and
is exact. A Markdown note (legacy, or written through the API) is scanned line by
line, outside code fences, which is a close approximation and never more than
that: it only decides what the list shows, never what the note contains.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

PREVIEW_LENGTH = 180
EXCERPT_LENGTH = 600
EXCERPT_LINES = 12

#: Workspace files are served from one route, and that is how a Markdown body
#: points at an attachment.
_FILE_URL = re.compile(r"/api/files/([0-9a-fA-F-]{36})/download")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TASK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\](?:\s|$)")
_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)")
_BARE_URL = re.compile(r"(?:https?://|mailto:)\S+", re.IGNORECASE)
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
_NBSP = re.compile(r"&nbsp;| ")


@dataclass(frozen=True)
class Derived:
    checklist_total: int = 0
    checklist_done: int = 0
    has_images: bool = False
    has_files: bool = False
    has_sketches: bool = False
    has_tables: bool = False
    has_links: bool = False
    cover_file_id: str | None = None
    preview: str = ""
    excerpt: str = ""
    attachment_file_ids: tuple[str, ...] = ()

    def columns(self) -> dict[str, Any]:
        """The values stored on the note row (attachment ids live elsewhere)."""

        values = asdict(self)
        values.pop("attachment_file_ids")
        for key in ("has_images", "has_files", "has_sketches", "has_tables", "has_links"):
            values[key] = 1 if values[key] else 0
        return values


EMPTY = Derived()


def strip_inline_markdown(line: str) -> str:
    """One line as a reader would see it, with the punctuation of Markdown gone."""

    text = _NBSP.sub(" ", line)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"^\s*>\s?", "", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+\[[ xX]\]\s*", "", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "Image", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"==(.+?)==", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    if "|" in text and text.strip().startswith("|"):
        text = " ".join(cell.strip() for cell in text.strip().strip("|").split("|") if cell.strip())
    return re.sub(r"\s+", " ", text).strip()


def _readable_lines(body: str) -> list[str]:
    lines: list[str] = []
    fence: str | None = None
    for raw in body.splitlines():
        match = _FENCE.match(raw)
        if match:
            marker = match.group(1)[0]
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is not None:
            stripped = raw.strip()
            if stripped:
                lines.append(stripped)
            continue
        if _TABLE_DELIMITER.match(raw) or re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", raw):
            continue
        text = strip_inline_markdown(raw)
        if text:
            lines.append(text)
    return lines


def _preview_and_excerpt(body: str) -> tuple[str, str]:
    lines = _readable_lines(body)
    preview = " ".join(lines)
    if len(preview) > PREVIEW_LENGTH:
        preview = preview[:PREVIEW_LENGTH].rstrip() + "…"
    excerpt_lines: list[str] = []
    used = 0
    for line in lines[:EXCERPT_LINES]:
        if used + len(line) > EXCERPT_LENGTH:
            excerpt_lines.append(line[: max(0, EXCERPT_LENGTH - used)].rstrip() + "…")
            break
        excerpt_lines.append(line)
        used += len(line) + 1
    return preview, "\n".join(excerpt_lines)


def derive_from_markdown(body: str) -> Derived:
    total = done = 0
    has_images = has_files = has_tables = has_links = False
    cover: str | None = None
    attachments: list[str] = []
    fence: str | None = None
    previous = ""
    for raw in body.splitlines():
        match = _FENCE.match(raw)
        if match:
            marker = match.group(1)[0]
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is not None:
            continue
        task = _TASK.match(raw)
        if task:
            total += 1
            done += task.group(1) in ("x", "X")
        if _TABLE_DELIMITER.match(raw) and "|" in previous:
            has_tables = True
        for image in _IMAGE.findall(raw):
            has_images = True
            file_match = _FILE_URL.search(image)
            if file_match:
                attachments.append(file_match.group(1))
                cover = cover or file_match.group(1)
        for target in _LINK.findall(raw):
            file_match = _FILE_URL.search(target)
            if file_match:
                has_files = True
                attachments.append(file_match.group(1))
            elif re.match(r"(?:https?://|mailto:)", target, re.IGNORECASE):
                has_links = True
        if not has_links and _BARE_URL.search(_IMAGE.sub("", raw)):
            has_links = True
        previous = raw
    preview, excerpt = _preview_and_excerpt(body)
    return Derived(
        checklist_total=total,
        checklist_done=done,
        has_images=has_images,
        has_files=has_files,
        has_tables=has_tables,
        has_links=has_links,
        cover_file_id=cover,
        preview=preview,
        excerpt=excerpt,
        attachment_file_ids=tuple(dict.fromkeys(attachments)),
    )


def derive_from_doc(doc: dict[str, Any] | None, body: str = "") -> Derived:
    """Exact facts from the editor's document; the preview comes from its Markdown."""

    if not isinstance(doc, dict):
        return derive_from_markdown(body)
    total = done = 0
    flags = {"images": False, "files": False, "sketches": False, "tables": False, "links": False}
    cover: str | None = None
    attachments: list[str] = []

    stack: list[Any] = [doc]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if kind == "taskItem":
            total += 1
            done += bool(attrs.get("checked"))
        elif kind == "image":
            flags["images"] = True
            file_id = attrs.get("fileId")
            # An image read back from Markdown has only its address.
            if not (isinstance(file_id, str) and file_id):
                found = _FILE_URL.search(str(attrs.get("src") or ""))
                file_id = found.group(1) if found else None
            if isinstance(file_id, str) and file_id:
                attachments.append(file_id)
                cover = cover or file_id
        elif kind == "fileAttachment":
            flags["files"] = True
            file_id = attrs.get("fileId")
            if isinstance(file_id, str) and file_id:
                attachments.append(file_id)
        elif kind == "sketch":
            flags["sketches"] = True
        elif kind == "table":
            flags["tables"] = True
        for mark in node.get("marks") or []:
            if isinstance(mark, dict) and mark.get("type") == "link":
                flags["links"] = True
        children = node.get("content")
        if isinstance(children, list):
            # Reversed so the first image in reading order is popped first.
            stack.extend(reversed(children))

    preview, excerpt = _preview_and_excerpt(body)
    return Derived(
        checklist_total=total,
        checklist_done=done,
        has_images=flags["images"],
        has_files=flags["files"],
        has_sketches=flags["sketches"],
        has_tables=flags["tables"],
        has_links=flags["links"],
        cover_file_id=cover,
        preview=preview,
        excerpt=excerpt,
        attachment_file_ids=tuple(dict.fromkeys(attachments)),
    )


def snippet(text: str, query: str, radius: int = 60) -> str | None:
    """A short window of ``text`` around the first match of ``query``."""

    if not text or not query:
        return None
    lines = _readable_lines(text)
    plain = " ".join(lines)
    index = plain.lower().find(query.lower())
    if index < 0:
        return None
    start = max(0, index - radius)
    end = min(len(plain), index + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(plain) else ""
    return f"{prefix}{plain[start:end].strip()}{suffix}"
