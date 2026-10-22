"""Dates in answers and evidence, set against today.

A small model copies a source's tense: an article written in spring that says a film "is
scheduled for May 22" becomes, in October, an answer saying the film "is scheduled for May
22". Telling it today's date helps but does not stop it. This module checks the finished
answer instead, deterministically:

* ``find_dates`` reads the dates an answer or passage states, at the precision they are
  written (a day, a month, a season, a quarter or a year), as a span of days.
* ``audit_answer`` finds a date that has passed presented as still to come ("is set to apply
  from August 2" in October), and a date still to come presented as done ("was released on
  November 19" in October). The first is rewritten in place to its past form ("was set to
  apply"), which states exactly what the source said without claiming it happened; anything
  it cannot rewrite safely is only recorded. Quoted text and reported speech ("the studio
  said it will launch on ...") keep their wording: they report what someone said then.
* ``guard_unsourced_status`` handles answers written without any usable source: there the
  model's knowledge stops before today, so a claim that something has not come out yet, or
  is still to come, cannot be true as far as anyone in the conversation knows, and the
  sentence is replaced by one saying its current status could not be confirmed.
* ``support_framing`` says whether a source sentence reports a date as done or as planned,
  so a release date that has passed is called "out" only when a source says it came out.

Nothing here names a topic: only dates, tense and the words that mark a plan or a report.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH = (
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|"
    r"december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
)
_YEAR = r"(?P<year>(?:19|20)\d{2})"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"

#: Most precise first: a full date is masked before its month and year are read again.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("day", re.compile(rf"\b{_YEAR}-(?P<m>[01]\d)-(?P<d>[0-3]\d)\b")),
    ("day", re.compile(rf"\b{_MONTH}\s+{_DAY},?\s+{_YEAR}\b", re.IGNORECASE)),
    ("day", re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH},?\s+{_YEAR}\b", re.IGNORECASE)),
    ("month", re.compile(rf"\b{_MONTH}\s+(?:of\s+)?{_YEAR}\b", re.IGNORECASE)),
    (
        "season",
        re.compile(
            rf"\b(?P<season>spring|summer|fall|autumn)\s+(?:of\s+)?{_YEAR}\b", re.IGNORECASE
        ),
    ),
    (
        "part",
        re.compile(
            rf"\b(?P<part>early|mid|late|end of|start of|beginning of)[\s-]+{_YEAR}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quarter",
        re.compile(
            rf"\b(?:Q(?P<q>[1-4])|(?P<qword>first|second|third|fourth)\s+quarter\s+of)\s+"
            rf"{_YEAR}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "half",
        re.compile(
            rf"\b(?:H(?P<h>[12])|(?P<hword>first|second)\s+half\s+of)\s+{_YEAR}\b",
            re.IGNORECASE,
        ),
    ),
    # A bare year only after a word that makes it a time ("in 2025", "by 2027"), never as a
    # name or a model number ("the 2025 film" is still a date; "iPhone 2025" is rare).
    (
        "year",
        re.compile(
            rf"\b(?:in|by|during|until|through|throughout|before|after|for|from|since)\s+"
            rf"{_YEAR}\b",
            re.IGNORECASE,
        ),
    ),
)
_SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11)}
_PARTS = {
    "early": (1, 4),
    "start of": (1, 3),
    "beginning of": (1, 3),
    "mid": (5, 8),
    "late": (9, 12),
    "end of": (10, 12),
}
_ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}


@dataclass(frozen=True)
class DateSpan:
    """A date as written: the first and last day it can mean, and where it is in the text."""

    start: date
    end: date
    text: str
    begin: int
    finish: int
    granularity: str


def _month_span(year: int, first: int, last: int) -> tuple[date, date]:
    return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])


def _span(kind: str, match: re.Match[str]) -> tuple[date, date] | None:
    groups = match.groupdict()
    year = int(groups["year"])
    try:
        if kind == "day":
            if groups.get("m"):
                day = date(year, int(groups["m"]), int(groups["d"]))
            else:
                day = date(year, _MONTHS[groups["month"].lower().rstrip(".")], int(groups["day"]))
            return day, day
        if kind == "month":
            month = _MONTHS[groups["month"].lower().rstrip(".")]
            return _month_span(year, month, month)
        if kind == "season":
            return _month_span(year, *_SEASONS[groups["season"].lower()])
        if kind == "part":
            return _month_span(year, *_PARTS[" ".join(groups["part"].lower().split())])
        if kind == "quarter":
            quarter = int(groups["q"]) if groups.get("q") else _ORDINAL[groups["qword"].lower()]
            return _month_span(year, quarter * 3 - 2, quarter * 3)
        if kind == "half":
            half = int(groups["h"]) if groups.get("h") else _ORDINAL[groups["hword"].lower()]
            return _month_span(year, 1, 6) if half == 1 else _month_span(year, 7, 12)
        if kind == "year":
            return date(year, 1, 1), date(year, 12, 31)
    except (KeyError, ValueError):
        return None
    return None


def find_dates(text: str) -> list[DateSpan]:
    """Every date ``text`` states, most precise reading first, without overlaps, in order."""

    found: list[DateSpan] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text or ""):
            begin, finish = match.start(), match.end()
            if kind == "year":
                begin = match.start("year")
            if any(begin < end and finish > start for start, end in taken):
                continue
            span = _span(kind, match)
            if span is None:
                continue
            taken.append((begin, finish))
            found.append(DateSpan(span[0], span[1], text[begin:finish], begin, finish, kind))
    return sorted(found, key=lambda item: item.begin)


# --------------------------------------------------------------------------- framing

#: An auxiliary that puts the next words in the future or in a plan.
_PLAN_AUX = (
    r"(?P<aux>is|are|'s|'re)\s+(?P<adv>(?:(?:now|currently|still|also|reportedly|officially|"
    r"tentatively|finally)\s+)*)"
)
_PLAN_PARTICIPLE = (
    r"(?P<word>set|scheduled|slated|expected|due|planned|poised|confirmed|tipped|rumou?red|"
    r"anticipated|projected|targeted|pencilled|penciled|going)"
)
_PLAN_GERUND = (
    r"(?P<gerund>launching|releasing|premiering|arriving|coming|debuting|opening|landing|"
    r"hitting|dropping|returning|starting|beginning|shipping|airing)"
)
#: "is set to", "are expected", "'s scheduled for".
PLAN_STATE = re.compile(rf"\b{_PLAN_AUX}{_PLAN_PARTICIPLE}\b", re.IGNORECASE)
#: "is launching", "are coming" (a plan stated in the progressive).
PLAN_PROGRESSIVE = re.compile(rf"\b{_PLAN_AUX}{_PLAN_GERUND}\b", re.IGNORECASE)
#: "will launch", "will be released", "will finally arrive".
WILL = re.compile(
    r"\bwill\s+(?P<adv>(?:(?:now|also|finally|officially|reportedly|likely|then|first)\s+)*)"
    r"(?P<be>be\s+)?(?P<verb>[a-z]+)\b",
    re.IGNORECASE,
)
_UPCOMING = re.compile(r"\bupcoming\b|\bcoming soon\b", re.IGNORECASE)
_NOT_YET = re.compile(
    r"\bnot\s+(?:yet\s+)?(?:been\s+)?(?:out|released|available|launched)(?:\s+yet)?\b|"
    r"\byet\s+to\s+(?:be\s+)?(?:released|launched|come out|arrive|premiere|open)\b",
    re.IGNORECASE,
)
#: Words that make a clause a report of what someone said at the time, whose tense belongs
#: to that moment: "the studio said it will launch on ...".
REPORTING = re.compile(
    r"\b(?:said|says|announced|stated|reported|claimed|told|wrote|according to|promised|"
    r"predicted|revealed|confirmed that|insisted|hinted)\b",
    re.IGNORECASE,
)
#: Past tense of release-like events, attached to a date: "was released on", "launched on".
DONE = re.compile(
    r"\b(?:(?P<aux>was|were)\s+(?P<part>released|launched|published|premiered|held|opened|"
    r"completed|unveiled|introduced)|(?:released|launched|premiered|debuted|opened|arrived|"
    r"came out|took place|happened|went live|went on sale|took effect|came into (?:force|effect)))"
    r"\b",
    re.IGNORECASE,
)

_GERUND_BASE = {
    "launching": "launch",
    "releasing": "release",
    "premiering": "premiere",
    "arriving": "arrive",
    "coming": "come",
    "debuting": "debut",
    "opening": "open",
    "landing": "land",
    "hitting": "hit",
    "dropping": "drop",
    "returning": "return",
    "starting": "start",
    "beginning": "begin",
    "shipping": "ship",
    "airing": "air",
}
#: Verbs that may follow "will" in a plan about an event; anything else ("will be a hit",
#: "will love it") is not a statement about when something happens.
_EVENT_VERBS = frozenset(
    {
        "launch",
        "release",
        "premiere",
        "arrive",
        "come",
        "debut",
        "open",
        "land",
        "hit",
        "drop",
        "return",
        "start",
        "begin",
        "ship",
        "air",
        "apply",
        "take",
        "go",
        "happen",
        "end",
        "close",
        "finish",
        "run",
        "kick",
        "roll",
        "launched",
        "released",
        "held",
        "published",
        "available",
        "out",
        "shown",
        "streaming",
        "playable",
    }
)

_QUOTED = re.compile(r"\"[^\"\n]{1,400}\"|“[^”\n]{1,400}”")
#: A full stop that ends a sentence: followed by a space and a capital, a quote, a bracket or
#: list markup, and not part of an abbreviation ("Aug. 30", "U.S.", "e.g.") or a number.
_SENTENCE_END = re.compile(
    r"(?<!\b[A-Z])(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bSt)(?<!\bvs)(?<!\bNo)(?<!\be\.g)"
    r"(?<!\bi\.e)(?<!\bJan)(?<!\bFeb)(?<!\bMar)(?<!\bApr)(?<!\bJun)(?<!\bJul)(?<!\bAug)"
    r"(?<!\bSep)(?<!\bSept)(?<!\bOct)(?<!\bNov)(?<!\bDec)[.!?]+[\"”')\]]*"
    r"(?=\s+[\"“(\[*#A-Z0-9-])|\n+"
)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Start and end offsets of each sentence or line of ``text``, covering all of it."""

    spans, start = [], 0
    for match in _SENTENCE_END.finditer(text or ""):
        end = match.end()
        if end > start:
            spans.append((start, end))
        start = end
    if start < len(text or ""):
        spans.append((start, len(text)))
    return spans


_CLAUSE_BREAK = re.compile(
    r";|:\s|,\s+(?:while|whereas|but|although|though|and|with|as|after|before|since)\b|"
    r"\s+(?:while|whereas|but)\s+|\s+\(|\)",
    re.IGNORECASE,
)


@dataclass
class TemporalFinding:
    kind: str
    date: str
    sentence: str
    edited: bool = False
    replacement: str | None = None


@dataclass
class TemporalAudit:
    """The answer after the check, and what the check found or changed."""

    body: str
    findings: list[TemporalFinding] = field(default_factory=list)

    @property
    def edits(self) -> int:
        return sum(1 for finding in self.findings if finding.edited)

    def summary(self) -> dict[str, object]:
        return {
            "findings": [
                {
                    "kind": finding.kind,
                    "date": finding.date,
                    "edited": finding.edited,
                    "sentence": finding.sentence[:240],
                    **({"replacement": finding.replacement[:240]} if finding.replacement else {}),
                }
                for finding in self.findings
            ],
            "edits": self.edits,
        }


def _clauses(sentence: str) -> list[tuple[int, int]]:
    """Start and end offsets of the clauses in ``sentence``."""

    bounds, start = [], 0
    for match in _CLAUSE_BREAK.finditer(sentence):
        if match.start() > start:
            bounds.append((start, match.start()))
        start = match.end()
    if start < len(sentence):
        bounds.append((start, len(sentence)))
    return bounds or [(0, len(sentence))]


#: A bold run of five or more words: a headline or title being named ("**Cyberpunk is
#: coming to Apex Legends!**"), not a label ("**Upcoming releases:**").
_BOLD_TITLE = re.compile(r"\*\*(?=(?:[^*\n]*?\s+){4})[^*\n]+?\*\*")


def _masked(sentence: str) -> str:
    """The sentence with quotations and bold titles blanked, so neither is read as Neo's claim."""

    blank = lambda match: " " * len(match.group(0))  # noqa: E731
    return _BOLD_TITLE.sub(blank, _QUOTED.sub(blank, sentence))


def _plan_before(clause: str, date_at: int, others: list[int]) -> re.Match[str] | None:
    """The plan wording that governs the date at ``date_at`` in ``clause``, if any.

    It must come before the date with no other date in between: in "X is set for June 2027,
    after Y premiered in May 2026", "is set for" belongs to June, not May.
    """

    best = None
    for pattern in (PLAN_STATE, PLAN_PROGRESSIVE, WILL, _UPCOMING, _NOT_YET):
        for match in pattern.finditer(clause):
            if match.end() > date_at:
                continue
            if any(match.end() <= other < date_at for other in others):
                continue
            if pattern is WILL:
                verb = match.group("verb").lower()
                if verb not in _EVENT_VERBS:
                    continue
            if best is None or match.end() > best.end():
                best = match
    return best


def _done_before(clause: str, date_at: int, others: list[int]) -> re.Match[str] | None:
    best = None
    for match in DONE.finditer(clause):
        if match.end() > date_at or any(match.end() <= other < date_at for other in others):
            continue
        if best is None or match.end() > best.end():
            best = match
    return best


def _past_form(match: re.Match[str]) -> str | None:
    """The plan wording rewritten as what was planned, or ``None`` when that is not safe."""

    text = match.group(0)
    groups = match.groupdict()
    plural = (groups.get("aux") or "").lower() in {"are", "'re"}
    was = "were" if plural else "was"
    lead = " " if text.startswith("'") else ""
    if match.re is PLAN_STATE:
        return f"{lead}{was} {groups.get('adv') or ''}{groups['word']}"
    if match.re is PLAN_PROGRESSIVE:
        base = _GERUND_BASE.get(groups["gerund"].lower())
        return f"{lead}{was} {groups.get('adv') or ''}due to {base}" if base else None
    if match.re is WILL:
        verb = groups["verb"]
        if groups.get("be"):
            return f"was {groups.get('adv') or ''}due to be {verb}"
        return f"was {groups.get('adv') or ''}due to {verb}"
    return None


def _drop_upcoming(text: str, begin: int, end: int) -> tuple[int, int, str]:
    """The edit removing "upcoming" at ``begin:end``; "an" before it becomes "a" if needed."""

    while end < len(text) and text[end] == " ":
        end += 1
    article = re.search(r"\b(an)\s+$", text[:begin], re.IGNORECASE)
    following = text[end : end + 1]
    if article and following.isalpha() and following.lower() not in "aeiou":
        return article.start(), end, text[article.start()] + " "
    if text[begin : begin + 1].isupper() and following.isalpha():
        # "Upcoming series:" keeps its capital: "Series:".
        return begin, end + 1, following.upper()
    return begin, end, ""


def _future_form(match: re.Match[str]) -> str | None:
    groups = match.groupdict()
    if groups.get("aux") and groups.get("part"):
        are = groups["aux"].lower() == "were"
        return f"{'are' if are else 'is'} due to be {groups['part']}"
    return None


def audit_answer(body: str, today: date) -> TemporalAudit:
    """Find, and where safe rewrite, dates set in the wrong tense against ``today``.

    A passed date under plan wording becomes its past form ("is scheduled for May 22" ->
    "was scheduled for May 22"). A coming date stated as done is rewritten only in the
    passive ("was released on" -> "is due to be released on"); other cases are recorded. A
    clause that reports what someone said, or uses a past form already, is left alone.
    """

    findings: list[TemporalFinding] = []
    pieces: list[str] = []
    position = 0
    for start, end in _sentence_spans(body or ""):
        sentence = body[start:end]
        if start > position:
            pieces.append(body[position:start])
        position = end
        if not sentence.strip() or "```" in sentence:
            pieces.append(sentence)
            continue
        masked = _masked(sentence)
        dates = find_dates(masked)
        if not dates:
            pieces.append(sentence)
            continue
        edits: list[tuple[int, int, str]] = []
        for clause_start, clause_end in _clauses(masked):
            clause = masked[clause_start:clause_end]
            inside = [d for d in dates if clause_start <= d.begin < clause_end]
            for span in inside:
                at = span.begin - clause_start
                others = [d.begin - clause_start for d in inside if d is not span]
                if span.end < today:
                    # "in force since July 27", "as of August 2": a state that still holds.
                    if re.search(r"\b(?:since|as of)\s*$", clause[max(0, at - 12) : at], re.I):
                        continue
                    plan = _plan_before(clause, at, others)
                    if plan is None:
                        continue
                    if REPORTING.search(clause[: plan.start()]):
                        continue
                    replacement = _past_form(plan)
                    finding = TemporalFinding("past_date_as_upcoming", span.text, sentence)
                    if plan.re is _UPCOMING and plan.group(0).lower() == "upcoming":
                        begin, end, cut = _drop_upcoming(
                            masked, clause_start + plan.start(), clause_start + plan.end()
                        )
                        edits.append((begin, end, cut))
                        finding.edited = True
                        findings.append(finding)
                        continue
                    if replacement is not None:
                        edits.append(
                            (clause_start + plan.start(), clause_start + plan.end(), replacement)
                        )
                        finding.edited = True
                    findings.append(finding)
                elif span.start > today:
                    done = _done_before(clause, at, others)
                    if done is None:
                        continue
                    before = clause[: done.start()]
                    if REPORTING.search(before) or re.search(
                        r"\b(?:trailer|teaser|pre-?orders?|demo|beta)\b", before, re.IGNORECASE
                    ):
                        continue
                    replacement = _future_form(done)
                    finding = TemporalFinding("future_date_as_done", span.text, sentence)
                    if replacement is not None:
                        edits.append(
                            (clause_start + done.start(), clause_start + done.end(), replacement)
                        )
                        finding.edited = True
                    findings.append(finding)
        # "Upcoming" describes the subject of the whole sentence, so it is matched across
        # clauses ("a major upcoming entry, with a release date of February 27").
        for span in dates:
            if span.end >= today or re.search(
                r"\b(?:since|as of)\s*$", masked[max(0, span.begin - 12) : span.begin], re.I
            ):
                continue
            word = None
            for match in re.finditer(r"\bupcoming\b", masked, re.IGNORECASE):
                between = [
                    d for d in dates if d is not span and match.end() <= d.begin < span.begin
                ]
                if match.end() <= span.begin and not between:
                    word = match
            if word is None or any(b <= word.start() < e for b, e, _ in edits):
                continue
            if REPORTING.search(masked[: word.start()]):
                continue
            edits.append(_drop_upcoming(masked, word.start(), word.end()))
            findings.append(
                TemporalFinding("past_date_as_upcoming", span.text, sentence, edited=True)
            )
        if edits:
            rewritten = sentence
            for begin, end, replacement in sorted(set(edits), reverse=True):
                rewritten = rewritten[:begin] + replacement + rewritten[end:]
            for finding in findings:
                if finding.sentence is sentence and finding.edited:
                    finding.replacement = rewritten
            pieces.append(rewritten)
        else:
            pieces.append(sentence)
    pieces.append((body or "")[position:])
    return TemporalAudit("".join(pieces), findings)


# --------------------------------------------------------------------------- no sources

#: A claim about whether something has come out, or that it is still to come. Without any
#: source this can only come from training data, which stops before today.
UNSOURCED_STATUS = re.compile(
    r"\b(?:has|have)(?:n't|\s+not)(?:\s+yet)?\s+(?:been\s+)?(?:officially\s+)?(?:released|"
    r"launched|approved|premiered|come out|happened|started|opened|aired|arrived)\b|"
    r"\b(?:is|are)(?:n't|\s+not)(?:\s+yet)?\s+(?:officially\s+)?(?:out|available|released|"
    r"launched)\b|"
    r"\bnot\s+(?:yet\s+)?(?:been\s+)?(?:officially\s+)?(?:out|released|launched)\s+yet\b|"
    r"\byet\s+to\s+(?:be\s+)?(?:released|launched|come out|arrive|premiere)\b|"
    r"\b(?:is|are)\s+(?:still\s+)?(?:upcoming|unreleased|in development)\b|"
    r"\b(?:is|are)\s+(?:currently\s+)?(?:expected|set|scheduled|slated|due|planned)\s+to\s+"
    r"(?:be\s+)?(?:release|released|launch|launched|come out|arrive|premiere|open)\b|"
    r"\bwill\s+(?:be\s+)?(?:release|released|launch|launched|come out|arrive|premiere)\b",
    re.IGNORECASE,
)
UNCONFIRMED_STATUS_SENTENCE = (
    "I couldn't confirm whether it has come out yet, because I found no current source for it."
)


def guard_unsourced_status(body: str) -> TemporalAudit:
    """Replace claims about current release status in an answer that had no sources.

    Only the sentences making such a claim change; general background stays as written.
    """

    findings: list[TemporalFinding] = []
    pieces: list[str] = []
    position = 0
    replaced_once = False
    for start, end in _sentence_spans(body or ""):
        sentence = body[start:end]
        if start > position:
            pieces.append(body[position:start])
        position = end
        masked = _masked(sentence)
        hit = UNSOURCED_STATUS.search(masked)
        if hit and "```" not in sentence:
            finding = TemporalFinding("unsourced_status", hit.group(0), sentence, edited=True)
            # One plain statement replaces every such claim; repeating it reads as a glitch.
            replacement = "" if replaced_once else UNCONFIRMED_STATUS_SENTENCE
            finding.replacement = replacement
            findings.append(finding)
            trailing = sentence[len(sentence.rstrip()) :]
            pieces.append((replacement + trailing) if replacement else trailing.lstrip(" "))
            replaced_once = True
        else:
            pieces.append(sentence)
    pieces.append((body or "")[position:])
    text = re.sub(r"[ \t]{2,}", " ", "".join(pieces))
    return TemporalAudit(text.strip(), findings)


# --------------------------------------------------------------------------- evidence

_SUPPORT_DONE = re.compile(
    r"\b(?:was|were|has been|have been|got)\s+(?:officially\s+|finally\s+)?(?:released|launched|"
    r"published|premiered|opened|made available)\b|"
    r"\b(?:released|launched|premiered|debuted|opened|arrived|came out|went live|hit "
    r"(?:theaters|theatres|shelves|stores))\s+(?:on|in|worldwide|globally)\b|"
    r"\bis\s+(?:now\s+)?(?:out|available|streaming)\b|\bsince\s+its\s+release\b",
    re.IGNORECASE,
)
_SUPPORT_PLAN = re.compile(
    r"\bwill\b|\b(?:is|are)\s+(?:\w+\s+)?(?:set|scheduled|slated|expected|due|planned|poised|"
    r"confirmed)\b|\bcoming\b|\bupcoming\b|\b(?:arrives|launches|premieres|releases|opens|"
    r"debuts|hits)\b|\bpre-?orders?\b|\bwishlist\b",
    re.IGNORECASE,
)


def support_framing(text: str) -> str | None:
    """``"done"`` when ``text`` reports an event as having happened, ``"planned"`` when it
    gives it as a plan, else ``None``. A sentence that does both counts as done."""

    if _SUPPORT_DONE.search(text or ""):
        return "done"
    if _SUPPORT_PLAN.search(text or ""):
        return "planned"
    return None


def relative_age(when: date, today: date) -> int:
    return (when - today).days


__all__ = [
    "DateSpan",
    "TemporalAudit",
    "TemporalFinding",
    "UNCONFIRMED_STATUS_SENTENCE",
    "audit_answer",
    "find_dates",
    "guard_unsourced_status",
    "relative_age",
    "support_framing",
    "timedelta",
]
