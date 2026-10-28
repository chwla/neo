"""The only place an external agent's input is assembled.

Everything a CLI is told passes through :func:`build_payload`, which takes typed,
labelled segments and returns an immutable record: the exact text, a hash of it, and
what was held back. Preview, approval and execution all read that one record, so
what the user approved and what the agent received cannot drift apart.

The rules it enforces (docs/security/data-isolation.md §4-5):

* A segment whose provenance is PUBLIC flows.
* A segment whose provenance is PRIVATE flows only under a grant covering this
  engine, profile, thread and that exact content version.
* A segment whose provenance is UNKNOWN never flows. A chat row with no recorded
  provenance -- every row written before this existed -- is UNKNOWN.

What this replaced: the previous builder copied the last eight chat messages into
every run unconditionally. That included answers Neo's own model had written *from
the user's memory*, and turns whose memory the user had since deleted (Neo's own
history substitutes a placeholder for those; this path did not). None of it was
previewed, and none of it was consented to.

Lineage is the one thing that still flows without a fresh grant, and only inside its
own disclosure scope: a turn produced by *this* engine on *this* thread. That keeps
"now do the same for the other module" working without letting an answer Claude
derived from a granted note become free input for Codex.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.disclosure import (
    Destination,
    GrantStore,
    Label,
    Labeled,
    authorize,
    content_hash,
    from_json,
    join,
    label_of,
    public,
)

#: Ceiling on the lineage block. Orientation, not a transcript.
MAX_LINEAGE_MESSAGES = 8
MAX_MESSAGE_CHARS = 1200
MAX_PREAMBLE_CHARS = 8000

SEGMENT_ORDER = ("instructions", "lineage", "changes", "objective")


@dataclass(frozen=True)
class Segment:
    """One labelled piece of what the agent is told."""

    kind: str
    text: str
    label: Label
    source_version: str


@dataclass(frozen=True)
class WithheldSegment:
    kind: str
    reason: str
    #: What the user would have to approve for this to flow. Never the content.
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Payload:
    """The immutable record of one disclosure to one engine."""

    text: str
    segments: tuple[Segment, ...]
    withheld: tuple[WithheldSegment, ...]
    label: Label
    engine: str
    thread_id: str | None
    profile_id: str | None
    content_hash: str
    #: Per-segment source hashes, so a changed source invalidates an approval.
    source_versions: dict[str, str] = field(default_factory=dict)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[... {len(text) - limit} more characters]"


def row_label(row: dict[str, Any]) -> Label:
    """The provenance of one stored chat row.

    Conservative by construction: a row that does not say where it came from is
    UNKNOWN, which is every row written before provenance was recorded.
    """

    if row.get("forgotten_memory"):
        # The user deleted the memory this turn was built from. Neo's own history
        # replaces it with a placeholder; it must not reach an external agent at all.
        return Label.unknown("forgotten_memory")
    provenance = str(row.get("provenance") or "")
    if not provenance:
        return Label.unknown("row_without_provenance")
    if provenance == "user_input":
        # A user message stored in chat was addressed to Neo at that earlier
        # time. Only the objective explicitly submitted for *this* external
        # turn is public to this engine; replaying old messages would disclose
        # material the user may have typed solely for Neo's internal model.
        stored = row.get("source_label")
        if isinstance(stored, Label):
            source = stored
        elif isinstance(stored, str):
            source = from_json(stored)
        else:
            source = Label.unknown("legacy_user_message")
        return join(Label.private("chat_private"), source)
    if provenance.startswith("external_agent:"):
        stored = row.get("source_label")
        if isinstance(stored, Label):
            source = stored
        elif isinstance(stored, str):
            source = from_json(stored)
        else:
            source = Label.unknown("legacy_external_answer")
        if not source.is_private():
            return Label.unknown("unverified_external_answer")
        tags = tuple(row.get("label_tags") or ())
        return join(source, Label.private(*tags)) if tags else source
    if provenance == "internal_model":
        stored = row.get("source_label")
        if isinstance(stored, Label):
            source = stored
        elif isinstance(stored, str):
            source = from_json(stored)
        else:
            source = Label.unknown("legacy_internal_answer")
        return join(Label.private("chat_private"), source)
    return Label.unknown(f"unrecognised_provenance:{provenance}")


def _lineage_segment(
    history: list[dict[str, Any]], *, engine: str, thread_id: str | None
) -> Labeled | None:
    """The tail of this engine's own earlier turns on this thread, as plain lines.

    Only untagged answers this engine produced in this exact thread are eligible.
    Earlier user messages need their own approval, even if they were typed into
    the same chat.
    """

    eligible: list[str] = []
    labels: list[Label] = []
    for row in history[-MAX_LINEAGE_MESSAGES * 3 :]:
        label = row_label(row)
        provenance = str(row.get("provenance") or "")
        own_turn = (
            thread_id is not None
            and row.get("role") == "assistant"
            and provenance == f"external_agent:{engine}"
            and row.get("thread_id") == thread_id
        )
        if not (label.is_public() or own_turn):
            continue
        if own_turn and label.tags != {f"lineage:{engine}:{thread_id}"}:
            # A chain answer, or one that carried granted material, cannot be
            # replayed merely because one participating engine wrote this row.
            continue
        content = _clip(str(row.get("content") or ""), MAX_MESSAGE_CHARS)
        if not content:
            continue
        speaker = {"user": "User", "assistant": "Assistant"}.get(str(row.get("role")))
        if not speaker:
            continue
        eligible.append(f"{speaker}: {content}")
        labels.append(public(f"user_input:{row.get('id', 'row')}") if label.is_public() else label)
    if not eligible:
        return None
    text = "Earlier in this conversation:\n" + "\n".join(eligible[-MAX_LINEAGE_MESSAGES:])
    # Lineage is public-by-construction here: every row that reached this point
    # was this engine's own untagged output in this thread.
    return Labeled(text, public(f"user_input:lineage:{engine}"))


def build_payload(
    *,
    objective: Labeled,
    history: list[dict[str, Any]] | None = None,
    engine: str,
    thread_id: str | None = None,
    profile_id: str | None = None,
    instructions: Labeled | None = None,
    change_summary: Labeled | None = None,
    granted: list[Labeled] | None = None,
    grants: GrantStore | None = None,
) -> Payload:
    """Assemble one engine's input, dropping anything it is not authorized to see."""

    destination = Destination(
        profile_id=profile_id or "", engine=engine, thread_id=thread_id
    )
    candidates: list[tuple[str, Labeled]] = []
    if instructions is not None:
        candidates.append(("instructions", instructions))
    lineage = _lineage_segment(history or [], engine=engine, thread_id=thread_id)
    if lineage is not None:
        candidates.append(("lineage", lineage))
    if change_summary is not None:
        candidates.append(("changes", change_summary))
    for item in granted or []:
        candidates.append(("granted", item))
    candidates.append(("objective", objective))

    kept: list[Segment] = []
    withheld: list[WithheldSegment] = []
    for kind, item in candidates:
        label = label_of(item)
        text = str(item.value if isinstance(item, Labeled) else item)
        decision = authorize("external_agent", item, destination, grants=grants)
        if not decision.allowed:
            withheld.append(
                WithheldSegment(kind=kind, reason=decision.reason, tags=tuple(sorted(label.tags)))
            )
            continue
        kept.append(
            Segment(
                kind=kind,
                text=text,
                label=label,
                source_version=content_hash(text),
            )
        )

    objective_segments = [s for s in kept if s.kind == "objective"]
    preamble_segments = [s for s in kept if s.kind != "objective"]
    preamble = _clip(
        "\n\n---\n\n".join(s.text for s in preamble_segments if s.text.strip()),
        MAX_PREAMBLE_CHARS,
    )
    task = "\n\n".join(f"Your task:\n{s.text.strip()}" for s in objective_segments)
    text = f"{preamble}\n\n---\n\n{task}" if preamble and task else (task or preamble)

    label = join(*[s.label for s in kept]) if kept else Label.unknown("empty_payload")
    return Payload(
        text=text,
        segments=tuple(kept),
        withheld=tuple(withheld),
        label=label,
        engine=engine,
        thread_id=thread_id,
        profile_id=profile_id,
        content_hash=content_hash(text),
        source_versions={s.kind: s.source_version for s in kept},
    )


__all__ = [
    "MAX_LINEAGE_MESSAGES",
    "Payload",
    "Segment",
    "WithheldSegment",
    "build_payload",
    "row_label",
]
