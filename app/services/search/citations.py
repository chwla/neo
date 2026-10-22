from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.services.source_citations import SourceCitation

#: One marker, or a group such as [1, 2] -- models write both, and the rest of
#: the chat pipeline already reads groups (``_referenced_citation_indices``).
_MARKER = re.compile(r"\[(?P<group>\d+(?:\s*,\s*\d+)*)]")
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
    used = list(
        dict.fromkeys(
            int(number)
            for marker in markers
            for number in re.findall(r"\d+", marker.group("group"))
        )
    )
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
        # A marker belongs to the text before it. Anything that can end a claim
        # counts -- a word, a closing bracket or quote, markdown emphasis, or
        # another marker ("[1][2]") -- so only a marker with no claim before it
        # (the start of the answer, or after an opening bracket) is orphaned.
        if not re.search(r"[^\s(\[](?:[.!?,;:]|\s)*$", prefix):
            errors.append(f"Orphaned citation marker: [{marker.group('group')}].")
            break
    return CitationValidationResult(
        answer=cleaned,
        valid=not errors,
        used_indices=used,
        errors=errors,
    )


#: A run of this many consecutive words found verbatim in a source is worth flagging.
_VERBATIM_RUN = 8
_QUOTED_SPAN = re.compile(r"[\"“]([^\"”]{12,300})[\"”]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


class CitationAudit(BaseModel):
    """What the structural citation check found, and the answer with any fix applied.

    Only structural faults change the answer: a marker that no shown source has, a source
    whose identity differs between the citation list and the passages shown (a pipeline
    fault, so its markers are removed rather than trusted), and markers written against an
    earlier evidence pack, which are carried over by source identity. Text overlap never
    changes a citation: two sources can share a long phrase and state different claims, so
    overlap only marks a sentence for review.
    """

    body: str
    unknown: list[int] = Field(default_factory=list)
    mismatched: list[int] = Field(default_factory=list)
    translated: dict[int, int] = Field(default_factory=dict)
    stripped: list[int] = Field(default_factory=list)
    incomplete_cited: list[int] = Field(default_factory=list)
    suspicious: list[dict[str, object]] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.unknown or self.mismatched or self.translated or self.stripped)

    def summary(self) -> dict[str, object]:
        return {
            "unknown": self.unknown,
            "mismatched": self.mismatched,
            "translated": {str(k): v for k, v in self.translated.items()},
            "stripped": self.stripped,
            "incomplete_cited": self.incomplete_cited,
            "suspicious": self.suspicious,
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def audit_citations(
    body: str,
    citations: list[SourceCitation],
    chunks: list,
    *,
    generated_with: list[SourceCitation] | None = None,
) -> CitationAudit:
    """Check every marker in ``body`` against the sources it can refer to.

    ``citations`` and ``chunks`` are the evidence the answer is being validated against.
    ``generated_with`` is the citation list of the pack the answer was written from, when
    that differs: markers are then mapped through each source's identity to its number in
    the current list, and a marker whose source is not in the current list is removed.
    """

    current = {citation.index: citation for citation in citations}
    passage_ids: dict[int, set[str]] = {}
    for chunk in chunks:
        if getattr(chunk, "source_id", None):
            passage_ids.setdefault(int(chunk.source_index), set()).add(chunk.source_id)
    mismatched = sorted(
        index
        for index, citation in current.items()
        if citation.source_id
        and passage_ids.get(index)
        and citation.source_id not in passage_ids[index]
    )
    by_identity = {
        citation.source_id: citation.index for citation in citations if citation.source_id
    }
    translation: dict[int, int | None] = {}
    if generated_with is not None:
        for earlier in generated_with:
            target = by_identity.get(earlier.source_id) if earlier.source_id else None
            if target != earlier.index:
                translation[earlier.index] = target

    unknown: list[int] = []
    stripped: list[int] = []
    translated: dict[int, int] = {}

    def rewrite(match: re.Match) -> str:
        kept: list[int] = []
        for number in (int(n) for n in re.findall(r"\d+", match.group("group"))):
            if number in translation:
                target = translation[number]
                if target is None:
                    stripped.append(number)
                    continue
                translated[number] = target
                number = target
            if number not in current:
                unknown.append(number)
                continue
            if number in mismatched:
                stripped.append(number)
                continue
            if number not in kept:
                kept.append(number)
        return f"[{', '.join(str(n) for n in kept)}]" if kept else ""

    cleaned = _MARKER.sub(rewrite, body)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned) if cleaned != body else cleaned
    cleaned = re.sub(r" {2,}", " ", cleaned)

    passages: dict[int, str] = {}
    for chunk in chunks:
        index = int(chunk.source_index)
        passages[index] = f"{passages.get(index, '')} {_norm(getattr(chunk, 'text', ''))}"
    incomplete = sorted(
        {
            int(n)
            for group in _MARKER.findall(cleaned)
            for n in re.findall(r"\d+", group)
            if int(n) in current and current[int(n)].incomplete
        }
    )
    suspicious: list[dict[str, object]] = []
    for sentence in _SENTENCE_SPLIT.split(cleaned):
        cited = {int(n) for group in _MARKER.findall(sentence) for n in re.findall(r"\d+", group)}
        if not cited:
            continue
        spans = [_norm(span) for span in _QUOTED_SPAN.findall(sentence)]
        words = _norm(_MARKER.sub("", sentence)).split()
        spans.extend(
            " ".join(words[i : i + _VERBATIM_RUN])
            for i in range(0, max(0, len(words) - _VERBATIM_RUN + 1))
        )
        for span in spans:
            if not span:
                continue
            holders = sorted(index for index, text in passages.items() if span in text)
            if holders and not set(holders) & cited:
                suspicious.append(
                    {"sentence": sentence[:240], "cited": sorted(cited), "also_in": holders}
                )
                break
    return CitationAudit(
        body=cleaned,
        unknown=sorted(set(unknown)),
        mismatched=mismatched,
        translated=translated,
        stripped=sorted(set(stripped)),
        incomplete_cited=incomplete,
        suspicious=suspicious,
    )
