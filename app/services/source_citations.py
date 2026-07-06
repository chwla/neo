from __future__ import annotations

from pydantic import BaseModel, Field


class SourceCitation(BaseModel):
    index: int
    title: str
    url: str
    source: str
    fetched: bool = False


class CitationFormatter:
    """Formats source citations and keeps citation indices stable."""

    def citations_for_fetched_pages(self, pages) -> list[SourceCitation]:
        citations: list[SourceCitation] = []
        for page in pages:
            if not page.fetched or not page.text:
                continue
            citations.append(
                SourceCitation(
                    index=len(citations) + 1,
                    title=page.title or page.url,
                    url=page.url,
                    source=page.domain,
                    fetched=True,
                ),
            )
        return citations

    def format_citations(self, citations: list[SourceCitation]) -> str:
        if not citations:
            return ""
        lines = ["Sources:"]
        lines.extend(
            f"[{citation.index}] {citation.title} — {citation.url}" for citation in citations
        )
        return "\n".join(lines)


class CitedAnswer(BaseModel):
    answer: str
    citations: list[SourceCitation] = Field(default_factory=list)
    used_web: bool = False
    warning: str | None = None
