"""Deciding whether an answer was any good, without asking another model.

Every check in here is a pure function of the text a model returned. That is the whole
reason the comparison is fast: grading three models over three tasks costs microseconds,
so the wall-clock time of a run is the models generating and nothing else.

Two rules shape the vocabulary.

Checks are tolerant about *shape* and strict about *substance*. A model that answers
"The next number is 42." has got the question right and should not lose to one that
answered "42" -- so the answer is extracted before it is compared, and whether the model
padded it is scored separately, as its own check, rather than folded into correctness.

And a check that cannot be applied returns "skipped", never "failed". A task asking for
Python that came back with prose has no function to inspect; recording that as a failed
parse would count the same failure twice.

Nothing here executes model-generated code. The generated text is untrusted, this runs
on the user's own machine, and a comparison feature is nowhere near a good enough reason
to run arbitrary code on it -- so code answers are checked by parsing them, which
verifies they are real Python without ever evaluating them.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.model_compare.types import CheckResult

# A regex answer is compiled and tried against short fixtures. Python's `re` has no
# timeout, so a pathological pattern is refused by length rather than by running it --
# the honest answers to these tasks are all well under this.
MAX_PATTERN_LENGTH = 200

#: Bumped whenever a rule changes what a given answer scores. The rules *are* the score,
#: so a result graded by a later version is not directly comparable with an earlier one;
#: recording this on every run is what lets a future history feature say so rather than
#: silently charting two different measurements as one line.
GRADER_VERSION = "3"

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)*")
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def strip_fences(text: str) -> str:
    """The contents of the first fenced block, or the text itself when there is none."""

    found = _FENCE.search(text or "")
    return found.group(1).strip() if found else (text or "").strip()


#: Models emit typographic quotes as readily as ASCII ones, and "I don't know" written
#: with U+2019 must not read as a different answer from the same words with an ASCII
#: apostrophe. Normalised before anything is compared.
_SMART_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"'})


def condensed(text: str) -> str:
    """Lowercased, whitespace collapsed, quotes normalised, edge punctuation removed."""

    cleaned = re.sub(r"\s+", " ", (text or "").translate(_SMART_QUOTES).strip().lower())
    return cleaned.strip(" \t\"'`*.,:;!?()[]{}")


def final_number(text: str) -> float | None:
    """The last number in the answer.

    The last rather than the first: a model that works out loud writes the intermediate
    values before the result, so the first number it says is usually not its answer.
    """

    found = _NUMBER.findall(text or "")
    if not found:
        return None
    # Thousands separators only; a comma decimal point would make "1,5" ambiguous and
    # none of the built-in tasks have an answer where that arises.
    raw = found[-1].replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def word_count(text: str) -> int:
    return len([word for word in re.split(r"\s+", (text or "").strip()) if word])


def sentence_count(text: str) -> int:
    return len([part for part in _SENTENCE_END.split((text or "").strip()) if part.strip()])


@dataclass(frozen=True)
class Check:
    """Base class. Subclasses implement ``run`` and inherit the result plumbing."""

    label: str = ""
    weight: float = 1.0

    def run(self, answer: str) -> tuple[str, str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def apply(self, answer: str) -> CheckResult:
        try:
            status, detail = self.run(answer)
        except Exception as exc:  # a malformed answer must not end the run
            status, detail = "skipped", f"This one could not be checked ({exc.__class__.__name__})."
        return CheckResult(label=self.label, status=status, detail=detail, weight=self.weight)


@dataclass(frozen=True)
class Equals(Check):
    """The answer is a short value, compared after normalisation."""

    expected: tuple[str, ...] = ()

    def run(self, answer: str) -> tuple[str, str]:
        cleaned = condensed(answer)
        if not cleaned:
            return "skipped", "Nothing came back."
        # Whole words inside the answer, rather than equality or a bare substring.
        # Equality would fail "The capital is Canberra.", which is the right answer; a
        # bare substring would pass "pineapple" for "apple", which is not.
        if any(
            re.search(rf"\b{re.escape(condensed(item))}\b", cleaned) for item in self.expected
        ):
            return "passed", ""
        return "failed", f"Expected {self.expected[0]}."


@dataclass(frozen=True)
class NumberIs(Check):
    """The answer works out to a particular number.

    ``accepts`` carries the same answer written another way -- five cents as ``0.05``
    rather than ``5``, say. It is for genuinely equivalent expressions of the right
    answer, never for making a hard task easier to pass.
    """

    expected: float = 0.0
    tolerance: float = 0.001
    accepts: tuple[float, ...] = ()

    def run(self, answer: str) -> tuple[str, str]:
        value = final_number(answer)
        if value is None:
            return "skipped", "No number in the answer."
        for candidate in (self.expected, *self.accepts):
            if abs(value - candidate) <= self.tolerance:
                return "passed", ""
        return "failed", f"Answered {value:g}, expected {self.expected:g}."


@dataclass(frozen=True)
class Contains(Check):
    """Every one of these has to appear. Case-insensitive."""

    needles: tuple[str, ...] = ()
    any_of: bool = False

    def run(self, answer: str) -> tuple[str, str]:
        haystack = (answer or "").translate(_SMART_QUOTES).lower()
        if not haystack.strip():
            return "skipped", "Nothing came back."
        hits = [item for item in self.needles if item.lower() in haystack]
        if self.any_of:
            return ("passed", "") if hits else ("failed", f"None of: {', '.join(self.needles)}.")
        missing = [item for item in self.needles if item not in hits]
        return ("passed", "") if not missing else ("failed", f"Missing: {', '.join(missing)}.")


@dataclass(frozen=True)
class Matches(Check):
    """The answer matches a pattern.

    For the cases a needle cannot express, because the right answer is a substring of a
    wrong one: "log n" appears inside "n log n", so binary search and merge sort would
    score the same on a ``Contains``.
    """

    pattern: str = ""
    #: What to say when it did not match. The pattern itself is no use to the reader.
    detail: str = ""
    flags: int = re.IGNORECASE

    def run(self, answer: str) -> tuple[str, str]:
        text = (answer or "").translate(_SMART_QUOTES)
        if not text.strip():
            return "skipped", "Nothing came back."
        return ("passed", "") if re.search(self.pattern, text, self.flags) else (
            "failed", self.detail or "Not what was expected."
        )


@dataclass(frozen=True)
class Excludes(Check):
    """None of these may appear."""

    needles: tuple[str, ...] = ()

    def run(self, answer: str) -> tuple[str, str]:
        haystack = (answer or "").lower()
        if not haystack.strip():
            return "skipped", "Nothing came back."
        found = [item for item in self.needles if item.lower() in haystack]
        return ("passed", "") if not found else ("failed", f"Still there: {', '.join(found)}.")


@dataclass(frozen=True)
class Terse(Check):
    """The answer stayed near the length it was asked for.

    Scored on its own so that following an instruction is visible as its own axis. A
    model that is right but ignores "reply with only the number" is a different problem
    from one that is wrong, and the grid should say which.
    """

    max_words: int = 12

    def run(self, answer: str) -> tuple[str, str]:
        count = word_count(answer)
        if not count:
            return "skipped", "Nothing came back."
        if count <= self.max_words:
            return "passed", ""
        return "failed", f"{count} words, asked for at most {self.max_words}."


@dataclass(frozen=True)
class AtMostWords(Check):
    """A hard length ceiling that the task itself stated."""

    limit: int = 50

    def run(self, answer: str) -> tuple[str, str]:
        count = word_count(strip_fences(answer))
        if not count:
            return "skipped", "Nothing came back."
        if count <= self.limit:
            return "passed", ""
        return "failed", f"{count} words, the limit was {self.limit}."


@dataclass(frozen=True)
class SentencesAtMost(Check):
    limit: int = 1

    def run(self, answer: str) -> tuple[str, str]:
        count = sentence_count(answer)
        if not count:
            return "skipped", "Nothing came back."
        if count <= self.limit:
            return "passed", ""
        return "failed", f"{count} sentences, asked for {self.limit}."


@dataclass(frozen=True)
class IsJson(Check):
    """Valid JSON, optionally with particular keys and values."""

    keys: tuple[str, ...] = ()
    values: tuple[tuple[str, Any], ...] = ()

    def run(self, answer: str) -> tuple[str, str]:
        text = strip_fences(answer)
        if not text:
            return "skipped", "Nothing came back."
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return "failed", "Not valid JSON."
        if not isinstance(payload, dict):
            return "failed", "JSON, but not an object."
        missing = [key for key in self.keys if key not in payload]
        if missing:
            return "failed", f"Missing: {', '.join(missing)}."
        wrong = [
            key
            for key, expected in self.values
            if condensed(str(payload.get(key))) != condensed(str(expected))
        ]
        if wrong:
            return "failed", f"Wrong value for: {', '.join(wrong)}."
        return "passed", ""


@dataclass(frozen=True)
class IsPython(Check):
    """The answer parses as Python and defines the function that was asked for.

    Parsed, never executed. ``ast.parse`` answers "is this real code" -- which is the
    question that separates a model that can write Python from one that produces
    something code-shaped -- and it answers it without running a line of it.
    """

    function: str = ""
    arity: int | None = None

    def run(self, answer: str) -> tuple[str, str]:
        source = strip_fences(answer)
        if not source:
            return "skipped", "Nothing came back."
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return "failed", f"Does not parse (line {exc.lineno})."
        if not self.function:
            return "passed", ""
        defined = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == self.function
        ]
        if not defined:
            return "failed", f"No function called {self.function}."
        if self.arity is not None:
            found = defined[0].args
            count = len(found.posonlyargs) + len(found.args)
            if count != self.arity:
                return "failed", f"{self.function} takes {count} arguments, expected {self.arity}."
        return "passed", ""


@dataclass(frozen=True)
class RegexAnswer(Check):
    """The answer *is* a regular expression, tried against fixtures."""

    should_match: tuple[str, ...] = ()
    should_reject: tuple[str, ...] = ()

    def run(self, answer: str) -> tuple[str, str]:
        pattern = strip_fences(answer).strip().strip("`").splitlines()[0].strip() if answer else ""
        # Some models wrap the pattern in slashes, the way JavaScript writes them.
        if len(pattern) > 2 and pattern.startswith("/") and pattern.endswith("/"):
            pattern = pattern[1:-1]
        if not pattern:
            return "skipped", "Nothing came back."
        if len(pattern) > MAX_PATTERN_LENGTH:
            return "failed", "Far longer than this task needs."
        try:
            compiled = re.compile(pattern)
        except re.error:
            return "failed", "Not a valid pattern."
        missed = [item for item in self.should_match if not compiled.fullmatch(item)]
        if missed:
            return "failed", f"Does not match {missed[0]}."
        wrong = [item for item in self.should_reject if compiled.fullmatch(item)]
        if wrong:
            return "failed", f"Wrongly matches {wrong[0]}."
        return "passed", ""


@dataclass(frozen=True)
class LinesAtLeast(Check):
    """A list came back as a list."""

    minimum: int = 3
    numbered: bool = False

    def run(self, answer: str) -> tuple[str, str]:
        lines = [line.strip() for line in strip_fences(answer).splitlines() if line.strip()]
        if not lines:
            return "skipped", "Nothing came back."
        if self.numbered:
            lines = [line for line in lines if re.match(r"^\s*(?:\d+[.)]|[-*•])\s+", line)]
        if len(lines) >= self.minimum:
            return "passed", ""
        return "failed", f"{len(lines)} items, expected at least {self.minimum}."


def grade(answer: str, checks: tuple[Check, ...]) -> tuple[list[CheckResult], float | None]:
    """Run every check and roll them into one 0..1 score.

    Skipped checks leave the denominator, they do not fail it. When every check skipped
    -- the model said nothing usable -- the score is None rather than 0, because "could
    not be judged" and "judged and got everything wrong" are different findings and the
    grid shows them differently.
    """

    results = [check.apply(answer) for check in checks]
    applicable = [
        (result, check)
        for result, check in zip(results, checks, strict=True)
        if result.status != "skipped"
    ]
    if not applicable:
        return results, None
    earned = sum(check.weight for result, check in applicable if result.status == "passed")
    possible = sum(check.weight for _, check in applicable)
    return results, (earned / possible if possible else None)
