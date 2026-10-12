"""Answering "what images do you have?" from inside an ordinary chat turn.

Chat mode has no tools, so without this the model is asked about the gallery
with nothing in front of it and says, truthfully, that it has no images. This
module decides whether a message is asking for saved images, runs the gallery's
own search for it, and hands back two things: a block of text the model answers
from, and the items the transcript shows beneath that answer.

The decision follows ``chat_intent.py``: a conservative reading of what kind of
request the message is, never of what it is about. An image word with a
saved-or-earlier cue ("do you have", "I sent you", "last week") or a fetch verb
("show", "find", "pull up") is a request for the gallery. Asking Neo to *make* a
picture, or how to take one, is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.services.gallery import store
from app.services.gallery.search import GallerySearch, _terms

GalleryCue = Literal["explicit", "implied"]

#: How many images a reply carries. A search shows its best few; a request to
#: see the gallery as a whole shows more, newest first, and says how many exist.
SEARCH_LIMIT = 8
BROWSE_LIMIT = 12

#: A search result is shown only when it scores at least this fraction of the
#: best one. The embedder rates every image ~0.5 similar to any query, which is
#: enough to clear the search's absolute floor but not to be the answer.
RELATIVE_CUTOFF = 0.75

_IMAGE_NOUN = (
    r"(?:images?|pictures?|pics?|pix|photos?|photographs?|screenshots?|"
    r"screen\s+shots?|snapshots?)"
)
_HAS_IMAGE_NOUN = re.compile(rf"\b{_IMAGE_NOUN}\b", re.IGNORECASE)
_GALLERY = re.compile(r"\bgaller(?:y|ies)\b", re.IGNORECASE)

#: Something that places the image in the past or in Neo's keeping.
_SAVED_CUE = re.compile(
    r"\b(?:do\s+you\s+have|have\s+you\s+got|you(?:'ve|\s+have)\s+(?:got|saved|stored|kept)|"
    r"saved|stored|kept|uploaded|"
    r"(?:sent|showed|shown|gave|given|shared|uploaded)\s+(?:to\s+)?you|"
    r"i\s+(?:sent|showed|shared|gave|uploaded|added|pasted)|"
    r"earlier|previously|from\s+before|yesterday|ago|"
    r"last\s+(?:week|month|year|time|night))\b",
    re.IGNORECASE,
)

#: A fetch verb aimed at an image: "show me the sword picture", "pull up
#: pictures of my dog". Neo cannot fetch images from anywhere else, so this is
#: as plain a request for the gallery as naming it.
_FETCH_IMAGE = re.compile(
    r"\b(?:show|find|pull\s+up|bring\s+up|display|list|give|send)\s+(?:me\s+)?"
    rf"(?:[\w'-]+\s+){{0,4}}?{_IMAGE_NOUN}\b",
    re.IGNORECASE,
)

#: Looser signs of wanting to look at images. Acted on only when something
#: actually matches what the message describes, so a passing "I can't see the
#: image" never turns into a tour of the gallery.
_LOOSE_CUE = re.compile(
    r"\b(?:see|look\s+(?:up|for|at)|search\s+for)\b|"
    rf"\bmy\s+(?:\w+\s+)?{_IMAGE_NOUN}\b",
    re.IGNORECASE,
)

#: Asking Neo to produce a picture rather than retrieve one.
_CREATE_REQUEST = re.compile(
    r"\b(?:generate|create|draw|make|design|paint|render|produce|sketch)\s+"
    r"(?:me\s+)?(?:an?\s+|the\s+|some\s+)?(?:\w+\s+){0,2}?"
    rf"{_IMAGE_NOUN}\b",
    re.IGNORECASE,
)

#: Questions *about* images as a subject: how to take one, what one is.
_EXPLANATORY = re.compile(
    r"^\s*(?:how\s+(?:do|can|to|would|should|does)|why\b|explain\b)|"
    r"\bshow\s+(?:me\s+)?(?:how|why)\b|"
    rf"\bwhat\s+(?:is|are)\s+(?:an?\s+)?{_IMAGE_NOUN}\b",
    re.IGNORECASE,
)

#: How a request for saved images is phrased, as opposed to what it describes.
#: Removed before searching so "what spiderman images do you have stored in the
#: gallery" is scored on "spiderman" alone. Time phrases are left in place for
#: ``parse_window`` to find.
_REQUEST_FILLER = re.compile(
    r"\b(?:saved|save|stored|store|kept|keep|galler(?:y|ies)|uploaded|upload|"
    r"sent|send|shared|share|showed|shown|gave|given|added|pasted|pull|bring|"
    r"display|see|look|search|open|get|got|give|list|check|tell|know|remember|"
    r"pics?|pix|photographs?|snapshots?|anything|something|everything|"
    r"how\s+many|many|currently|neo)\b",
    re.IGNORECASE,
)


def wants_gallery(prompt: str, *, has_attachments: bool = False) -> GalleryCue | None:
    """Whether *prompt* asks for images Neo already holds, and how plainly.

    ``explicit`` names the gallery, places the image in the past ("the one I
    sent you"), or asks to be shown images outright. Such a turn is answered
    from the gallery even when nothing matches -- "there is none" is the answer.
    ``implied`` is a looser image request, acted on only if something matches.

    A turn that carries its own image is about that image unless the request
    names the gallery or the past, so "show me what's in this photo" keeps its
    attachment.
    """

    text = re.sub(r"\s+", " ", (prompt or "").strip())
    if not text or _CREATE_REQUEST.search(text):
        return None
    has_noun = bool(_HAS_IMAGE_NOUN.search(text))
    if _GALLERY.search(text) or (has_noun and _SAVED_CUE.search(text)):
        return "explicit"
    if has_attachments or not has_noun or _EXPLANATORY.search(text):
        return None
    if _FETCH_IMAGE.search(text):
        return "explicit"
    if _LOOSE_CUE.search(text):
        return "implied"
    return None


@dataclass
class GalleryMatches:
    """What a gallery lookup found, in the two forms the turn needs."""

    items: list[dict]
    total: int
    browsing: bool
    query: str
    cue: GalleryCue
    window_phrase: str | None = None
    appearances: dict[str, list[dict]] = field(default_factory=dict)
    chat_id: int | None = None

    def shown(self) -> list[dict]:
        """The snapshot stored on the reply, which the transcript renders."""

        return [{"id": item["id"], "title": item.get("title") or ""} for item in self.items]

    def prompt_block(self) -> str:
        """What the model answers from. Ids are left out on purpose: the images
        are already on screen, and an id in the reply is noise to the user."""

        holds = (
            "The user's gallery is empty."
            if self.total == 0
            else f"The user's gallery holds {self.total} image{'s' if self.total != 1 else ''}."
        )
        when = f" seen {self.window_phrase}" if self.window_phrase else ""
        if not self.items:
            if self.total == 0:
                outcome = "No image is shown with this reply."
            elif self.query:
                among = f" among images seen {self.window_phrase}" if self.window_phrase else ""
                outcome = (
                    f'It was searched for "{self.query}"{among} and nothing matched, so no '
                    "image is shown with this reply."
                )
            elif self.window_phrase:
                outcome = f"None of them was seen {self.window_phrase}, so no image is shown."
            else:
                outcome = "No other image is in it, so none is shown."
            return f"{holds} {outcome}"
        if self.browsing:
            heading = (
                f"{holds} The user asked to see what is in it. Newest first, "
                f"{len(self.items)} {'are' if len(self.items) != 1 else 'is'} shown"
                f"{when}:"
            )
        else:
            heading = (
                f'{holds} {len(self.items)} matched "{self.query}"{when}, shown in this order:'
            )
        lines = [heading]
        for position, item in enumerate(self.items, start=1):
            lines.append(f"{position}. {self._describe(item)}")
        return "\n".join(lines)

    def _describe(self, item: dict) -> str:
        parts = [item.get("title") or "Untitled image"]
        if item.get("description_status") == "ready" and item.get("caption"):
            parts.append(f"Description: {item['caption']}")
        elif item.get("description_status") != "ready":
            parts.append("Not described yet.")
        if item.get("tags"):
            parts.append(f"Tags: {', '.join(item['tags'])}")
        if item.get("ocr_text"):
            transcript = " ".join(str(item["ocr_text"]).split())[:300]
            parts.append(f"Text in image: {transcript}")
        seen = [a for a in self.appearances.get(item["id"], []) if a.get("seen_at")]
        if seen:
            latest = max(seen, key=lambda a: str(a["seen_at"]))
            where = (
                " in this chat"
                if self.chat_id is not None and latest.get("chat_id") == self.chat_id
                else " in an earlier chat"
                if latest.get("chat_id") is not None
                else ""
            )
            parts.append(f"Last seen {str(latest['seen_at'])[:10]}{where}")
        else:
            parts.append(f"Added {str(item.get('created_at') or '')[:10]}")
        return " | ".join(part for part in parts if part)


def lookup(
    prompt: str,
    *,
    cue: GalleryCue,
    chat_id: int | None = None,
    project_id: str | None = None,
    exclude_ids: list[str] | None = None,
    search: GallerySearch | None = None,
) -> GalleryMatches | None:
    """Search the gallery for what *prompt* describes.

    Returns ``None`` only when the gallery cannot be read; "nothing matched" is
    an answer, and comes back as an empty ``GalleryMatches``. Search results
    must match the words of the request -- in the description, the transcript
    or the tags -- to be shown. A result carried by embedding similarity alone
    is how a "dog" request came back with a superhero, and showing the wrong
    picture is worse than saying there is none.
    """

    excluded = set(exclude_ids or [])
    query = re.sub(r"\s+", " ", _REQUEST_FILLER.sub(" ", prompt or "")).strip()
    try:
        outcome = (search or GallerySearch()).search(
            query,
            chat_id=chat_id,
            project_id=project_id,
            # Wider than what is shown: the word filter below runs after the
            # search's own ranking, and must have the whole field to pick from.
            limit=50 + len(excluded),
        )
        total = store.list_items(limit=1)[1]
    except Exception:
        return None

    terms = _terms(outcome.get("query") or "")
    browsing = not terms
    results = [
        entry for entry in outcome.get("results") or [] if entry["item"]["id"] not in excluded
    ]
    if browsing:
        # Only a plain request gets the whole gallery. A loose cue with nothing
        # left to search for is someone talking about an image, not asking for
        # theirs.
        chosen = results[:BROWSE_LIMIT] if cue == "explicit" else []
    else:
        worded = [
            entry
            for entry in results
            if entry["score_breakdown"].get("lexical", 0) > 0
            or entry["score_breakdown"].get("tags", 0) > 0
        ]
        best = max((entry["score"] for entry in worded), default=0.0)
        chosen = [entry for entry in worded if entry["score"] >= best * RELATIVE_CUTOFF][
            :SEARCH_LIMIT
        ]
    window = outcome.get("window") or {}
    return GalleryMatches(
        items=[entry["item"] for entry in chosen],
        total=int(total),
        browsing=browsing,
        query=" ".join(terms),
        cue=cue,
        window_phrase=window.get("phrase"),
        appearances={entry["item"]["id"]: entry.get("appearances") or [] for entry in chosen},
        chat_id=chat_id,
    )
