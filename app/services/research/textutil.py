"""Small, topic-agnostic text and URL helpers shared by the research stages."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dateutil import parser as date_parser

# Function words of English. Linguistic, not topical: they carry no subject matter.
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers him his how i if in into is
    it its itself just me more most my no nor not now of off on once only or other our out
    over own same she should so some such than that the their them then there these they
    this those through to too under until up very was we were what when where which while
    who whom why will with would you your yours between versus vs via per etc
    """.split()
)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*(?:[.-][a-z0-9+#]+)*")
#: Symbols a token may carry that set a short name apart from the letter it is written with.
_NAME_SYMBOLS = "+#"
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "spm",
        "_hsenc",
        "_hsmi",
    }
)
_SECOND_LEVEL_LABELS = frozenset(
    {"co", "com", "ac", "gov", "edu", "org", "net", "or", "ne", "go", "gob", "gouv", "mil", "nic"}
)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def tokens(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN.findall((text or "").lower())
        if token not in _STOPWORDS and (len(token) > 1 or token.isdigit())
    ]


_SENTENCE_END = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")


def split_sentences(text: str) -> list[str]:
    """Sentence-sized units of a chunk: its lines, each split after sentence punctuation.

    Lines without sentence punctuation (list items, table rows, headings) stay whole. The units
    are what the evidence judge cites by number, so an excerpt is rebuilt from the chunk's own
    words rather than copied out by a model.
    """
    units: list[str] = []
    for line in (text or "").split("\n"):
        for piece in _SENTENCE_END.split(line.strip()):
            piece = normalize_ws(piece)
            if piece:
                units.append(piece)
    return units


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def jaccard(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def name_forms(names: list[str]) -> set[str]:
    """Separator-insensitive forms of a set of names, ignoring forms too short to match.

    A name whose letters are too few to find inside run-together text still has a form when
    symbols that prose never attaches to a word make it distinctive (a letter followed by
    "#" or "++"). That form keeps its symbols and is matched as a whole word by
    :func:`mentions_any`, since stripping them would leave a letter that every text contains.
    """
    forms: set[str] = set()
    for name in names:
        form = compact(name)
        if len(form) >= 3:
            forms.add(form)
            continue
        written = re.sub(r"\s+", "", (name or "").lower())
        if any(symbol in written for symbol in _NAME_SYMBOLS) and _TOKEN.fullmatch(written):
            forms.add(written)
    return forms


def mentions_any(text: str, forms: set[str]) -> bool:
    haystack = compact(text)
    lowered: str | None = None
    for form in forms:
        if form.isalnum():
            if form in haystack:
                return True
            continue
        lowered = (text or "").lower() if lowered is None else lowered
        if _written_form(form).search(lowered):
            return True
    return False


@lru_cache(maxsize=256)
def _written_form(form: str) -> re.Pattern[str]:
    # No letter, digit or symbol attached before it and no letter or symbol after it, so a
    # version number may follow but a longer word may not.
    return re.compile(rf"(?<![a-z0-9+#]){re.escape(form)}(?![a-z+#])")


_WORD = re.compile(r"[A-Za-z0-9]+")
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9&]{1,7}s?\b")


def _singular(form: str) -> str:
    return form[:-1] if len(form) > 4 and form.endswith("s") else form


def names_entity(text: str, names: list[str], other_names: list[str]) -> bool | None:
    """Whether ``text`` refers to an entity by one of its names; None when code cannot tell.

    A name counts when its compact form appears, singular or plural. A two-character name
    counts only when written exactly as the name is, so an ordinary word is not mistaken for
    it. A word of the name that no other entity's names share also counts. When nothing
    matches but the text holds an acronym that could stand for the name, or a name is a
    single character, the answer is None rather than a guess.
    """
    pairs = [(name, compact(name)) for name in names if compact(name)]
    if not pairs or any(len(form) < 2 for _, form in pairs):
        return None
    haystack = compact(text)
    words = set(_WORD.findall(text or ""))
    for name, form in pairs:
        if len(form) >= 3 and _singular(form) in haystack:
            return True
        written = "".join(_WORD.findall(name))
        if len(form) == 2 and (written in words or f"{written}s" in words):
            return True
    shared = {_singular(token) for name in other_names for token in tokens(name)}
    distinctive = {
        _singular(token)
        for name, _ in pairs
        for token in tokens(name)
        if len(token) >= 4 and not token.isdigit()
    } - shared
    if distinctive & {_singular(token) for token in tokens(text)}:
        return True
    initials = {
        "".join(part[0] for part in parts).upper()
        for name, _ in pairs
        if len(parts := _WORD.findall(name)) >= 2
    }
    acronyms = _ACRONYM.findall(text or "")
    if any(initial in acronym for acronym in acronyms for initial in initials):
        return None
    return False


#: What separates the items of a list, a tag cloud or a navigation trail.
_ITEM_BREAK = re.compile(r"\s*(?:[,;|\u2022\u00b7\u203a\u00bb\n]|\s[/>\u2013\u2014-]\s)\s*")
#: A label introducing a value, as metadata writes "Published: ..." or "Keywords: ...".
_LABEL = re.compile(r"(?:^|(?<=\s))[A-Z][A-Za-z]*(?:\s[A-Za-z]+){0,2}:(?=\s|$)")
_LINKING = frozenset({"and", "or", "nor", "but"})


def is_propositional(text: str) -> bool:
    """Whether text states something about what it names, rather than only naming things.

    Structural, so it holds in any subject: keyword lists and tag clouds are runs of short
    items with no connecting clause, navigation trails and titles are strings of capitalised
    words without a sentence's end, and metadata is labels with short values. A sentence that
    enumerates ("symptoms include a, b and c") keeps its linking word and still passes.
    """
    value = normalize_ws(text)
    words = _WORD.findall(value)
    if len(words) < 3:
        return False
    items = [item for item in _ITEM_BREAK.split(value) if _WORD.search(item)]
    item_words = [_WORD.findall(item) for item in items]
    if len(items) >= 4 and max(map(len, item_words)) <= 4:
        linked = any(part[0].lower() in _LINKING for part in item_words[1:])
        if not linked:
            return False
    labels = _LABEL.findall(value)
    if len(labels) >= 2:
        values = [part for part in _LABEL.split(value) if _WORD.search(part)]
        # Metadata values are names, dates and identifiers; a clause has lowercase words.
        if not any(sum(word[0].islower() for word in _WORD.findall(part)) >= 2 for part in values):
            return False
    alphabetic = [word for word in words if word[0].isalpha()]
    if (
        len(alphabetic) >= 3
        and not re.search(r"[.!?:][\"')\]]*$", value)
        and sum(word[0].isupper() for word in alphabetic) >= 0.6 * len(alphabetic)
    ):
        return False
    return True


def grounding_form(text: str) -> str:
    """Whitespace- and typography-insensitive form used to prove an excerpt is verbatim."""
    value = unicodedata.normalize("NFKC", text or "")
    value = value.translate(
        {
            0x2018: "'",
            0x2019: "'",
            0x201C: '"',
            0x201D: '"',
            0x2013: "-",
            0x2014: "-",
            0x2212: "-",
            0x00A0: " ",
            0x2026: "...",
        }
    )
    return normalize_ws(value)


def is_grounded(excerpt: str, source_text: str, *, min_chars: int = 12) -> bool:
    candidate = grounding_form(excerpt)
    return len(candidate) >= min_chars and candidate in grounding_form(source_text)


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_ws(text).lower().encode("utf-8")).hexdigest()[:16]


def shingles(text: str, size: int = 5, cap: int = 4000) -> set[int]:
    words = tokens(text)
    found: set[int] = set()
    for index in range(max(0, len(words) - size + 1)):
        found.add(hash(" ".join(words[index : index + size])))
        if len(found) >= cap:
            break
    return found


def canonicalize_url(url: str) -> str | None:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().removeprefix("www.")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
        ]
    )
    path = parsed.path.rstrip("/") or ""
    return urlunparse(("https", host, path, "", query, ""))


def host_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def host_group(host: str) -> str:
    """Registrable-domain approximation using generic public-suffix conventions."""
    labels = [label for label in (host or "").lower().split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


#: Words that say "no particular scope", so they narrow nothing and contradict nothing.
_SCOPE_WILDCARDS = frozenset(
    {"general", "generally", "global", "globally", "overall", "any", "all", "unspecified", "none"}
)


def scope_tokens(scope: str | None) -> set[str]:
    """The words that make a stated scope specific, plurals folded together."""
    return {_singular(token) for token in tokens(scope or "")} - _SCOPE_WILDCARDS


def scope_conflict(left: str | None, right: str | None) -> bool:
    """Whether two stated scopes describe different subjects rather than one narrowing the other.

    Generic by construction: scopes are compared as words, so a place, a population, a
    version, a period or a set of conditions are all handled alike. An unstated or merely
    generic scope contradicts nothing. Otherwise two scopes describe the same subject only
    when one contains the other ("Ferrindale" and "rural Ferrindale"); each having a word
    the other lacks makes them different subjects ("dry season trials" and "irrigated
    trials"), even when they share a common noun.
    """
    first, second = scope_tokens(left), scope_tokens(right)
    if not first or not second:
        return False
    return not (first <= second or second <= first)


def parse_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(value, fuzzy=False).date()
    except (ValueError, OverflowError, TypeError):
        return None
