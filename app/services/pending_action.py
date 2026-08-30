"""Semantic reading of a reply typed under a calendar proposal.

Resolving a proposal -- actually approving or declining it -- is not this
module's job and never reaches it. That happens through the proposal card's
own buttons, which call ``POST /calendar/proposals/{message_id}/approve``
(or ``/decline``); the outcome is stamped onto that message's
``calendar_proposal`` metadata as ``status``, so it is durable, survives a
reload, and is decided by exactly one thing -- a click. This module only
answers what a *typed* reply means, and the only outcome it can act on is
``"modify"``, which produces a new proposal that in turn needs its own
click.

Two lifecycles are deliberately kept apart:

* **Resolution** lives in the message's own metadata and is
  position-independent. A card is actionable wherever it sits in the
  transcript, and a resolved one is inert no matter what follows it.
* **The conversational hold** -- whether a typed "make it 4pm" gets routed
  to the modify path at all -- is about proximity to the tail of the
  transcript, and is what ``find_pending_calendar_proposal`` decides. It is
  bounded (see ``reask_count``) so a proposal can never capture the
  conversation indefinitely.

Because resolution no longer depends on position, nothing needs to drag a
proposal back to the tail to keep it approvable, and no reply ever
re-persists a draft it did not change.

What a reply *means* is decided by the model, not by matching it against a
fixed phrase list: a regex can't tell "yes, but make it 5pm" apart from
"yes" the way a semantic judgment can, and it can't tell an edit of the
event on screen ("make it 4pm") apart from a wholly new request ("schedule
a haircut next Friday") -- which is exactly the distinction that decides
whether the proposal keeps the turn or lets go of it.

Modelled on ``app.services.chat_intent``'s shape (frozen dataclass, pure
resolver function) and ``app.services.calendar.intent``'s classifier shape
(JSON-only system prompt, fail-closed on any parse error or low confidence).

This module never grants authorization: it only ever labels a reply against
a proposal that was already built and validated when it was first proposed.
It does not construct or supply a calendar payload, and it never calls a
mutating ``CalendarService`` method -- see
``app.services.calendar.service.execute_calendar_proposal`` for the single
path that does, reached only from the approve route.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from app.services.llm import LLMMessage

if TYPE_CHECKING:
    from app.models.chat import ChatMessage
    from app.services.llm import LLMClient

PendingActionOutcome = Literal[
    "confirm", "decline", "modify", "new_request", "unrelated", "ambiguous"
]


class PendingActionDecision(BaseModel):
    outcome: PendingActionOutcome = "ambiguous"
    confidence: float = 0.0


#: ``response_kind`` of the reply Neo gives when a typed message lands under
#: an unresolved proposal but isn't an edit of it -- a "yes", a "no", or
#: anything it can't place. It points at the card rather than acting, and
#: carries ``calendar_proposal_ref`` so the *next* typed message can still be
#: read as a modification of that same proposal.
PROPOSAL_REASK_KIND = "calendar_proposal_reask"

#: How many times in a row Neo may point back at the same card before letting
#: go of the turn. The escape that matters is ``"new_request"`` -- this is only
#: the backstop for a model that keeps mislabelling, so that a proposal can
#: never hold the conversation forever.
MAX_REASKS = 2


@dataclass(frozen=True)
class PendingCalendarProposal:
    """The already-validated proposal state carried on a proposal message's
    metadata -- nothing new is invented here, this just reads it."""

    action: Literal["create", "update", "delete"]
    event_id: str | None
    event_title: str | None
    draft: dict | None
    source_message_id: int
    #: How many times Neo has already pointed at this card without acting.
    reask_count: int = 0


def calendar_proposal_metadata(message: ChatMessage) -> dict | None:
    """The ``calendar_proposal`` block on a proposal message, or ``None``.

    Fails closed on a non-proposal message and on any parse error, so every
    caller can treat "no metadata" and "unreadable metadata" identically.
    """
    if getattr(message, "role", None) != "assistant":
        return None
    if getattr(message, "response_kind", None) != "calendar_proposal":
        return None
    try:
        metadata = json.loads(message.metadata_json) if message.metadata_json else {}
        proposal = metadata["calendar_proposal"]
    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return proposal if isinstance(proposal, dict) else None


def calendar_proposal_status(message: ChatMessage) -> str | None:
    """``"approved"``/``"declined"`` once the card has been clicked, else ``None``.

    A proposal with a status is finished: it can never be executed again and
    it never holds the conversation, wherever it sits in the transcript.
    """
    proposal = calendar_proposal_metadata(message)
    status = (proposal or {}).get("status")
    return status if isinstance(status, str) and status else None


def _proposal_from_metadata(
    proposal: dict, *, message_id: int, reask_count: int = 0
) -> PendingCalendarProposal | None:
    action = proposal.get("action")
    if action not in ("create", "update", "delete"):
        return None
    return PendingCalendarProposal(
        action=action,
        event_id=proposal.get("event_id"),
        event_title=proposal.get("event_title"),
        draft=proposal.get("draft"),
        source_message_id=message_id,
        reask_count=reask_count,
    )


def read_calendar_proposal(message: ChatMessage) -> PendingCalendarProposal | None:
    """Read one message's proposal regardless of where it sits or whether it
    is resolved -- what the approve/decline routes need. Callers that care
    about resolution check ``calendar_proposal_status`` themselves."""
    proposal = calendar_proposal_metadata(message)
    if proposal is None:
        return None
    return _proposal_from_metadata(proposal, message_id=message.id)


def find_pending_calendar_proposal(
    history: list[ChatMessage],
) -> PendingCalendarProposal | None:
    """The proposal, if any, that still has hold of the conversation.

    That is true in exactly two cases, both anchored at the tail so a
    proposal can only ever capture the message that directly follows it:

    * the last message is an unresolved proposal, or
    * the last message is a re-ask pointing back at one that is still
      unresolved -- which is how a typed "make it 4pm" still reaches the
      modify path after Neo has once said it couldn't tell what to change.

    A proposal whose card has been clicked holds nothing: ``status`` is set,
    so it is skipped here even though it may still be the last message.
    Fails closed to ``None`` on an empty history or any parse error.
    """
    if not history:
        return None
    last = history[-1]
    if getattr(last, "role", None) != "assistant":
        return None

    if last.response_kind == "calendar_proposal":
        if calendar_proposal_status(last) is not None:
            return None
        proposal = calendar_proposal_metadata(last)
        if proposal is None:
            return None
        return _proposal_from_metadata(proposal, message_id=last.id)

    if last.response_kind != PROPOSAL_REASK_KIND:
        return None
    try:
        metadata = json.loads(last.metadata_json) if last.metadata_json else {}
        reference = metadata["calendar_proposal_ref"]
        message_id = int(reference["message_id"])
        reask_count = int(reference.get("reask_count") or 0)
    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None
    if reask_count >= MAX_REASKS:
        return None
    for message in reversed(history):
        if message.id != message_id:
            continue
        if calendar_proposal_status(message) is not None:
            return None
        proposal = calendar_proposal_metadata(message)
        if proposal is None:
            return None
        return _proposal_from_metadata(
            proposal, message_id=message.id, reask_count=reask_count
        )
    return None


_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)
MIN_CONFIDENCE = 0.6

_SYSTEM_PROMPT = """You are Neo's conservative pending-action reply classifier.

Neo proposed a calendar change and is showing the user a draft card with
Approve and Decline buttons. Decide what the user's typed reply means with
respect to that specific draft -- do not pattern-match on surface wording
alone; judge the intent the way a careful person would.

Return exactly one JSON object with no Markdown or extra text, where
"outcome" is one of confirm, decline, modify, new_request, unrelated,
ambiguous, and "confidence" is between 0.0 and 1.0:
{"outcome": "confirm", "confidence": 0.9}

Rules:
- "confirm": an unqualified approval of exactly the proposal shown, nothing
  added or changed.
- "decline": an unqualified rejection or cancellation of the proposal shown.
- "modify": a change to a detail OF THE EVENT SHOWN -- its time, its date,
  or its wording -- while still meaning that same event. "Make it 4pm"
  changes the dentist appointment; it does not ask for a second one.
- "new_request": the user is asking for a DIFFERENT calendar action --
  another event, a different subject or activity, or an additional
  appointment alongside this one. The draft on screen is no longer what
  they are talking about. Prefer this over "modify" whenever the reply
  names an activity or event that is not the one shown.
- "unrelated": a new question or topic change that has nothing to do with
  the calendar at all.
- "ambiguous": a reply that could plausibly be about the proposal but does
  not clearly fit any of the above -- a vague acknowledgement, a one-word
  reply you can't confidently place, or a statement of uncertainty.
- Default confidence low (below 0.6) for anything you are not confident
  about; a low-confidence answer is treated the same as "ambiguous"
  downstream regardless of which outcome you pick, so it is always safe to
  say what you actually think and mark your confidence honestly.

The distinction that matters most is "modify" versus "new_request": one
edits the event on screen, the other replaces it with a different request.
Ask yourself whether the user is still talking about the same appointment.

These examples teach the boundary; use judgment for anything that doesn't
match them exactly -- do not treat this as a fixed phrase list.

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: yes
JSON: {"outcome": "confirm", "confidence": 0.98}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: yes, do it
JSON: {"outcome": "confirm", "confidence": 0.97}

PROPOSAL: delete "Team sync"
USER: no, leave it
JSON: {"outcome": "decline", "confidence": 0.95}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: don't add it
JSON: {"outcome": "decline", "confidence": 0.95}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: yes, but make it 4pm
JSON: {"outcome": "modify", "confidence": 0.9}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: actually make it tomorrow
JSON: {"outcome": "modify", "confidence": 0.85}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: call it Dental checkup instead
JSON: {"outcome": "modify", "confidence": 0.88}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: Schedule a haircut next Friday at 4pm.
JSON: {"outcome": "new_request", "confidence": 0.95}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: also add a team sync on Monday
JSON: {"outcome": "new_request", "confidence": 0.93}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: what's on my calendar next week?
JSON: {"outcome": "new_request", "confidence": 0.9}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: actually what's the weather like today
JSON: {"outcome": "unrelated", "confidence": 0.97}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: what is a mutex?
JSON: {"outcome": "unrelated", "confidence": 0.97}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: sounds good
JSON: {"outcome": "ambiguous", "confidence": 0.5}

PROPOSAL: create "Dentist appointment" at 2026-08-28T15:00:00-07:00
USER: I'm not sure
JSON: {"outcome": "ambiguous", "confidence": 0.3}
"""


def resolve_pending_action_reply(
    prompt: str,
    *,
    pending: PendingCalendarProposal,
    llm: LLMClient | None,
) -> PendingActionDecision:
    """Ask the model exactly once, every time a proposal holds the turn.

    There is no regex shortcut here -- interpreting what a reply means with
    respect to a specific draft is a semantic judgment (see the module
    docstring), not something a fixed phrase list can safely make. The only
    thing decided without the model is whether to call it at all, and that's
    a plain data lookup one level up in ``find_pending_calendar_proposal``.

    No outcome authorizes anything: only ``"modify"`` changes what Neo does,
    and it produces a fresh proposal that still needs its own click. So
    failing closed here costs a re-ask, never a wrong write.

    Fails closed to ``"ambiguous"`` whenever the outcome genuinely can't be
    determined: no LLM available, a blank prompt, malformed/unparseable
    model output, a schema validation failure, or confidence below
    ``MIN_CONFIDENCE``. The confidence floor applies uniformly -- including
    to ``"new_request"``, which earns its reliability from the prompt's
    modify-versus-new-request examples rather than from a lowered bar.
    """
    cleaned = (prompt or "").strip()
    if llm is None or not cleaned:
        return PendingActionDecision(outcome="ambiguous", confidence=0.0)

    proposal_summary = (
        f'{pending.action} "{pending.event_title or (pending.draft or {}).get("title")}"'
        + (
            f' at {(pending.draft or {}).get("start_at")}'
            if (pending.draft or {}).get("start_at")
            else ""
        )
    )
    try:
        raw = llm.chat(
            [
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=f"PROPOSAL: {proposal_summary}\nUSER: {cleaned}",
                ),
            ],
            temperature=0.0,
        )
        cleaned_response = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
        match = _JSON_BLOB.search(cleaned_response)
        payload = json.loads(match.group(0) if match else cleaned_response.strip())
        decision = PendingActionDecision.model_validate(payload)
    except (AttributeError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        return PendingActionDecision(outcome="ambiguous", confidence=0.0)
    if decision.confidence < MIN_CONFIDENCE:
        return PendingActionDecision(outcome="ambiguous", confidence=decision.confidence)
    return decision
