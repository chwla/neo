"""Server-controlled provenance labels and the values that carry them.

The contract (docs/security/data-isolation.md §4) requires three explicit
provenance states, with missing or malformed provenance defaulting to UNKNOWN and
never authorizing disclosure. Confidentiality must not depend on ambient context
propagating back to a caller, so provenance travels *inside* values: a
:class:`Labeled` wraps a value with its :class:`Label`, and that pairing survives
being returned across tasks, threads and processes.

Nothing here trusts a model, a client field, a summary or an external response to
set or clear a label. PUBLIC can be minted only by :func:`public`, and only from a
basis prefix this module recognises.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any

#: Basis prefixes that trusted server code may stamp onto a PUBLIC label. A basis
#: outside this set is treated as unverifiable, which collapses to UNKNOWN -- so a
#: forged ``{"state": "public", "basis": "model_said_so"}`` can never round-trip
#: into a usable PUBLIC label.
PUBLIC_BASES = ("user_input", "builtin", "web_result", "workspace", "search_query")

_STATES = ("public", "private", "unknown")


def _basis_is_trusted(basis: str | None) -> bool:
    if not isinstance(basis, str):
        return False
    head, separator, identity = basis.partition(":")
    return head in PUBLIC_BASES and bool(separator and identity.strip())


@dataclass(frozen=True)
class Label:
    """Provenance of a value: one of ``public``/``private``/``unknown``.

    Construct through the factories, never by trusting external data:
    :meth:`unknown`, :meth:`private`, and the module-level :func:`public`.
    """

    state: str
    tags: frozenset[str]
    basis: str | None = None
    reason: str | None = None

    @staticmethod
    def unknown(reason: str = "") -> Label:
        return Label(state="unknown", tags=frozenset(), basis=None, reason=reason or "unknown")

    @staticmethod
    def private(*tags: str) -> Label:
        cleaned = frozenset(t for t in tags if t)
        if not cleaned:
            # A private label with no source tag authorises nothing meaningfully and
            # is almost always a bug; fail closed rather than invent a tag.
            return Label.unknown("private_without_tags")
        return Label(state="private", tags=cleaned, basis=None)

    def is_public(self) -> bool:
        return (self.state == "public" and not self.tags
                and _basis_is_trusted(self.basis))

    def is_private(self) -> bool:
        return self.state == "private"

    def is_unknown(self) -> bool:
        return self.state == "unknown"


def public(basis: str) -> Label:
    """Mint a PUBLIC label. Trusted server code only.

    ``basis`` records *why* the value is public and must start with one of
    :data:`PUBLIC_BASES`. Anything else raises, so a caller cannot quietly launder
    private or unknown data into PUBLIC by passing an arbitrary string.
    """

    if not _basis_is_trusted(basis):
        raise ValueError(f"untrusted public basis: {basis!r}")
    return Label(state="public", tags=frozenset(), basis=basis)


def join(*labels: Label) -> Label:
    """Combine provenance conservatively.

    UNKNOWN dominates; any PRIVATE makes the result PRIVATE with the union of
    tags; the result is PUBLIC only when every input is verified PUBLIC; and the
    join of nothing is UNKNOWN (never an accidental PUBLIC).
    """

    if not labels:
        return Label.unknown("empty_join")
    tags: set[str] = set()
    saw_private = False
    for label in labels:
        label = label_of(label)
        if label.state == "unknown":
            return Label.unknown("join_with_unknown")
        if label.state == "private":
            saw_private = True
            tags |= set(label.tags)
        elif label.state != "public":
            return Label.unknown("join_with_unrecognised_state")
    if saw_private:
        return Label(state="private", tags=frozenset(tags), basis=None)
    bases = sorted({label.basis for label in labels if label.basis})
    return Label(state="public", tags=frozenset(), basis=",".join(bases) or "public")


@dataclass(frozen=True)
class Labeled:
    """A value paired with its provenance. The pairing is the mechanism."""

    value: Any
    label: Label


def label_of(obj: Any) -> Label:
    """The label of ``obj``: its own if it is :class:`Labeled`, else UNKNOWN.

    An unlabelled value -- a bare string, a dict read from somewhere untracked -- is
    UNKNOWN by construction, so forgetting to label something fails closed.
    """

    if isinstance(obj, Labeled):
        return label_of(obj.label)
    if isinstance(obj, Label):
        if not isinstance(obj.tags, frozenset) or not all(
            isinstance(tag, str) and tag for tag in obj.tags
        ):
            return Label.unknown("invalid_tags")
        if obj.is_public() or (obj.state == "private" and obj.tags and obj.basis is None):
            return obj
        return Label.unknown("invalid_or_unknown_label")
    return Label.unknown("unlabelled_value")


def to_json(label: Label) -> str:
    return json.dumps(
        {"state": label.state, "tags": sorted(label.tags), "basis": label.basis},
        sort_keys=True,
    )


def from_json(raw: str | None) -> Label:
    """Parse a stored label, failing closed to UNKNOWN.

    A null, non-JSON, wrong-shaped, unrecognised-state, or forged-PUBLIC blob all
    yield UNKNOWN. A PUBLIC blob is honoured only if its basis is trusted, so the
    serialized form cannot be used to launder provenance.
    """

    if not raw or not isinstance(raw, str):
        return Label.unknown("no_serialized_label")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return Label.unknown("malformed_label")
    if not isinstance(data, dict):
        return Label.unknown("malformed_label")
    state = data.get("state")
    tags = data.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return Label.unknown("malformed_tags")
    if state == "private":
        return Label.private(*tags)
    if state == "public":
        basis = data.get("basis")
        if not tags and _basis_is_trusted(basis):
            return Label(state="public", tags=frozenset(), basis=basis)
        return Label.unknown("untrusted_public_basis")
    return Label.unknown("unrecognised_state")


def content_hash(payload: Any) -> str:
    """A stable hash of a value's content, for binding a grant to a version."""

    import hashlib

    value = payload.value if isinstance(payload, Labeled) else payload
    if isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
    else:
        blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def invocation_label(inputs: Iterable[Any]) -> Label:
    """The label a model call inherits: the join of every input it receives.

    An unlabelled input contributes UNKNOWN, so a single untracked message makes
    the whole call UNKNOWN -- the conservative reading the contract requires.
    """

    labels = [label_of(item) for item in inputs]
    if not labels:
        return Label.unknown("no_inputs")
    return join(*labels)


def wrap_outputs(outputs: Iterable[Any], call_label: Label) -> list[Labeled]:
    """Carry a call's inherited label onto each of its outputs and tool arguments."""

    return [Labeled(value=out, label=call_label) for out in outputs]


async def gather_labeled(*awaitables: Any) -> Labeled:
    """Await several child tasks and return their results with the joined label.

    Each child is expected to return a :class:`Labeled`; an unlabelled return
    contributes UNKNOWN. The aggregate's label never depends on the caller's
    context -- only on what the children actually returned.
    """

    results = await asyncio.gather(*awaitables)
    labels = [label_of(r) for r in results]
    combined = join(*labels) if labels else Label.unknown("empty_gather")
    return Labeled(value=list(results), label=combined)


def run_in_thread_labeled(executor: Executor, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn`` in a worker thread and return its :class:`Labeled` result intact.

    The guarantee under test is that the label rides inside the returned value, so
    crossing the thread boundary cannot silently drop it.
    """

    return executor.submit(fn, *args, **kwargs).result()


__all__ = [
    "PUBLIC_BASES",
    "Label",
    "Labeled",
    "content_hash",
    "from_json",
    "gather_labeled",
    "invocation_label",
    "join",
    "label_of",
    "public",
    "run_in_thread_labeled",
    "to_json",
    "wrap_outputs",
]
