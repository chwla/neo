from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.services.source_citations import SourceCitation

_MARKER = re.compile(r"\[(?P<index>\d+)]")
_SOURCES_HEADER = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:sources?|references?)\s*:",
    re.IGNORECASE,
)
_INLINE_SOURCES_HEADER = re.compile(
    r"\s*\[\s*(?:sources?|references?)\s*:",
    re.IGNORECASE,
)


class CitationValidationResult(BaseModel):
    answer: str
    valid: bool
    used_indices: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def strip_generated_sources_block(answer: str) -> str:
    """Remove only model-generated source/reference blocks.

    The backend appends its own verified source list after validation.
    """

    cut = len(answer)
    for pattern in (_SOURCES_HEADER, _INLINE_SOURCES_HEADER):
        match = pattern.search(answer)
        if match is not None:
            cut = min(cut, match.start())
    return answer[:cut].rstrip()


def validate_citation_markers(
    answer: str,
    citations: list[SourceCitation],
    *,
    supported_indices: set[int] | None = None,
    require_marker: bool = True,
) -> CitationValidationResult:
    cleaned = strip_generated_sources_block(answer)
    markers = list(_MARKER.finditer(cleaned))
    used = list(dict.fromkeys(int(marker.group("index")) for marker in markers))
    available = {citation.index for citation in citations if citation.fetched}
    supported = available if supported_indices is None else available & supported_indices
    errors: list[str] = []

    if require_marker and citations and not used:
        errors.append("The answer has no verified citation markers.")
    unknown = sorted(set(used) - available)
    if unknown:
        errors.append(f"Unknown citation indices: {unknown}.")
    unsupported = sorted(set(used) - supported)
    if unsupported:
        errors.append(f"Unsupported citation indices: {unsupported}.")
    for marker in markers:
        prefix = cleaned[max(0, marker.start() - 120) : marker.start()]
        if not re.search(r"[A-Za-z0-9)](?:[.!?,;:]|\s)*$", prefix):
            errors.append(f"Orphaned citation marker: [{marker.group('index')}].")
            break
    return CitationValidationResult(
        answer=cleaned,
        valid=not errors,
        used_indices=used,
        errors=errors,
    )
