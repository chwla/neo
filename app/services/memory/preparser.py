"""Small bounded grammar for safe foreground extraction and model validation hints."""

from __future__ import annotations

import re
import unicodedata

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import (
    ConversationRole,
    ExtractionRequest,
    LifecycleHint,
    PreparsedSourceSpan,
    PreparsedSpan,
    PreparseKind,
    PreparseResult,
)
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelProposalResponse,
    ModelRetractionProposal,
    ModelSourceSpan,
    SubjectHint,
)
from app.services.memory.taxonomy import MemoryType

_IMPLICIT_GOAL_CORRECTION = re.compile(
    r"^I\s+no\s+longer\s+want\s+(?:to\s+)?(?P<old>[^.]+)\.\s*"
    r"I\s+want\s+(?:to\s+)?(?P<new>[^.]+)\.?$",
    re.IGNORECASE,
)
# Both halves are confined to a single statement.  Allowing ".+?" let this
# grammar swallow a whole multi-sentence correction by latching onto an
# unrelated "with" several sentences later, producing a retraction target that
# matched no stored memory, so the correction was dropped instead of applied.
_EXPLICIT_REPLACE = re.compile(
    r"^Correction:\s*replace\s+(?P<old>[^.?!]+?)\s+with\s+(?P<new>[^.?!]+?)\.?$",
    re.IGNORECASE,
)
_PREFERENCE_CORRECTION = re.compile(
    r"^I\s+(?:do\s+not|don't)\s+prefer\s+(?P<old>.+?)\s+anymore\.\s*"
    r"I\s+prefer\s+(?P<new>.+?)\.?$",
    re.IGNORECASE,
)
_GOAL_CORRECTION_PART = re.compile(
    r"I\s+no\s+longer\s+want\s+(?:to\s+)?(?P<old>[^.]+)\.\s*"
    r"I\s+want\s+(?:to\s+)?(?P<new>[^.]+)(?:\.|$)",
    re.IGNORECASE,
)
_PREFERENCE_CORRECTION_PART = re.compile(
    r"I\s+(?:do\s+not|don't)\s+prefer\s+(?P<old>.+?)\s+anymore\.\s*"
    r"I\s+prefer\s+(?P<new>[^.]+)(?:\.|$)",
    re.IGNORECASE,
)
_PURE_LOCATION_RETRACTION = re.compile(
    r"^I\s+no\s+longer\s+live\s+in\s+(?P<old>[^.]+)\.?$",
    re.IGNORECASE,
)
_CURRENT_LOCATION = re.compile(
    r"^(?:I\s+(?:currently\s+)?live\s+in|My\s+current\s+city\s+is)\s+"
    r"(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)
# "Call me X" is listed in ``policy._MEMORY_COMMAND`` as an explicit memory
# instruction, so the gate already opens for it; without a branch here the most
# direct way a person states their name fell through to the local model.
#
# A bare copula ("I'm Soham") is deliberately *not* here.  It cannot be told
# apart from "I am British", "I am Muslim" or "I am Deaf" by any rule this file
# could carry — the last two are sensitive categories, and filing one as a name
# would both be wrong and skip the judgement the model exists to apply.
_DIRECT_NAME = re.compile(
    r"^(?:My\s+(?:full\s+)?name\s+is"
    r"|I\s+am\s+called"
    r"|(?:Please\s+|You\s+can\s+)?Call\s+me)"
    r"\s+(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)
_DIRECT_AGE = re.compile(
    r"^I\s*(?:'m|’m|\s+am)\s+(?P<value>\d{1,3})\s+years?\s+old\.?$",
    re.IGNORECASE,
)
_DIRECT_ORIGIN = re.compile(
    r"^I\s*(?:'m|’m|\s+am)\s+from\s+(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)
# The optional ``-ly`` adverb keeps ordinary hedges such as "currently" (and the
# common misspellings of it) from defeating an otherwise unambiguous statement.
_DIRECT_EMPLOYER = re.compile(
    r"^I\s+(?:[a-z]+ly\s+)?work\s+(?:at|for)\s+(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)
_DIRECT_OCCUPATION = re.compile(
    r"^I\s+(?:[a-z]+ly\s+)?work\s+as\s+(?:an?\s+)?(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)
# Ordered so the more specific "work as" wins over the "work at/for" employer
# rule, and evaluated per sentence so one message may carry several facts.
_IDENTITY_SENTENCE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_DIRECT_NAME, "name"),
    (_DIRECT_AGE, "age"),
    (_DIRECT_ORIGIN, "origin"),
    (_DIRECT_OCCUPATION, "occupation"),
    (_DIRECT_EMPLOYER, "employer"),
    (_CURRENT_LOCATION, "current_location"),
)
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")
# A named body of work.  Kept deterministic because the projects field controls
# which chats can read the memory, so it must not depend on a model's judgement.
_DIRECT_PROJECT = re.compile(
    r"^(?:I\s+(?:am|'m)\s+(?:currently\s+)?(?:working\s+on|building)|"
    r"My\s+project\s+is(?:\s+called)?|"
    r"This\s+project\s+is(?:\s+called)?)\s+"
    r"(?:a\s+project\s+called\s+|the\s+project\s+called\s+|a\s+project\s+named\s+)?"
    # "I am working on my fitness" is a personal pursuit, not a named piece of
    # work.  Possessive phrasing is ambiguous, so it is left to the model rather
    # than forced into the projects field, which controls who can read it.
    r"(?!(?:my|our|your|his|her|their|the)\s)"
    r"(?P<value>[^.?!]+)\.?$",
    re.IGNORECASE,
)

_TRANSIENT_LOCATION = re.compile(
    r"^I\s+am\s+(?:visiting\s+[^.?!]+|in\s+[^.?!]+\s+right\s+now)\.?$",
    re.IGNORECASE,
)
_CATEGORY_CORRECTION = re.compile(
    r"^(?:That|What\s+I\s+said)\s+is\s+a\s+goal,\s+not\s+a\s+preference\.?$",
    re.IGNORECASE,
)
_ADDITIVE_GOALS = re.compile(
    r"^I\s+still\s+want\s+(?P<first>.+?),\s*and\s+I\s+also\s+want\s+(?P<second>.+?)\.?$",
    re.IGNORECASE,
)
_DIRECT_GOAL = re.compile(r"^I\s+want\s+to\s+(?P<value>.+?)\.?$", re.IGNORECASE)
_NOW_GOAL = re.compile(r"^Now\s+I\s+want\s+(?:to\s+)?(?P<value>.+?)\.?$", re.IGNORECASE)
_DIRECT_PREFERENCE = re.compile(r"^I\s+prefer\s+(?P<value>.+?)\.?$", re.IGNORECASE)
_DOMAIN_PREFERENCE = re.compile(
    r"^For\s+video[- ]editing\s+advice,\s*(?P<value>.+?)\.?$",
    re.IGNORECASE,
)
_GLOBAL_STYLE = re.compile(r"^Always\s+(?P<value>answer\s+me\s+.+?)\.?$", re.IGNORECASE)
_GOAL_AND_GLOBAL_STYLE = re.compile(
    r"^I\s+want\s+to\s+(?P<goal>[^.]+)\.\s*"
    r"Always\s+(?P<preference>answer\s+me\s+[^.]+)\.?$",
    re.IGNORECASE,
)
_REMEMBER = re.compile(r"^Remember(?:\s+that|:)?\s+(?P<value>.+?)\.?$", re.IGNORECASE)
_EXPLICIT_LIFECYCLE = re.compile(
    r"^(?P<action>forget|archive|erase permanently|restore)\s+(?P<value>.+?)\.?$",
    re.IGNORECASE,
)
_TEMPORARY = re.compile(
    r"\b(?:right now|today|at the moment|currently drinking|have a headache|"
    r"temporary|for this turn)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(
    r"\b(?:maybe|perhaps|might|could|someday|if I|imagine)\b",
    re.IGNORECASE,
)
_THIRD_PARTY = re.compile(
    r"^(?:My friend|My colleague|My partner|He|She|They)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_PRONOUN = re.compile(r"^(?:That|It|This)\b", re.IGNORECASE)


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip(" .\t\n").split())


def _has_multiple_statements(value: str) -> bool:
    """Keep whole-message ignore rules from masking mixed prose fallback."""

    return bool(re.search(r"[.!?]\s+\S", value))


def _video_verb(value: str) -> str:
    cleaned = _clean(value)
    return re.sub(r"^(?:make|making)\b", "create", cleaned, flags=re.IGNORECASE)


def _span(
    request: ExtractionRequest,
    match: re.Match[str],
    group: str,
    *,
    value: str | None = None,
    memory_type: MemoryType | None = None,
    domain: str | None = None,
    slot: str | None = None,
    correction_group: str | None = None,
    additive: bool = False,
    additional_source_spans: tuple[PreparsedSourceSpan, ...] = (),
    explicit_type_change: bool = False,
) -> PreparsedSpan:
    start, end = match.span(group)
    return PreparsedSpan(
        message_id=request.message_id,
        start=start,
        end=end,
        quoted_text=request.user_message[start:end],
        normalized_value=value or _clean(match.group(group)),
        memory_type_hint=memory_type.value if memory_type else None,
        domain_hint=domain,
        slot_hint=slot,
        correction_group=correction_group,
        additive=additive,
        additional_source_spans=additional_source_spans,
        explicit_type_change=explicit_type_change,
    )


def _category_reference(request: ExtractionRequest) -> PreparsedSpan | None:
    recent_user = next(
        (
            item
            for item in reversed(request.supporting_window)
            if item.role is ConversationRole.USER
        ),
        None,
    )
    if recent_user is None:
        return None
    prior_text = recent_user.content.strip()
    match = _IMPLICIT_GOAL_CORRECTION.fullmatch(prior_text)
    group = "new"
    if match is None:
        match = _DIRECT_GOAL.fullmatch(prior_text)
        group = "value"
    if match is None:
        match = _NOW_GOAL.fullmatch(prior_text)
        group = "value"
    if match is None:
        return None
    start, end = match.span(group)
    value = _video_verb(match.group(group))
    domain = "video_creation" if re.search(r"video|youtube|reels", value, re.I) else None
    return PreparsedSpan(
        message_id=recent_user.message_id,
        start=start,
        end=end,
        quoted_text=recent_user.content[start:end],
        normalized_value=value,
        memory_type_hint=MemoryType.GOAL.value,
        domain_hint=domain,
        slot_hint="current_primary_goal" if domain else None,
        explicit_type_change=True,
    )


def _compound_corrections(request: ExtractionRequest) -> PreparseResult | None:
    """Parse consecutive explicit corrections without joining their meanings.

    A user may correct more than one independent memory in a single turn.  The
    single-correction grammars below intentionally use ``fullmatch``; this
    bounded scanner composes only complete, adjacent known correction pairs and
    leaves every other mixed statement for the model/review path.
    """

    text = request.user_message.strip()
    cursor = 0
    assertions: list[PreparsedSpan] = []
    retractions: list[PreparsedSpan] = []
    group = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        match = _GOAL_CORRECTION_PART.match(text, cursor)
        if match is not None:
            group += 1
            correction_group = f"foreground-correction-{group}"
            assertions.append(
                _span(
                    request,
                    match,
                    "new",
                    value=_video_verb(match.group("new")),
                    memory_type=MemoryType.GOAL,
                    domain="video_creation",
                    slot="current_primary_goal",
                    correction_group=correction_group,
                )
            )
            retractions.append(
                _span(
                    request,
                    match,
                    "old",
                    value=_video_verb(match.group("old")),
                    memory_type=MemoryType.GOAL,
                    domain="video_creation",
                    slot="current_primary_goal",
                    correction_group=correction_group,
                )
            )
            cursor = match.end()
            continue
        match = _PREFERENCE_CORRECTION_PART.match(text, cursor)
        if match is not None:
            group += 1
            correction_group = f"foreground-correction-{group}"
            assertions.append(
                _span(
                    request,
                    match,
                    "new",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                    correction_group=correction_group,
                )
            )
            retractions.append(
                _span(
                    request,
                    match,
                    "old",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                    correction_group=correction_group,
                )
            )
            cursor = match.end()
            continue
        return None

    if group < 2:
        return None
    return PreparseResult(
        kind=PreparseKind.DETERMINISTIC_CORRECTION,
        reason="consecutive_explicit_corrections",
        assertions=tuple(assertions),
        retractions=tuple(retractions),
        deterministic=True,
    )


def _identity_facts(request: ExtractionRequest, text: str) -> PreparseResult | None:
    """Extract durable first-person identity facts sentence by sentence.

    The single-statement grammars above use ``fullmatch``, so an ordinary message
    that states several facts at once ("i am 21 years old. I am from new delhi.
    i currently work at ey gds") matched nothing and fell through to the model.
    Each fact here is unambiguous on its own, so parse them directly and give
    each one a stable ``identity:global:<slot>`` home that recall can target.
    Every sentence must be recognised; a single unknown sentence hands the whole
    message back to the model path rather than silently keeping only part of it.
    """

    base = request.user_message.find(text)
    if base < 0:
        return None
    sentences = [
        (match.start(), match.group())
        for match in _SENTENCE.finditer(text)
        if match.group().strip()
    ]
    if len(sentences) < 2:
        return None

    assertions: list[PreparsedSpan] = []
    for offset, raw_sentence in sentences:
        stripped = raw_sentence.strip()
        lead = offset + (len(raw_sentence) - len(raw_sentence.lstrip()))
        for pattern, slot in _IDENTITY_SENTENCE_RULES:
            match = pattern.fullmatch(stripped)
            if match is None:
                continue
            start, end = match.span("value")
            absolute = base + lead
            assertions.append(
                PreparsedSpan(
                    message_id=request.message_id,
                    start=absolute + start,
                    end=absolute + end,
                    quoted_text=request.user_message[absolute + start : absolute + end],
                    normalized_value=_clean(match.group("value")),
                    memory_type_hint=MemoryType.IDENTITY.value,
                    domain_hint="global",
                    slot_hint=slot,
                )
            )
            break
        else:
            return None

    if not assertions:
        return None
    return PreparseResult(
        kind=PreparseKind.DETERMINISTIC_ASSERTION,
        reason="durable_first_person_identity_facts",
        assertions=tuple(assertions),
        deterministic=True,
    )


def preparse(request: ExtractionRequest) -> PreparseResult:
    text = request.user_message.strip()
    if _THIRD_PARTY.search(text):
        return PreparseResult(
            kind=PreparseKind.IGNORE,
            reason="third_party_subject",
            third_party=True,
        )
    if _HYPOTHETICAL.search(text) and not _has_multiple_statements(text):
        return PreparseResult(
            kind=PreparseKind.IGNORE,
            reason="hypothetical_language",
            hypothetical=True,
        )
    if _TRANSIENT_LOCATION.fullmatch(text):
        return PreparseResult(
            kind=PreparseKind.IGNORE,
            reason="transient_location_not_residence",
            temporary=True,
        )
    if _TEMPORARY.search(text) and not re.search(
        r"\b(?:I am a|I work as|I prefer|my ongoing|my goal)\b",
        text,
        re.IGNORECASE,
    ):
        return PreparseResult(
            kind=PreparseKind.IGNORE,
            reason="temporary_state",
            temporary=True,
        )
    if text.endswith("?"):
        return PreparseResult(
            kind=PreparseKind.IGNORE,
            reason="question_not_memory",
            question=True,
        )

    category = _CATEGORY_CORRECTION.fullmatch(text)
    if category:
        current_span = PreparsedSourceSpan(
            message_id=request.message_id,
            start=0,
            end=len(text),
            quoted_text=request.user_message[: len(text)],
        )
        referenced = _category_reference(request)
        if referenced is not None:
            assertion = referenced.model_copy(update={"additional_source_spans": (current_span,)})
            return PreparseResult(
                kind=PreparseKind.DETERMINISTIC_ASSERTION,
                reason="bounded_recent_goal_category_correction",
                assertions=(assertion,),
                deterministic=True,
            )
        return PreparseResult(
            kind=PreparseKind.AMBIGUOUS,
            reason="category_reference_unresolved",
            assertions=(
                PreparsedSpan(
                    message_id=request.message_id,
                    start=0,
                    end=len(text),
                    quoted_text=request.user_message[: len(text)],
                    normalized_value=_clean(text),
                    memory_type_hint=MemoryType.GOAL.value,
                    domain_hint="global",
                    additional_source_spans=(),
                    explicit_type_change=True,
                ),
            ),
            ambiguous_subject=True,
        )

    current_location = _CURRENT_LOCATION.fullmatch(text)
    if current_location:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="durable_first_person_current_location",
            assertions=(
                _span(
                    request,
                    current_location,
                    "value",
                    memory_type=MemoryType.IDENTITY,
                    domain="global",
                    slot="current_location",
                ),
            ),
            deterministic=True,
        )

    for pattern, slot in _IDENTITY_SENTENCE_RULES:
        single_fact = pattern.fullmatch(text)
        if single_fact is None:
            continue
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason=f"durable_first_person_{slot}",
            assertions=(
                _span(
                    request,
                    single_fact,
                    "value",
                    memory_type=MemoryType.IDENTITY,
                    domain="global",
                    slot=slot,
                ),
            ),
            deterministic=True,
        )

    identity_facts = _identity_facts(request, text)
    if identity_facts is not None:
        return identity_facts

    direct_project = _DIRECT_PROJECT.fullmatch(text)
    if direct_project:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="durable_named_project",
            assertions=(
                _span(
                    request,
                    direct_project,
                    "value",
                    memory_type=MemoryType.PROJECT,
                    domain="global",
                ),
            ),
            deterministic=True,
        )

    if text.casefold().startswith("remember these ") and ":" in text:
        header, body = text.split(":", 1)
        lines = [line.strip(" -\t") for line in body.splitlines() if line.strip(" -\t")]
        if not lines:
            return PreparseResult(kind=PreparseKind.AMBIGUOUS, reason="empty_explicit_batch")
        assertions = []
        search_from = len(header) + 1
        for line in lines:
            start = request.user_message.find(line, search_from)
            if start < 0:
                return PreparseResult(
                    kind=PreparseKind.AMBIGUOUS,
                    reason="batch_span_not_found",
                )
            end = start + len(line)
            search_from = end
            assertions.append(
                PreparsedSpan(
                    message_id=request.message_id,
                    start=start,
                    end=end,
                    quoted_text=request.user_message[start:end],
                    normalized_value=_clean(line),
                    memory_type_hint=MemoryType.KNOWLEDGE.value,
                    domain_hint=(
                        "software_development"
                        if re.search(r"\b(?:python|rust|code|programming)\b", line, re.I)
                        else "learning"
                    ),
                    additive=True,
                )
            )
        return PreparseResult(
            kind=PreparseKind.EXPLICIT_BATCH,
            reason="explicit_batch_grammar",
            assertions=tuple(assertions),
            explicit_memory_intent=True,
            deterministic=True,
        )

    lifecycle = _EXPLICIT_LIFECYCLE.fullmatch(text)
    if lifecycle:
        action = lifecycle.group("action").casefold()
        hint = {
            "forget": LifecycleHint.FORGET,
            "archive": LifecycleHint.ARCHIVE,
            "erase permanently": LifecycleHint.ERASE_PERMANENTLY,
            "restore": LifecycleHint.RESTORE,
        }[action]
        return PreparseResult(
            kind=PreparseKind.EXPLICIT_LIFECYCLE,
            reason="explicit_lifecycle_grammar",
            retractions=(_span(request, lifecycle, "value"),),
            lifecycle_hint=hint,
            explicit_memory_intent=True,
            deterministic=True,
        )

    compound_corrections = _compound_corrections(request)
    if compound_corrections is not None:
        return compound_corrections

    goal_and_global_style = _GOAL_AND_GLOBAL_STYLE.fullmatch(text)
    if goal_and_global_style:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="separate_goal_and_global_preference",
            assertions=(
                _span(
                    request,
                    goal_and_global_style,
                    "goal",
                    value=_video_verb(goal_and_global_style.group("goal")),
                    memory_type=MemoryType.GOAL,
                    domain="video_creation",
                    slot="current_primary_goal",
                ),
                _span(
                    request,
                    goal_and_global_style,
                    "preference",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                ),
            ),
            deterministic=True,
        )

    correction = _IMPLICIT_GOAL_CORRECTION.fullmatch(text)
    if correction:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_CORRECTION,
            reason="linked_goal_retraction_assertion",
            assertions=(
                _span(
                    request,
                    correction,
                    "new",
                    value=_video_verb(correction.group("new")),
                    memory_type=MemoryType.GOAL,
                    domain="video_creation",
                    slot="current_primary_goal",
                    correction_group="foreground-correction-1",
                ),
            ),
            retractions=(
                _span(
                    request,
                    correction,
                    "old",
                    value=_video_verb(correction.group("old")),
                    memory_type=MemoryType.GOAL,
                    domain="video_creation",
                    slot="current_primary_goal",
                    correction_group="foreground-correction-1",
                ),
            ),
            deterministic=True,
        )

    replacement = _EXPLICIT_REPLACE.fullmatch(text)
    if replacement:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_CORRECTION,
            reason="explicit_replace_grammar",
            assertions=(
                _span(
                    request,
                    replacement,
                    "new",
                    memory_type=MemoryType.GOAL,
                    correction_group="foreground-correction-1",
                ),
            ),
            retractions=(
                _span(
                    request,
                    replacement,
                    "old",
                    memory_type=MemoryType.GOAL,
                    correction_group="foreground-correction-1",
                ),
            ),
            deterministic=True,
        )

    preference = _PREFERENCE_CORRECTION.fullmatch(text)
    if preference:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_CORRECTION,
            reason="linked_preference_retraction_assertion",
            assertions=(
                _span(
                    request,
                    preference,
                    "new",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                    correction_group="foreground-correction-1",
                ),
            ),
            retractions=(
                _span(
                    request,
                    preference,
                    "old",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                    correction_group="foreground-correction-1",
                ),
            ),
            deterministic=True,
        )

    location = _PURE_LOCATION_RETRACTION.fullmatch(text)
    if location:
        return PreparseResult(
            kind=PreparseKind.EXPLICIT_LIFECYCLE,
            reason="grounded_location_retraction",
            retractions=(
                _span(
                    request,
                    location,
                    "old",
                    memory_type=MemoryType.IDENTITY,
                    domain="global",
                    slot="current_location",
                ),
            ),
            lifecycle_hint=LifecycleHint.ARCHIVE,
            deterministic=True,
        )

    additive = _ADDITIVE_GOALS.fullmatch(text)
    if additive:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="explicit_additive_language",
            assertions=(
                _span(
                    request,
                    additive,
                    "first",
                    memory_type=MemoryType.GOAL,
                    additive=True,
                ),
                _span(
                    request,
                    additive,
                    "second",
                    memory_type=MemoryType.GOAL,
                    additive=True,
                ),
            ),
            deterministic=True,
        )

    domain_preference = _DOMAIN_PREFERENCE.fullmatch(text)
    if domain_preference:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="domain_specific_preference",
            assertions=(
                _span(
                    request,
                    domain_preference,
                    "value",
                    memory_type=MemoryType.PREFERENCE,
                    domain="video_creation",
                    slot="practice_advice_format",
                ),
            ),
            deterministic=True,
        )

    global_style = _GLOBAL_STYLE.fullmatch(text)
    if global_style:
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="global_response_style",
            assertions=(
                _span(
                    request,
                    global_style,
                    "value",
                    memory_type=MemoryType.PREFERENCE,
                    domain="global",
                    slot="verbosity",
                ),
            ),
            deterministic=True,
        )

    direct_goal = _DIRECT_GOAL.fullmatch(text)
    if direct_goal and re.search(
        r"\b(?:video|youtube|reels|learn|career|fitness|travel)\b",
        direct_goal.group("value"),
        re.IGNORECASE,
    ):
        value = _video_verb(direct_goal.group("value"))
        domain = "video_creation" if re.search(r"video|youtube|reels", value, re.I) else None
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="durable_first_person_goal",
            assertions=(
                _span(
                    request,
                    direct_goal,
                    "value",
                    value=value,
                    memory_type=MemoryType.GOAL,
                    domain=domain,
                    slot="current_primary_goal" if domain == "video_creation" else None,
                ),
            ),
            deterministic=domain is not None,
        )

    direct_preference = _DIRECT_PREFERENCE.fullmatch(text)
    if direct_preference:
        value = direct_preference.group("value")
        domain = "learning" if re.search(r"learning|courses", value, re.I) else None
        return PreparseResult(
            kind=(PreparseKind.DETERMINISTIC_ASSERTION if domain else PreparseKind.MODEL_REQUIRED),
            reason="first_person_preference",
            assertions=(
                _span(
                    request,
                    direct_preference,
                    "value",
                    memory_type=MemoryType.PREFERENCE,
                    domain=domain,
                    slot="learning_format" if domain else None,
                ),
            ),
            deterministic=domain is not None,
        )

    remember = _REMEMBER.fullmatch(text)
    if remember:
        remembered = remember.group("value")
        if re.search(r"\b(?:python|rust|programming|code)\b", remembered, re.I):
            domain = "software_development"
        elif re.search(r"\b(?:learn|learning|study)\b", remembered, re.I):
            domain = "learning"
        elif re.search(r"\b(?:video|youtube|reels)\b", remembered, re.I):
            domain = "video_creation"
        else:
            domain = None
        return PreparseResult(
            kind=PreparseKind.DETERMINISTIC_ASSERTION,
            reason="explicit_remember_grammar",
            assertions=(
                _span(
                    request,
                    remember,
                    "value",
                    memory_type=MemoryType.KNOWLEDGE,
                    domain=domain,
                    additive=True,
                ),
            ),
            explicit_memory_intent=True,
            deterministic=domain is not None,
        )

    now_goal = _NOW_GOAL.fullmatch(text)
    if now_goal:
        return PreparseResult(
            kind=PreparseKind.AMBIGUOUS,
            reason="unlinked_current_goal_assertion",
            assertions=(_span(request, now_goal, "value", memory_type=MemoryType.GOAL),),
            ambiguous_subject=False,
        )
    if _AMBIGUOUS_PRONOUN.search(text):
        return PreparseResult(
            kind=PreparseKind.MODEL_REQUIRED,
            reason="pronoun_requires_grounded_context",
            ambiguous_subject=True,
        )
    return PreparseResult(kind=PreparseKind.MODEL_REQUIRED, reason="bounded_model_required")


def deterministic_model_response(result: PreparseResult) -> ModelProposalResponse:
    assertions = tuple(
        ModelAssertionProposal(
            proposal_id=f"assertion-{index}",
            source_spans=(
                ModelSourceSpan(
                    message_id=item.message_id,
                    start=item.start,
                    end=item.end,
                    quoted_text=item.quoted_text,
                ),
                *(
                    ModelSourceSpan(
                        message_id=extra.message_id,
                        start=extra.start,
                        end=extra.end,
                        quoted_text=extra.quoted_text,
                    )
                    for extra in item.additional_source_spans
                ),
            ),
            subject_hint=SubjectHint.USER,
            memory_type_hint=MemoryType(item.memory_type_hint or MemoryType.KNOWLEDGE.value),
            domain_hint=item.domain_hint,
            slot_hint=item.slot_hint,
            typed_value=item.normalized_value,
            display_hint=item.normalized_value,
            durability=DurabilityHint.DURABLE,
            confidence=0.99,
            sensitivity_hint=Sensitivity.NORMAL,
            correction_group=item.correction_group,
            additive=item.additive,
            explicit_type_change=item.explicit_type_change,
            explicit_domain_change=item.explicit_domain_change,
            explicit_slot_change=item.explicit_slot_change,
        )
        for index, item in enumerate(result.assertions)
    )
    retractions = tuple(
        ModelRetractionProposal(
            proposal_id=f"retraction-{index}",
            source_spans=(
                ModelSourceSpan(
                    message_id=item.message_id,
                    start=item.start,
                    end=item.end,
                    quoted_text=item.quoted_text,
                ),
            ),
            subject_hint=SubjectHint.USER,
            old_value_hint=item.normalized_value,
            memory_type_hint=(MemoryType(item.memory_type_hint) if item.memory_type_hint else None),
            domain_hint=item.domain_hint,
            slot_hint=item.slot_hint,
            confidence=0.99,
            correction_group=item.correction_group,
            explicit_forget=result.lifecycle_hint is LifecycleHint.FORGET,
        )
        for index, item in enumerate(result.retractions)
    )
    return ModelProposalResponse(assertions=assertions, retractions=retractions)
