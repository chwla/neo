"""Structured Phase 3 surface adapters; all enabled writes end at one coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import JsonValue, ValidationError

from app.services.memory_v2.contracts import (
    ActorKind,
    ArchiveMemoryCommand,
    CandidateIntent,
    CandidateTargetHints,
    CreateMemoryCommand,
    DetachMemorySourceCommand,
    ErasePermanentlyMemoryCommand,
    EvidenceSpan,
    ForgetMemoryCommand,
    MemoryActor,
    MemoryCommand,
    MemorySource,
    MemoryUpdatePatch,
    MergeMemoryCommand,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    RestoreMemoryCommand,
    RestoreMode,
    Sensitivity,
    SourceChangeResult,
    SourceKind,
    SupersedeMemoryCommand,
    TargetRevision,
    UpdateMemoryCommand,
    ValidatedCandidateProposal,
)
from app.services.memory_v2.coordinator import (
    MemoryV2CoordinationResult,
    MemoryV2ExecutionContext,
    MemoryV2MutationCoordinator,
)
from app.services.memory_v2.idempotency import MemoryV2Idempotency
from app.services.memory_v2.taxonomy import Cardinality, MemoryType


class MemoryV2AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryV2AdapterContext:
    execution: MemoryV2ExecutionContext
    actor_kind: ActorKind
    actor_id: str
    source_kind: SourceKind
    source_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    observed_at: datetime | None = None
    evidence: tuple[EvidenceSpan, ...] = ()

    def actor(self) -> MemoryActor:
        return MemoryActor(kind=self.actor_kind, actor_id=self.actor_id)

    def source(self) -> MemorySource:
        return MemorySource(
            kind=self.source_kind,
            source_id=self.source_id,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            observed_at=self.observed_at,
            evidence=self.evidence,
        )


@dataclass(frozen=True)
class StructuredMemoryInput:
    memory_type: MemoryType
    domain_key: str
    slot_key: str
    cardinality: Cardinality
    canonical_value: JsonValue
    display_text: str
    sensitivity: Sensitivity = Sensitivity.NORMAL
    confidence: float = 1.0
    importance: int = 7
    explicit_user_request: bool = False
    subject_key: str = "user"
    proposal_id: UUID | None = None
    value_schema_version: int = 1
    evidence: tuple[EvidenceSpan, ...] = ()

    def candidate(
        self,
        *,
        stable_proposal_id: UUID | None = None,
        intent: CandidateIntent = CandidateIntent.ASSERT,
        targets: tuple[TargetRevision, ...] = (),
        predecessor_domain_key: str | None = None,
        predecessor_slot_key: str | None = None,
        explicit_domain_change: bool = False,
        explicit_slot_change: bool = False,
    ) -> ValidatedCandidateProposal:
        return ValidatedCandidateProposal(
            proposal_id=self.proposal_id or stable_proposal_id or uuid4(),
            intent=intent,
            subject_key=self.subject_key,
            memory_type=self.memory_type,
            domain_key=self.domain_key,
            slot_key=self.slot_key,
            cardinality=self.cardinality,
            canonical_value=self.canonical_value,
            display_text=self.display_text,
            sensitivity=self.sensitivity,
            confidence=self.confidence,
            importance=self.importance,
            value_schema_version=self.value_schema_version,
            explicit_user_request=self.explicit_user_request,
            target_hints=CandidateTargetHints(
                target_memory_ids=tuple(target.memory_id for target in targets),
                predecessor_domain_key=predecessor_domain_key,
                predecessor_slot_key=predecessor_slot_key,
                explicit_domain_change=explicit_domain_change,
                explicit_slot_change=explicit_slot_change,
            ),
            evidence=self.evidence,
        )


def _validated_candidate(
    item: StructuredMemoryInput,
    **kwargs: Any,
) -> ValidatedCandidateProposal:
    try:
        return item.candidate(**kwargs)
    except (TypeError, ValueError, ValidationError):
        raise MemoryV2AdapterError("structured_candidate_invalid") from None


class GenericMemoryV2Adapter:
    def __init__(self, coordinator: MemoryV2MutationCoordinator) -> None:
        self.coordinator = coordinator

    def execute(
        self,
        context: MemoryV2AdapterContext,
        command: MemoryCommand,
    ) -> MemoryV2CoordinationResult:
        return self.coordinator.execute(context.execution, command)

    def detach_source(
        self,
        context: MemoryV2AdapterContext,
        command: DetachMemorySourceCommand,
    ) -> SourceChangeResult:
        return self.coordinator.detach_source(context.execution, command)

    def create(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            CreateMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                candidate=_validated_candidate(
                    item, stable_proposal_id=uuid5(NAMESPACE_URL, f"neo-memory:{idempotency_key}")
                ),
            ),
        )

    def update(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        patch: MemoryUpdatePatch,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            UpdateMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                target=target,
                patch=patch,
            ),
        )

    def replace(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        targets: tuple[TargetRevision, ...],
        *,
        authority: ReplacementAuthority,
        idempotency_key: str,
        predecessor_domain_key: str | None = None,
        predecessor_slot_key: str | None = None,
        explicit_domain_change: bool = False,
        explicit_slot_change: bool = False,
    ) -> MemoryV2CoordinationResult:
        candidate = _validated_candidate(
            item,
            stable_proposal_id=uuid5(NAMESPACE_URL, f"neo-memory:{idempotency_key}"),
            intent=CandidateIntent.REPLACE,
            targets=targets,
            predecessor_domain_key=predecessor_domain_key,
            predecessor_slot_key=predecessor_slot_key,
            explicit_domain_change=explicit_domain_change,
            explicit_slot_change=explicit_slot_change,
        )
        return self.execute(
            context,
            ReplaceMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                candidate=candidate,
                authority=authority,
                targets=targets,
            ),
        )

    def merge(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        sources: tuple[TargetRevision, ...],
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            MergeMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                sources=sources,
                candidate=_validated_candidate(
                    item, stable_proposal_id=uuid5(NAMESPACE_URL, f"neo-memory:{idempotency_key}")
                ),
            ),
        )

    def archive(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            ArchiveMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                target=target,
            ),
        )

    def forget(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            ForgetMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                target=target,
            ),
        )

    def erase_permanently(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            ErasePermanentlyMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                target=target,
            ),
        )

    def restore(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        *,
        idempotency_key: str,
        mode: RestoreMode = RestoreMode.ARCHIVED_ONLY,
        replacement: StructuredMemoryInput | None = None,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            RestoreMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                target=target,
                mode=mode,
                replacement_candidate=(
                    _validated_candidate(
                        replacement,
                        stable_proposal_id=uuid5(
                            NAMESPACE_URL,
                            f"neo-memory:{idempotency_key}:replacement",
                        ),
                    )
                    if replacement
                    else None
                ),
            ),
        )

    def supersede(
        self,
        context: MemoryV2AdapterContext,
        predecessors: tuple[TargetRevision, ...],
        successor_memory_id: UUID,
        *,
        idempotency_key: str,
    ) -> MemoryV2CoordinationResult:
        return self.execute(
            context,
            SupersedeMemoryCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=idempotency_key,
                actor=context.actor(),
                source=context.source(),
                predecessors=predecessors,
                successor_memory_id=successor_memory_id,
            ),
        )


class TypedMemoryV2Adapter(GenericMemoryV2Adapter):
    """Typed surfaces supply an explicit taxonomy identity, never a typed-table write."""

    def create_typed(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        *,
        client_mutation_id: str,
    ) -> MemoryV2CoordinationResult:
        key = MemoryV2Idempotency.manual(context.execution.owner_id, client_mutation_id)
        return self.create(context, item, idempotency_key=key)


class CandidateReviewAction(StrEnum):
    ACCEPT = "accept"
    REFINE = "refine"
    REPLACE = "replace"
    CATEGORY_CORRECTION = "category_correction"
    MERGE = "merge"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CandidateReviewV2Result:
    action: CandidateReviewAction
    coordination: MemoryV2CoordinationResult | None
    outcome: str
    review_required: bool


class CandidateReviewV2Adapter(GenericMemoryV2Adapter):
    def apply(
        self,
        context: MemoryV2AdapterContext,
        *,
        candidate_id: str,
        candidate_revision: int,
        action: CandidateReviewAction,
        item: StructuredMemoryInput | None = None,
        targets: tuple[TargetRevision, ...] = (),
        patch: MemoryUpdatePatch | None = None,
        authority: ReplacementAuthority = ReplacementAuthority.REVIEWED,
        explicit_domain_change: bool = False,
        explicit_slot_change: bool = False,
    ) -> CandidateReviewV2Result:
        if action is CandidateReviewAction.REJECT:
            return CandidateReviewV2Result(action, None, "rejected", False)
        if action is CandidateReviewAction.AMBIGUOUS:
            return CandidateReviewV2Result(action, None, "needs_review", True)
        key = MemoryV2Idempotency.review(
            context.execution.owner_id,
            candidate_id,
            candidate_revision,
            action.value,
        )
        if action is CandidateReviewAction.ACCEPT and item is not None:
            result = self.create(context, item, idempotency_key=key)
        elif action is CandidateReviewAction.REFINE and len(targets) == 1 and patch is not None:
            result = self.update(context, targets[0], patch, idempotency_key=key)
        elif (
            action
            in {
                CandidateReviewAction.REPLACE,
                CandidateReviewAction.CATEGORY_CORRECTION,
            }
            and item is not None
        ):
            result = self.replace(
                context,
                item,
                targets,
                authority=authority,
                idempotency_key=key,
                explicit_domain_change=explicit_domain_change,
                explicit_slot_change=explicit_slot_change,
            )
        elif action is CandidateReviewAction.MERGE and item is not None:
            result = self.merge(context, item, targets, idempotency_key=key)
        else:
            raise MemoryV2AdapterError("review_action_inputs_invalid")
        outcome = result.compatibility.outcome if result.compatibility else result.mode.value
        return CandidateReviewV2Result(action, result, outcome, False)


class ChatMemoryV2Adapter(GenericMemoryV2Adapter):
    def apply_structured_candidate(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        *,
        extraction_version: str,
        candidate_key: str,
        transport: str,
    ) -> MemoryV2CoordinationResult:
        if transport not in {"sync", "stream"}:
            raise MemoryV2AdapterError("unsupported_chat_transport")
        if not context.message_id:
            raise MemoryV2AdapterError("chat_message_id_required")
        key = MemoryV2Idempotency.chat(
            context.execution.owner_id,
            context.message_id,
            extraction_version,
            candidate_key,
        )
        return self.create(context, item, idempotency_key=key)

    def apply_structured_replacement(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        targets: tuple[TargetRevision, ...],
        *,
        extraction_version: str,
        candidate_key: str,
        transport: str,
        authority: ReplacementAuthority = ReplacementAuthority.EXPLICIT_CORRECTION,
    ) -> MemoryV2CoordinationResult:
        if transport not in {"sync", "stream"}:
            raise MemoryV2AdapterError("unsupported_chat_transport")
        if not context.message_id:
            raise MemoryV2AdapterError("chat_message_id_required")
        key = MemoryV2Idempotency.chat(
            context.execution.owner_id,
            context.message_id,
            extraction_version,
            candidate_key,
        )
        return self.replace(
            context,
            item,
            targets,
            authority=authority,
            idempotency_key=key,
        )


class ImportMemoryV2Adapter(GenericMemoryV2Adapter):
    def accept(
        self,
        context: MemoryV2AdapterContext,
        imported: dict[str, Any],
        *,
        batch_id: str,
        item_hash: str,
    ) -> MemoryV2CoordinationResult:
        required = {
            "memory_type",
            "domain_key",
            "slot_key",
            "cardinality",
            "canonical_value",
            "display_text",
        }
        if not required <= imported.keys():
            raise MemoryV2AdapterError("imported_structured_memory_incomplete")
        item = StructuredMemoryInput(
            memory_type=MemoryType(str(imported["memory_type"])),
            domain_key=str(imported["domain_key"]),
            slot_key=str(imported["slot_key"]),
            cardinality=Cardinality(str(imported["cardinality"])),
            canonical_value=imported["canonical_value"],
            display_text=str(imported["display_text"]),
            sensitivity=Sensitivity(str(imported.get("sensitivity", "normal"))),
            explicit_user_request=bool(imported.get("explicit_user_request", False)),
        )
        key = MemoryV2Idempotency.imported(context.execution.owner_id, batch_id, item_hash)
        return self.create(context, item, idempotency_key=key)


class MaintenanceMemoryV2Adapter(GenericMemoryV2Adapter):
    def archive_proposal(
        self,
        context: MemoryV2AdapterContext,
        target: TargetRevision,
        *,
        run_id: str,
        proposal_hash: str,
    ) -> MemoryV2CoordinationResult:
        key = MemoryV2Idempotency.maintenance(
            context.execution.owner_id,
            run_id,
            proposal_hash,
        )
        return self.archive(context, target, idempotency_key=key)


class AgentMemoryV2Adapter(GenericMemoryV2Adapter):
    def create_from_tool(
        self,
        context: MemoryV2AdapterContext,
        item: StructuredMemoryInput,
        *,
        tool_call_id: str,
    ) -> MemoryV2CoordinationResult:
        if context.actor_kind is not ActorKind.AGENT:
            raise MemoryV2AdapterError("agent_actor_required")
        key = MemoryV2Idempotency.agent(context.execution.owner_id, tool_call_id)
        return self.create(context, item, idempotency_key=key)


def structured_item_hash(item: StructuredMemoryInput) -> str:
    material = {
        "memory_type": item.memory_type.value,
        "domain_key": item.domain_key,
        "slot_key": item.slot_key,
        "cardinality": item.cardinality.value,
        "canonical_value": item.canonical_value,
        "display_text": item.display_text,
        "sensitivity": item.sensitivity.value,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
