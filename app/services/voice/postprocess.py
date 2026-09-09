"""Turning a decoded transcript into text fit for the composer.

Whisper already emits punctuated, capitalised prose, which is most of why it beats
other local speech recognition for this job. What is left is tidying, plus one opt-in
feature -- spoken punctuation -- that has to be written carefully because the words it
listens for are ordinary English.

Two things deliberately do **not** happen here:

*Insertion spacing.* Whether the text needs a leading space depends on the character
before the caret, which only the browser knows. The server returns trimmed text and
``frontend/src/voice/insertion.js`` decides how it joins.

*Any kind of model-based cleanup.* Running an LLM over the transcript would be slow,
and it would paraphrase. The user asked for what they said.
"""

from __future__ import annotations

import re

# Spoken punctuation, longest phrases first so that "exclamation point" is matched
# before "point" would be. Order is load-bearing.
#
# Only multi-word commands are here, and that omission is the whole design of this
# feature. The single words people reach for first -- "period", "comma", "colon",
# "dash" -- are ordinary English, and no amount of regex distinguishes the command in
# "hello comma world" from the noun in "the Jurassic period was long" or "the comma is
# missing". Substituting them silently rewrites sentences the user actually dictated,
# which is exactly the failure this whole subsystem is built to avoid: losing or
# corrupting speech is far worse than making somebody type a full stop themselves.
#
# The multi-word forms carry no such ambiguity. Nobody says "new line" or "question
# mark" mid-sentence meaning the words, and "new line" is the one a composer genuinely
# needs, because Enter submits rather than breaking the line.
SPOKEN_PUNCTUATION: tuple[tuple[str, str], ...] = (
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("newline", "\n"),
    ("exclamation point", "!"),
    ("exclamation mark", "!"),
    ("question mark", "?"),
    ("open parenthesis", "("),
    ("close parenthesis", ")"),
    ("open paren", "("),
    ("close paren", ")"),
    ("open quote", '"'),
    ("close quote", '"'),
)

# Punctuation that closes up against the word before it, so "hello , world" reads as
# "hello, world" once a spoken comma has been substituted.
_CLINGS_LEFT = ".,!?;:)"

_WHITESPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def collapse_whitespace(text: str) -> str:
    """One space between words, at most one blank line between paragraphs.

    Newlines survive because spoken punctuation can introduce them deliberately;
    everything else horizontal collapses.
    """

    text = text.replace(" ", " ")
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def capitalise_first(text: str) -> str:
    """Capitalise the opening letter if the model did not.

    Skipped when the opening word carries a capital anywhere else in it, because that
    is the shape of a deliberately-cased identifier -- "iOS", "iPhone", "eBay" -- and
    "IOS builds fine" is a worse sentence than the uncapitalised one. An opening quote
    or digit is likewise left alone.
    """

    stripped = text.lstrip()
    if not stripped:
        return text
    offset = len(text) - len(stripped)
    first = stripped[0]
    if not first.islower():
        return text

    opening_word = stripped.split(maxsplit=1)[0]
    if any(character.isupper() for character in opening_word[1:]):
        return text
    return text[:offset] + first.upper() + text[offset + 1 :]


def apply_spoken_punctuation(text: str) -> str:
    """Replace standalone spoken punctuation words with the marks they name.

    Anchored on word boundaries and matched only as whole tokens, which is what keeps
    "the Jurassic period was long" intact while turning "hello comma world period"
    into "hello, world." A phrase is also skipped when the punctuation it names is
    already adjacent, so dictating "world period" after Whisper already wrote a full
    stop does not produce "world..".
    """

    result = text
    for phrase, mark in SPOKEN_PUNCTUATION:
        pattern = re.compile(rf"(?<!\w)\s*\b{re.escape(phrase)}\b(?!\w)", re.IGNORECASE)

        # ``source`` and ``mark`` are bound as defaults rather than captured: the loop
        # rebinds both on every pass, and a closure that read them late would judge
        # each match against the wrong string.
        def substitute(match: re.Match[str], mark: str = mark, source: str = result) -> str:
            if source[: match.start()].rstrip().endswith(mark):
                return ""
            return mark

        result = pattern.sub(substitute, result)

    # A substituted mark leaves the space that preceded its word; close it up.
    result = re.sub(rf"\s+([{re.escape(_CLINGS_LEFT)}])", r"\1", result)
    # An opening bracket clings the other way.
    result = re.sub(r"\(\s+", "(", result)
    return result


def finalise(text: str, *, spoken_punctuation: bool = False) -> str:
    """The full tidy-up, in the order the steps depend on each other.

    Punctuation substitution runs before whitespace collapsing because it introduces
    newlines and leaves gaps behind the words it removes, and before capitalisation
    because it can change which character is first.
    """

    if not text or not text.strip():
        return ""
    if spoken_punctuation:
        text = apply_spoken_punctuation(text)
    text = collapse_whitespace(text)
    return capitalise_first(text)


def strip_prompt_echo(text: str, initial_prompt: str | None, *, min_run: int = 24) -> str:
    """Remove a vocabulary prompt the model echoed back instead of transcribing.

    On short or near-silent audio Whisper sometimes emits its own ``initial_prompt``,
    so a user who said "hi" gets a list of their repository's symbol names. A long
    verbatim run shared with the prompt is the signature, and ``min_run`` is set well
    above the length of an ordinary shared phrase so that genuinely dictating a term
    that appears in the prompt -- which is the entire point of biasing -- is untouched.
    """

    if not initial_prompt or not text:
        return text

    needle = text.strip()
    haystack = initial_prompt.strip()
    if len(needle) >= min_run and needle in haystack:
        return ""

    # A prefix or suffix of the output that matches the prompt is trimmed rather than
    # discarding the whole thing, since real speech may follow the echo.
    for run in range(len(needle), min_run - 1, -1):
        if needle[:run] in haystack:
            return needle[run:].lstrip(" ,.;:")
    return needle
