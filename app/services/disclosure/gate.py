"""The disclosure gate: the single authorization point for every outbound sink.

A payload reaches a sink only if its provenance (docs/security/data-isolation.md
§4) allows it:

* PUBLIC passes.
* PRIVATE needs a grant whose profile, engine, thread, sink, tags and content
  version all cover the payload, and which is neither expired nor revoked.
* UNKNOWN needs an explicit approval of the exact content.

The gate reads provenance from the value itself and consults a grant store. It
never trusts a model, a client field or content to authorize anything, and it
fails closed on anything it does not positively recognise.

This module is pure policy over synthetic, in-memory grants. Durable storage and
the HTTP surface that lets a user create grants are separate, deferred pieces; the
contract's binding and lifetime rules are enforced here regardless.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.services.disclosure.labels import Label, content_hash, label_of


class DisclosureBlocked(RuntimeError):
    """Raised by :func:`require` when a payload may not reach a sink."""


@dataclass(frozen=True)
class Destination:
    """Where a payload is headed, for scope binding."""

    profile_id: str
    engine: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class Approval:
    """A user's one-off approval of exact content (used for UNKNOWN payloads)."""

    content_hash: str
    profile_id: str


@dataclass
class Grant:
    """An authorization bound to profile, engines, thread, sink, tags and version."""

    id: str
    profile_id: str
    engines: frozenset[str]
    thread_scope: str | None
    tags: frozenset[str]
    content_hash: str
    sink: str
    created_at: float
    expires_at: float
    revoked: bool = False

    def active(self, now: float) -> bool:
        return not self.revoked and now < self.expires_at

    def covers(
        self, *, payload_tags: frozenset[str], payload_hash: str, sink: str, dest: Destination
    ) -> bool:
        if self.profile_id != dest.profile_id:
            return False
        if self.sink != sink:
            return False
        if self.engines and (dest.engine is None or dest.engine not in self.engines):
            return False
        if self.thread_scope is not None and self.thread_scope != dest.thread_id:
            return False
        if self.content_hash != payload_hash:
            return False
        return payload_tags <= self.tags


@dataclass(frozen=True)
class ApprovedCall:
    """A user's approval of one agent tool call, as claimed by the server.

    Built only by the agent loop, after it has atomically claimed the stored
    approval against the call's frozen argument hash. A tool receives it through
    the server-set tool context; nothing the model writes can produce one, and a
    mapping shaped like one is not one.
    """

    approval_id: str
    session_id: str
    call_id: str
    tool_name: str
    arguments_hash: str
    #: The private sources the run had read when the user approved -- what the
    #: approval prompt named. A request carrying any other source is not covered.
    tags: frozenset[str]
    #: Whether the run's context was of unrecorded origin when approved. An
    #: UNKNOWN payload is covered only when the approval said so explicitly.
    covers_unknown: bool = False


@dataclass
class ScopedGrant:
    """Authorization for the requests one approved tool call makes.

    A user approves a tool call's exact arguments; the requests the tool then sends
    are fixed functions of those arguments, but they are not the arguments, so they
    cannot be bound by content hash the way a :class:`Grant` is. This grant is the
    narrow alternative: it lives only in the call's own grant store, names the
    sinks and destinations it covers, covers only the private sources the approval
    named, and expires within the call. It authorizes; it never relabels.
    """

    id: str
    approval_id: str
    tags: frozenset[str]
    sinks: frozenset[str]
    destinations: frozenset[str]
    covers_unknown: bool
    created_at: float
    expires_at: float
    revoked: bool = False

    def active(self, now: float) -> bool:
        return not self.revoked and now < self.expires_at

    def covers(
        self, *, payload_tags: frozenset[str], unknown: bool, sink: str, dest: Destination
    ) -> bool:
        if sink not in self.sinks or dest.profile_id not in self.destinations:
            return False
        if unknown:
            return self.covers_unknown
        return bool(payload_tags) and payload_tags <= self.tags


class GrantStore:
    """An in-memory store of grants. A durable backend can replace this later."""

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._scoped: dict[str, ScopedGrant] = {}

    @classmethod
    def in_memory(cls) -> GrantStore:
        return cls()

    def create(
        self,
        *,
        profile_id: str,
        engines: frozenset[str],
        thread_scope: str | None,
        tags: frozenset[str],
        content_hash: str,
        sink: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> Grant:
        now = time.time() if now is None else now
        grant = Grant(
            id=uuid.uuid4().hex,
            profile_id=profile_id,
            engines=frozenset(engines),
            thread_scope=thread_scope,
            tags=frozenset(tags),
            content_hash=content_hash,
            sink=sink,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        self._grants[grant.id] = grant
        return grant

    def create_scoped(
        self,
        *,
        approval_id: str,
        tags: frozenset[str],
        sinks: frozenset[str],
        destinations: frozenset[str],
        ttl_seconds: float,
        covers_unknown: bool = False,
        now: float | None = None,
    ) -> ScopedGrant:
        now = time.time() if now is None else now
        grant = ScopedGrant(
            id=uuid.uuid4().hex,
            approval_id=approval_id,
            tags=frozenset(tags),
            sinks=frozenset(sinks),
            destinations=frozenset(destinations),
            covers_unknown=covers_unknown,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        self._scoped[grant.id] = grant
        return grant

    def revoke(self, grant_id: str) -> None:
        grant = self._grants.get(grant_id) or self._scoped.get(grant_id)
        if grant is not None:
            grant.revoked = True

    def revoke_all(self) -> None:
        for grant in (*self._grants.values(), *self._scoped.values()):
            grant.revoked = True

    def matching_scoped(
        self,
        *,
        payload_tags: frozenset[str],
        unknown: bool,
        sink: str,
        dest: Destination,
        now: float,
    ) -> ScopedGrant | None:
        for grant in self._scoped.values():
            if grant.active(now) and grant.covers(
                payload_tags=payload_tags, unknown=unknown, sink=sink, dest=dest
            ):
                return grant
        return None

    def matching(
        self,
        *,
        payload_tags: frozenset[str],
        payload_hash: str,
        sink: str,
        dest: Destination,
        now: float,
    ) -> Grant | None:
        for grant in self._grants.values():
            if grant.active(now) and grant.covers(
                payload_tags=payload_tags, payload_hash=payload_hash, sink=sink, dest=dest
            ):
                return grant
        return None


def authorize(
    sink: str,
    payload: Any,
    destination: Destination,
    *,
    grants: GrantStore | None = None,
    approval: Approval | None = None,
    now: float | None = None,
) -> Decision:
    """Decide whether ``payload`` may cross into ``sink`` toward ``destination``."""

    now = time.time() if now is None else now
    label: Label = label_of(payload)

    if label.is_public():
        return Decision(True, f"public:{label.basis}")

    if label.is_unknown():
        if approval is not None and approval.profile_id == destination.profile_id and (
            approval.content_hash == content_hash(payload)
        ):
            return Decision(True, "unknown_approved_exact_content")
        if grants is not None:
            scoped = grants.matching_scoped(
                payload_tags=frozenset(), unknown=True, sink=sink, dest=destination, now=now
            )
            if scoped is not None:
                return Decision(True, f"approved_call:{scoped.approval_id}")
        return Decision(False, "unknown provenance: needs explicit approval of exact content")

    # PRIVATE
    if grants is None:
        return Decision(False, "private payload with no grant store")
    grant = grants.matching(
        payload_tags=label.tags,
        payload_hash=content_hash(payload),
        sink=sink,
        dest=destination,
        now=now,
    )
    if grant is not None:
        return Decision(True, f"grant:{grant.id}")
    scoped = grants.matching_scoped(
        payload_tags=frozenset(label.tags), unknown=False, sink=sink, dest=destination, now=now
    )
    if scoped is not None:
        return Decision(True, f"approved_call:{scoped.approval_id}")
    return Decision(False, f"no active grant covers {sorted(label.tags)} -> {sink}")


def require(
    sink: str,
    payload: Any,
    destination: Destination,
    *,
    grants: GrantStore | None = None,
    approval: Approval | None = None,
    now: float | None = None,
) -> None:
    decision = authorize(sink, payload, destination, grants=grants, approval=approval, now=now)
    if not decision.allowed:
        raise DisclosureBlocked(decision.reason)


__all__ = [
    "Approval",
    "ApprovedCall",
    "ScopedGrant",
    "Decision",
    "Destination",
    "DisclosureBlocked",
    "Grant",
    "GrantStore",
    "authorize",
    "require",
]
