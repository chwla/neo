"""Finding an image by what was in it, when it was seen, and where.

"Find that image I showed you last week where the calendar approval button was
broken" is one query carrying four separate claims. Each is scored on its own and
then fused, because no single one of them finds the picture: the words come from
the transcript, "broken" from the caption, "last week" from when it was seen, and
"showed you" from the fact that it appeared in a conversation at all.

The blend follows ``CanonicalRecallService``: a lexical total assembled from
weighted components, mixed with a semantic score by a single configurable weight,
with the breakdown returned so a bad ranking can be read rather than guessed at.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.config import get_settings
from app.services.gallery import store
from app.services.gallery.timeframe import Window, parse_window, strip_phrase

#: Words that carry no information about which image is wanted. Stripped before
#: substring matching so "that image where the button was broken" is scored on
#: "button" and "broken" rather than rewarded for containing "image".
_STOPWORDS = {
    "a", "an", "and", "that", "the", "this", "was", "were", "what", "when", "where", "which",
    "who", "with", "you", "your", "show", "showed", "shown", "find", "image", "images", "photo",
    "photos", "picture", "pictures", "screenshot", "screenshots", "me", "my", "i", "of", "in",
    "on", "at", "to", "it", "is", "are", "for", "from", "about", "there",
}


def _terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(term) > 1 and term not in _STOPWORDS
    ]


def _age_days(value: str | None, now: datetime) -> float:
    if not value:
        return 3650.0
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return 3650.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp).total_seconds() / 86400.0)


def _recency(item: dict, appearances: list[dict], now: datetime) -> float:
    """Newest of "created" and "last seen", decayed over a year."""

    ages = [_age_days(item.get("created_at"), now)]
    ages.extend(_age_days(a.get("seen_at"), now) for a in appearances)
    return max(0.0, 1.0 - min(min(ages) / 365.0, 1.0))


def _text_match(item: dict, terms: list[str]) -> tuple[float, float]:
    """Substring hits in the words, and in the tags, as two 0-1 scores.

    This runs alongside bm25 rather than instead of it. fts5 is absent in some
    SQLite builds, and a rare term in a short OCR transcript can rank low under
    bm25 while being exactly what the user typed.
    """

    if not terms:
        return 0.0, 0.0
    haystack = " ".join(
        str(item.get(field) or "").lower()
        for field in ("title", "caption", "ocr_text", "alt_text")
    )
    tags = " ".join(item.get("tags") or []).lower()
    hits = sum(1 for term in terms if term in haystack)
    tag_hits = sum(1 for term in terms if term in tags)
    return hits / len(terms), tag_hits / len(terms)


class GallerySearch:
    def __init__(self, vector_index=None) -> None:
        settings = get_settings()
        self.semantic_weight = settings.gallery_semantic_weight
        self.min_score = settings.gallery_min_score
        self._vector_index = vector_index

    def _semantic_scores(self, query: str, limit: int) -> dict[str, float]:
        try:
            index = self._vector_index
            if index is None:
                from app.services.gallery.vectors import GalleryVectorIndex

                index = GalleryVectorIndex()
            return index.search(query, limit=limit)
        except Exception:
            # No embedder, no vectors, no problem: lexical still answers.
            return {}

    def search(
        self,
        query: str,
        *,
        chat_id: int | None = None,
        project_id: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 12,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(UTC)
        window: Window | None = None
        if not since and not until:
            window = parse_window(query, now)
        text = strip_phrase(query, window)
        terms = _terms(text)

        candidates = store.list_items(
            tags=tags,
            since=since or (window.start_iso if window else None),
            until=until or (window.end_iso if window else None),
            limit=store.SEARCH_SCAN_LIMIT,
        )[0]
        if not candidates:
            return {"results": [], "window": self._window_payload(window), "query": text}

        appearances = store.appearances_for([item["id"] for item in candidates])
        lexical_index = store.fts_scores(text) if terms else {}
        semantic_index = self._semantic_scores(text, limit=200) if terms else {}
        browsing = not terms

        results = []
        for item in candidates:
            seen = appearances.get(item["id"], [])
            breakdown = self._score(
                item,
                seen,
                terms=terms,
                lexical=lexical_index.get(item["id"], 0.0),
                semantic=semantic_index.get(item["id"], 0.0),
                chat_id=chat_id,
                project_id=project_id,
                now=now,
            )
            if not browsing:
                # Fail closed. An item that matched neither the words nor their
                # meaning is not a weak answer, it is the wrong picture -- and
                # recency or a pin must not be able to promote it into the
                # results. Returning nothing is the honest outcome.
                if breakdown["lexical"] <= 0 and breakdown["semantic"] <= 0:
                    continue
                if breakdown["score"] < self.min_score:
                    continue
            results.append(
                {
                    "item": item,
                    "appearances": seen,
                    "score": breakdown["score"],
                    "score_breakdown": breakdown,
                }
            )

        key = (
            (lambda entry: (entry["item"]["pinned"], entry["score"]))
            if browsing
            else (lambda entry: entry["score"])
        )
        results.sort(key=key, reverse=True)
        return {
            "results": results[:limit],
            "window": self._window_payload(window),
            "query": text,
            "total_candidates": len(candidates),
        }

    def _score(
        self,
        item: dict,
        appearances: list[dict],
        *,
        terms: list[str],
        lexical: float,
        semantic: float,
        chat_id: int | None,
        project_id: str | None,
        now: datetime,
    ) -> dict[str, float]:
        word_match, tag_match = _text_match(item, terms)
        recency = _recency(item, appearances, now)
        pinned = 1.0 if item.get("pinned") else 0.0
        described = 1.0 if item.get("description_status") == "ready" else 0.0

        scope = 0.0
        if chat_id is not None and any(a.get("chat_id") == chat_id for a in appearances):
            scope = 1.0
        elif project_id and any(a.get("project_id") == project_id for a in appearances):
            scope = 0.6

        # A lexical hit can arrive from bm25 or from a plain substring; the
        # stronger of the two is used so an absent fts5 build does not halve
        # every score.
        lexical_signal = max(lexical, word_match)
        lexical_total = min(
            1.0,
            0.45 * lexical_signal
            + 0.15 * tag_match
            + 0.15 * scope
            + 0.12 * recency
            + 0.08 * described
            + 0.05 * pinned,
        )
        weight = self.semantic_weight
        total = min(1.0, (1 - weight) * lexical_total + weight * semantic)
        return {
            "score": round(total, 4),
            "lexical": round(lexical_signal, 4),
            "semantic": round(semantic, 4),
            "tags": round(tag_match, 4),
            "scope": round(scope, 4),
            "recency": round(recency, 4),
            "described": described,
            "pinned": pinned,
        }

    @staticmethod
    def _window_payload(window: Window | None) -> dict | None:
        if not window:
            return None
        return {
            "start": window.start_iso,
            "end": window.end_iso,
            "phrase": window.phrase,
        }
