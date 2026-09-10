"""What a connected account is allowed to do, as a vocabulary the user reads.

This module is the single source of truth for which capabilities exist, what
each one means, and which ones are safe to grant without a further decision.
Nothing else may invent a capability id.

**Why capabilities and not scopes.** A provider's scopes are its own vocabulary,
they differ between providers, and they are not phrased for the person granting
them -- a bare "auth/gmail.send" URL is not a question anybody can answer. A
capability is the question: *may Neo send mail as you?* One capability may
need several scopes, and the same capability needs entirely different scopes
on a different provider.

So the mapping is split, and the split is the point:

* this module owns the capability ids, their wording, and their risk grade --
  all provider-agnostic;
* each ``providers/<name>.py`` owns the scope strings that capability costs on
  that provider, behind ``Provider.scopes_for``.

That is what lets ``providers/microsoft.py`` arrive later without touching a
line here, and it is why no scope string appears in this file. The plan called
this file the single source of truth for "capability to scope"; splitting it
this way is what makes that claim survivable once there are two providers,
because the half that varies lives with the thing it varies with.

**Granularity is the product requirement.** Connecting an account must never be
one unrestricted grant. Every capability is requested separately, so connecting
for the calendar never asks about mail, and a user who grants reading can
withhold sending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How exposed the user is if Neo misuses this capability. ``read`` observes,
#: ``write`` changes something only the user sees, and ``external`` is visible to
#: other people and cannot be taken back -- a sent email, an invite that fired a
#: notification on someone else's phone. Only the grade is decided here; what it
#: costs at call time is the agent permission overlay's business, and a tool
#: still declares its own ``risk`` independently.
CapabilityGrade = Literal["read", "write", "external"]


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    description: str
    grade: CapabilityGrade


def _capability(
    id: str, label: str, description: str, grade: CapabilityGrade
) -> tuple[str, Capability]:
    return id, Capability(id=id, label=label, description=description, grade=grade)


#: The whole vocabulary. Wording is user-facing: it is rendered verbatim on the
#: Connected Accounts screen next to a toggle, so it is written as a promise
#: about what Neo will do, not as a description of an API.
CAPABILITIES: dict[str, Capability] = dict(
    (
        _capability(
            "calendar.read",
            "Read your calendar",
            "See your events so Neo can answer questions about your schedule.",
            "read",
        ),
        _capability(
            "calendar.write",
            "Change your calendar",
            "Create, move and cancel events. Every change is confirmed with you first.",
            "external",
        ),
        _capability(
            "docs.read",
            "Read documents you open",
            "Read only the documents you pick or that Neo created. Not your whole drive.",
            "read",
        ),
        _capability(
            "docs.write",
            "Write documents",
            "Create new documents and edit ones Neo can already see. Edits are confirmed first.",
            "write",
        ),
        _capability(
            "mail.read",
            "Read your mail",
            "Read your inbox so Neo can tell you what matters and draft replies.",
            "read",
        ),
        _capability(
            "mail.send",
            "Send mail as you",
            "Send messages from your address. Every message is shown to you before it goes.",
            "external",
        ),
        _capability(
            "mail.organize",
            "Organise your mail",
            "Archive messages and change labels. Never deletes anything.",
            "write",
        ),
    )
)


class UnknownCapabilityError(ValueError):
    """A capability id that is not in the vocabulary was requested."""


def get(capability_id: str) -> Capability:
    try:
        return CAPABILITIES[capability_id]
    except KeyError as exc:
        raise UnknownCapabilityError(f"Unknown capability '{capability_id}'.") from exc


def validate(capability_ids: object) -> tuple[str, ...]:
    """Normalise a requested capability set, or refuse it.

    Fails closed and fails whole: one unknown id rejects the entire request
    rather than being dropped quietly. A grant that silently contained less than
    it was asked for would leave the user believing they had authorised
    something they had not, and the failure would surface much later as a tool
    that mysteriously does not work.

    Order is not meaningful, so the result is sorted and de-duplicated -- which
    also makes a stored capability list comparable by equality.
    """

    if isinstance(capability_ids, str) or not isinstance(capability_ids, (list, tuple, set)):
        raise UnknownCapabilityError("Capabilities must be a list of capability ids.")
    cleaned = {str(item).strip() for item in capability_ids if str(item).strip()}
    if not cleaned:
        raise UnknownCapabilityError("At least one capability is required.")
    unknown = sorted(cleaned - CAPABILITIES.keys())
    if unknown:
        raise UnknownCapabilityError(f"Unknown capability '{unknown[0]}'.")
    return tuple(sorted(cleaned))


def catalog() -> list[dict[str, str]]:
    """The vocabulary as the settings screen renders it."""

    return [
        {
            "id": capability.id,
            "label": capability.label,
            "description": capability.description,
            "grade": capability.grade,
        }
        for capability in CAPABILITIES.values()
    ]
