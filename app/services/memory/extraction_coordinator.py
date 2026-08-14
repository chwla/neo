"""Single Phase 4 extraction orchestrator; all writes delegate to Phase 3."""

from __future__ import annotations

import hashlib
import logging
import json
from dataclasses import replace
from time import monotonic
from uuid import UUID

from app.services.memory.adapters import (
    ChatMemoryAdapter,
    MemoryAdapterContext,
    StructuredMemoryInput,
)
from app.services.memory.contracts import (
    CandidateLifecycleState,
    CandidatePersistenceOutcome,
    CanonicalMemorySnapshot,
    EvidenceRole,
    EvidenceSpan,
    MemoryLifecycleState,
    MemoryOutcome,
    MemoryRejectionCode,
    PersistExtractionCandidateCommand,
    Sensitivity,
    SourceKind,
    TargetRevision,
)
from app.services.memory.correction_resolver import (
    CorrectionResolution,
    CorrectionResolutionKind,
    DeterministicCorrectionResolver,
    build_candidate,
)
from app.services.memory.extraction import (
    ExtractionModel,
    ExtractionModelError,
    ExtractionModelTimeout,
)
from app.services.memory.extraction_contracts import (
    CandidateAction,
    CurrentTurnOverride,
    ExtractionCandidateDecision,
    ExtractionDiagnostic,
    ExtractionMode,
    ExtractionRequest,
    ExtractionResult,
    ExtractionStatus,
    GroundingDecision,
    LifecycleHint,
    ModelExtractionInput,
    ModelProposalSummary,
    NormalizedExtractionCandidate,
    PreparseKind,
    PreparseResult,
)
from app.services.memory.extraction_diagnostics import (
    ExtractionDiagnosticSink,
    InMemoryExtractionDiagnostics,
)
from app.services.memory.grounding import ground_assertion, ground_retraction
from app.services.memory.idempotency import MemoryIdempotency
from app.services.memory.model_schema import (
    ModelOutputError,
    ModelProposalResponse,
    ModelRetractionProposal,
    parse_model_output,
)
from app.services.memory.policy import (
    ExtractionTrigger,
    classify_sensitivity,
    extraction_timing_policy,
)
from app.services.memory.preparser import deterministic_model_response, preparse

_SEMANTIC_LOG = logging.getLogger("neo.memory.extraction_coordinator")

EXTRACTOR_VERSION = "memory-extractor-v1"
REDACTED_SENSITIVE_ASSERTION = "[sensitive memory]"


class CurrentTurnOverrideBuilder:
    """Derive prompt suppression authority from final typed outcomes, never target discovery."""

    def __init__(self, *, owner_id: str, source_message_id: str) -> None:
        self.owner_id = owner_id
        self.source_message_id = source_message_id
        self._suppressed_ids: list[UUID] = []
        self._suppressed_slots: list[str] = []
        self._candidate_target_ids: list[UUID] = []
        self._unresolved_slots: list[str] = []
        self._unresolved_hints: list[str] = []

    @staticmethod
    def _unique(items):
        return tuple(dict.fromkeys(items))

    def record_candidate_targets(
        self,
        targets: tuple[CanonicalMemorySnapshot, ...],
        *,
        unresolved: bool = False,
    ) -> None:
        self._candidate_target_ids.extend(item.memory_id for item in targets)
        if unresolved:
            self._unresolved_slots.extend(item.slot_key for item in targets)

    def record_unresolved_hint(self, hint: str) -> None:
        self._unresolved_hints.append(hint)

    def record_final_outcome(
        self,
        resolution: CorrectionResolutionKind,
        targets: tuple[CanonicalMemorySnapshot, ...],
        decision: ExtractionCandidateDecision,
    ) -> None:
        self.record_candidate_targets(targets, unresolved=decision.review_required)
        replacement_applied = (
            resolution is CorrectionResolutionKind.REPLACE
            and decision.action is CandidateAction.REPLACE
            and decision.outcome == MemoryOutcome.REPLACED.value
            and not decision.review_required
        )
        retraction_applied = (
            resolution is CorrectionResolutionKind.RETRACT
            and decision.action in {CandidateAction.RETRACT, CandidateAction.FORGET}
            and decision.outcome in {"archived", "forgotten", "erased_permanently"}
            and not decision.review_required
        )
        if replacement_applied or retraction_applied:
            self._suppressed_ids.extend(item.memory_id for item in targets)
            self._suppressed_slots.extend(item.slot_key for item in targets)

    def build(
        self,
        *,
        status: ExtractionStatus,
        sensitivity: Sensitivity,
        positive_current_assertion,
        confidence: float,
    ) -> CurrentTurnOverride:
        publish_semantics = status in {ExtractionStatus.APPLIED, ExtractionStatus.NEEDS_REVIEW}
        if sensitivity is Sensitivity.PROHIBITED or not publish_semantics:
            return CurrentTurnOverride(
                owner_id=self.owner_id,
                source_message_id=self.source_message_id,
                sensitivity=sensitivity,
            )
        review_required = status is ExtractionStatus.NEEDS_REVIEW
        suppressed_ids = () if review_required else self._unique(self._suppressed_ids)
        suppressed_slots = () if review_required else self._unique(self._suppressed_slots)
        positive = positive_current_assertion if sensitivity is Sensitivity.NORMAL else None
        redacted = (
            REDACTED_SENSITIVE_ASSERTION
            if sensitivity is Sensitivity.SENSITIVE and positive_current_assertion is not None
            else None
        )
        unresolved_hints = (
            self._unique(self._unresolved_hints) if sensitivity is Sensitivity.NORMAL else ()
        )
        return CurrentTurnOverride(
            owner_id=self.owner_id,
            source_message_id=self.source_message_id,
            suppressed_memory_ids=suppressed_ids,
            suppressed_slot_keys=suppressed_slots,
            candidate_target_memory_ids=self._unique(self._candidate_target_ids),
            unresolved_conflict_slot_keys=self._unique(self._unresolved_slots),
            positive_current_assertion=positive,
            redacted_current_assertion=redacted,
            sensitivity=sensitivity,
            review_required=review_required,
            contradicted_memory_ids=suppressed_ids,
            contradicted_slot_keys=suppressed_slots,
            unresolved_target_hints=unresolved_hints,
            confidence=confidence,
            contradiction_deterministic=bool(suppressed_ids),
        )


class MemoryExtractionCoordinator:
    def __init__(
        self,
        adapter: ChatMemoryAdapter,
        *,
        model: ExtractionModel | None = None,
        resolver: DeterministicCorrectionResolver | None = None,
        diagnostics: ExtractionDiagnosticSink | None = None,
        duplicate_finder=None,
        duplicate_threshold: float = 0.93,
    ) -> None:
        self.adapter = adapter
        self.model = model
        self.resolver = resolver or DeterministicCorrectionResolver()
        self.diagnostics = diagnostics or InMemoryExtractionDiagnostics()
        self.duplicate_finder = duplicate_finder
        self.duplicate_threshold = duplicate_threshold

    def _semantic_duplicate(
        self,
        candidate: NormalizedExtractionCandidate,
        records: tuple[CanonicalMemorySnapshot, ...],
    ) -> CanonicalMemorySnapshot | None:
        """Find an active record already meaning what this candidate says.

        Only reached once the deterministic resolver has decided this is a new
        fact, so this is the last check before a second copy of one memory is
        written.  Comparison is restricted to records the candidate could
        legitimately be a restatement of — same subject, type, domain and scope
        — so a semantic near-miss can never reach across categories.
        """

        if self.duplicate_finder is None:
            return None
        proposal = candidate.proposal
        comparable = {
            item.memory_id: item
            for item in records
            if item.subject_key == proposal.subject_key
            and item.memory_type is proposal.memory_type
            and item.domain_key == proposal.domain_key
            and item.scope_type == proposal.scope_type
            and item.scope_project_id == proposal.scope_project_id
        }
        _SEMANTIC_LOG.warning(
            "semantic_duplicate_entry comparable=%d finder=%s type=%s domain=%s",
            len(comparable),
            self.duplicate_finder is not None,
            proposal.memory_type.value,
            proposal.domain_key,
        )
        if not comparable:
            return None
        try:
            match = self.duplicate_finder(
                proposal.display_text,
                frozenset(comparable),
                threshold=self.duplicate_threshold,
            )
        except Exception:
            # Not noticing a duplicate leaves the store exactly as it was before
            # this check existed; letting the failure escape would lose a memory
            # the user asked to keep.
            return None
        return comparable.get(match) if match is not None else None

    def process(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
        *,
        transport: str = "sync",
    ) -> ExtractionResult:
        if transport not in {"sync", "stream"}:
            raise ValueError("unsupported_chat_transport")
        gated = self._gate(request, context)
        if gated is not None:
            return gated
        source_sensitivity = classify_sensitivity(request.user_message)
        if source_sensitivity is Sensitivity.PROHIBITED:
            prohibited = PreparseResult(
                kind=PreparseKind.IGNORE,
                reason="prohibited_source_rejected_before_model",
                sensitive_content_redacted=True,
            )
            return self._finish(
                request,
                prohibited,
                timing="not_run",
                status=ExtractionStatus.REJECTED,
                model_summary=self._empty_model_summary(),
                reason_codes=("prohibited_source_rejected_before_model",),
                sensitivity=Sensitivity.PROHIBITED,
            )
        parsed = preparse(request)
        timing = self._timing(request, parsed)

        if parsed.kind is PreparseKind.IGNORE:
            return self._finish(
                request,
                parsed,
                timing=timing,
                status=ExtractionStatus.NO_ACTION,
                model_summary=self._empty_model_summary(),
                reason_codes=(parsed.reason,),
                sensitivity=source_sensitivity,
            )

        response, model_summary, model_error, model_meta = self._proposals(request, parsed)
        if source_sensitivity is Sensitivity.SENSITIVE:
            model_summary = model_summary.model_copy(update={"raw_output_hash": None})
        if model_error is not None or response is None:
            review = self._persist_grounded_preparse_review(
                request,
                context,
                parsed,
                timing=timing,
                model_summary=model_summary,
                model_error=model_error or "proposal_generation_failed",
                model_meta=model_meta,
                sensitivity=source_sensitivity,
            )
            if review is not None:
                return review
            status = (
                ExtractionStatus.NEEDS_REVIEW
                if parsed.kind is PreparseKind.AMBIGUOUS
                else ExtractionStatus.FAILED
            )
            return self._finish(
                request,
                parsed,
                timing=timing,
                status=status,
                model_summary=model_summary,
                reason_codes=(model_error or "proposal_generation_failed",),
                model_meta=model_meta,
                sensitivity=source_sensitivity,
            )

        response, capped_count = self._apply_candidate_cap(request, response)
        if capped_count:
            model_summary = model_summary.model_copy(update={"capped_count": capped_count})
        response, nested_count = self._drop_nested_assertions(response)
        if nested_count:
            reason_codes_nested = ("nested_assertion_dropped",)
        else:
            reason_codes_nested = ()

        grounding: list[GroundingDecision] = []
        grounded_retractions: list[ModelRetractionProposal] = []
        retraction_grounding: dict[str, GroundingDecision] = {}
        for item in response.retractions:
            decision = ground_retraction(request, item)
            grounding.append(decision)
            retraction_grounding[item.proposal_id] = decision
            if decision.accepted:
                grounded_retractions.append(item)

        assertion_grounding: dict[str, GroundingDecision] = {}
        for item in response.assertions:
            decision = ground_assertion(request, item)
            grounding.append(decision)
            assertion_grounding[item.proposal_id] = decision

        built: list[NormalizedExtractionCandidate] = []
        decisions: list[ExtractionCandidateDecision] = []
        reason_codes: list[str] = list(reason_codes_nested)
        for item in response.assertions:
            candidate = build_candidate(
                request,
                item,
                assertion_grounding[item.proposal_id],
                tuple(grounded_retractions),
            )
            if candidate.candidate is None:
                review = candidate.review_required
                decisions.append(
                    ExtractionCandidateDecision(
                        action=CandidateAction.REVIEW if review else CandidateAction.REJECT,
                        reason=candidate.reason,
                        review_required=review,
                    )
                )
                reason_codes.append(candidate.reason)
            else:
                built.append(candidate.candidate)

        effective_sensitivity = source_sensitivity
        if any(item.proposal.sensitivity is Sensitivity.SENSITIVE for item in built):
            effective_sensitivity = Sensitivity.SENSITIVE
            model_summary = model_summary.model_copy(update={"raw_output_hash": None})

        records = ()
        if built or grounded_retractions:
            records = self.adapter.list_active_memories(
                context,
                include_archived=parsed.lifecycle_hint is LifecycleHint.RESTORE,
            )
        active_records = tuple(
            item for item in records if item.status is MemoryLifecycleState.ACTIVE
        )
        override_builder = CurrentTurnOverrideBuilder(
            owner_id=request.owner_id,
            source_message_id=request.message_id,
        )

        for candidate in built:
            status = self.adapter.candidate_status(context, candidate.proposal.proposal_id)
            if status is not None and status.state is CandidateLifecycleState.APPLIED:
                decisions.append(
                    ExtractionCandidateDecision(
                        candidate_id=status.candidate_id,
                        action=CandidateAction.IDEMPOTENT_REPLAY,
                        reason="candidate_already_applied",
                        operation_id=status.applied_operation_id,
                        outcome=(
                            status.decision_outcome.value if status.decision_outcome else None
                        ),
                    )
                )
                continue
            if status is not None and status.state is CandidateLifecycleState.NEEDS_REVIEW:
                decisions.append(
                    ExtractionCandidateDecision(
                        candidate_id=status.candidate_id,
                        action=CandidateAction.REVIEW,
                        reason="candidate_already_requires_review",
                        review_required=True,
                    )
                )
                continue

            resolution = self.resolver.resolve(candidate, active_records)
            if resolution.kind is CorrectionResolutionKind.CREATE:
                restated = self._semantic_duplicate(candidate, active_records)
                if restated is not None:
                    resolution = CorrectionResolution(
                        CorrectionResolutionKind.RECONFIRM,
                        "semantic_active_duplicate",
                        (restated,),
                    )
            if resolution.kind is CorrectionResolutionKind.RECONFIRM and resolution.targets:
                # Recognising a restatement is not enough on its own.  The write
                # kernel matches on the canonical fingerprint, and the slot key is
                # part of that fingerprint, so a candidate the resolver matched to
                # a record in a different slot still hashed differently and was
                # written as a second copy.  The model naming another dimension
                # for one preference does not make it another preference: adopt
                # the slot of the record this restates.
                target = resolution.targets[0]
                if target.slot_key != candidate.proposal.slot_key:
                    proposal = candidate.proposal.model_copy(
                        update={
                            "slot_key": target.slot_key,
                            "cardinality": target.cardinality,
                        }
                    )
                    candidate = candidate.model_copy(update={"proposal": proposal})
            targets = resolution.targets
            if not targets:
                for hint in candidate.old_value_hints:
                    override_builder.record_unresolved_hint(hint)

            review_required = (
                resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
                or parsed.kind is PreparseKind.AMBIGUOUS
                or candidate.proposal.confidence < 0.85
                or request.mode is ExtractionMode.SUGGESTION_ONLY
                or candidate.expires_at is not None
            )
            if review_required:
                review_reason = (
                    "typed_expiry_requires_review"
                    if candidate.expires_at is not None
                    else (
                        "preparse_ambiguity_requires_review"
                        if parsed.kind is PreparseKind.AMBIGUOUS
                        and resolution.kind is not CorrectionResolutionKind.NEEDS_REVIEW
                        else resolution.reason
                    )
                )
                persisted = self._persist_candidate(
                    request,
                    context,
                    candidate,
                    tuple(grounded_retractions),
                    retraction_grounding,
                    model_summary,
                    needs_review=True,
                    reason=review_reason,
                )
                decisions.append(
                    ExtractionCandidateDecision(
                        candidate_id=persisted,
                        action=CandidateAction.REVIEW,
                        reason=review_reason,
                        review_required=True,
                        memory_ids=tuple(item.memory_id for item in targets),
                    )
                )
                override_builder.record_candidate_targets(targets, unresolved=True)
                reason_codes.append(review_reason)
                continue

            if resolution.kind is CorrectionResolutionKind.REPLACE:
                target_hints = candidate.proposal.target_hints.model_copy(
                    update={
                        "target_memory_ids": tuple(item.memory_id for item in targets),
                    }
                )
                proposal = candidate.proposal.model_copy(update={"target_hints": target_hints})
                candidate = candidate.model_copy(update={"proposal": proposal})

            persisted = self._persist_candidate(
                request,
                context,
                candidate,
                tuple(grounded_retractions),
                retraction_grounding,
                model_summary,
                needs_review=False,
                reason=resolution.reason,
            )
            if persisted is None:
                decisions.append(
                    ExtractionCandidateDecision(
                        candidate_id=candidate.proposal.proposal_id,
                        action=CandidateAction.REJECT,
                        reason="candidate_persistence_rejected",
                    )
                )
                reason_codes.append("candidate_persistence_rejected")
                continue
            applied = self._apply_candidate(
                request,
                context,
                candidate,
                resolution.kind,
                targets,
                transport=transport,
            )
            decisions.append(applied)
            override_builder.record_final_outcome(resolution.kind, targets, applied)

        linked_groups = {
            item.correction_group for item in response.assertions if item.correction_group
        }
        pure_retractions = tuple(
            item
            for item in grounded_retractions
            if not item.correction_group or item.correction_group not in linked_groups
        )
        for retraction in pure_retractions:
            eligible = records
            if parsed.lifecycle_hint is not LifecycleHint.RESTORE:
                eligible = active_records
            resolution = self.resolver.resolve_retraction(retraction, eligible)
            if resolution.kind is not CorrectionResolutionKind.RETRACT:
                decisions.append(
                    ExtractionCandidateDecision(
                        action=CandidateAction.REVIEW,
                        reason=resolution.reason,
                        review_required=True,
                        memory_ids=tuple(item.memory_id for item in resolution.targets),
                    )
                )
                override_builder.record_candidate_targets(
                    resolution.targets,
                    unresolved=True,
                )
                override_builder.record_unresolved_hint(retraction.old_value_hint)
                reason_codes.append(resolution.reason)
                continue
            # Every target here holds the same value, so all of them are the fact
            # the user asked to remove.  Retracting only the first left the
            # duplicates active and the value returned on the next recall.
            for target in resolution.targets:
                applied = self._apply_retraction(
                    request,
                    context,
                    retraction,
                    target.memory_id,
                    target.revision,
                    parsed.lifecycle_hint,
                )
                decisions.append(applied)
                override_builder.record_final_outcome(
                    CorrectionResolutionKind.RETRACT,
                    (target,),
                    applied,
                )

        positive = next(
            (
                item.typed_value
                for item in response.assertions
                if assertion_grounding[item.proposal_id].accepted
            ),
            None,
        )
        confidence = max(
            (item.confidence for item in response.assertions),
            default=max((item.confidence for item in response.retractions), default=0.0),
        )
        if any(item.review_required for item in decisions):
            final_status = ExtractionStatus.NEEDS_REVIEW
        elif any(
            item.action
            in {
                CandidateAction.CREATE,
                CandidateAction.RECONFIRM,
                CandidateAction.REPLACE,
                CandidateAction.RETRACT,
                CandidateAction.FORGET,
                CandidateAction.IDEMPOTENT_REPLAY,
            }
            for item in decisions
        ):
            final_status = ExtractionStatus.APPLIED
        elif decisions:
            final_status = ExtractionStatus.REJECTED
        else:
            final_status = ExtractionStatus.NO_ACTION
        if capped_count:
            reason_codes.append("automatic_candidate_cap_applied")
        override = override_builder.build(
            status=final_status,
            sensitivity=effective_sensitivity,
            positive_current_assertion=positive,
            confidence=confidence,
        )
        return self._finish(
            request,
            parsed,
            timing=timing,
            status=final_status,
            model_summary=model_summary,
            grounding=tuple(grounding),
            decisions=tuple(decisions),
            override=override,
            reason_codes=tuple(reason_codes),
            model_meta=model_meta,
            sensitivity=effective_sensitivity,
        )

    def _gate(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
    ) -> ExtractionResult | None:
        flags = self.adapter.coordinator.flags
        placeholder = PreparseResult(kind=PreparseKind.IGNORE, reason="extraction_not_started")
        reason = None
        if request.owner_id != context.execution.validated_owner():
            reason = "request_owner_context_mismatch"
        elif request.message_id != context.message_id:
            reason = "request_message_context_mismatch"
        elif request.conversation_id != context.conversation_id:
            reason = "request_conversation_context_mismatch"
        elif request.session_id != context.session_id:
            reason = "request_session_context_mismatch"
        elif request.incognito or context.execution.is_incognito:
            reason = "incognito_extraction_disabled"
        elif not request.memory_enabled or not context.execution.memory_enabled:
            reason = "memory_disabled_extraction"
        elif not flags.extraction_enabled:
            reason = "memory_extraction_disabled"
        elif (
            request.mode is ExtractionMode.FOREGROUND_DETERMINISTIC
            and not flags.foreground_extraction_enabled
        ):
            reason = "foreground_extraction_disabled"
        elif (
            request.mode is ExtractionMode.POST_TURN_AUTOMATIC
            and not flags.post_turn_extraction_enabled
        ):
            reason = "post_turn_extraction_disabled"
        elif len(request.user_message) > flags.extraction_max_input_chars:
            reason = "extraction_input_too_large"
        if reason is None:
            return None
        status = (
            ExtractionStatus.REJECTED if reason.endswith("mismatch") else ExtractionStatus.DISABLED
        )
        return self._finish(
            request,
            placeholder,
            timing="not_run",
            status=status,
            model_summary=self._empty_model_summary(),
            reason_codes=(reason,),
        )

    def _proposals(
        self,
        request: ExtractionRequest,
        parsed: PreparseResult,
    ) -> tuple[
        ModelProposalResponse | None,
        ModelProposalSummary,
        str | None,
        dict[str, object],
    ]:
        deterministic_kinds = {
            PreparseKind.DETERMINISTIC_ASSERTION,
            PreparseKind.DETERMINISTIC_CORRECTION,
            PreparseKind.EXPLICIT_LIFECYCLE,
            PreparseKind.EXPLICIT_BATCH,
        }
        if parsed.kind in deterministic_kinds and parsed.deterministic:
            response = deterministic_model_response(parsed)
            raw_hash = hashlib.sha256(
                json.dumps(response.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest()
            return (
                response,
                ModelProposalSummary(
                    called=False,
                    assertion_count=len(response.assertions),
                    retraction_count=len(response.retractions),
                    exclusion_count=len(response.exclusions),
                    raw_output_hash=raw_hash,
                ),
                None,
                {},
            )
        if request.mode is ExtractionMode.FOREGROUND_DETERMINISTIC:
            return None, self._empty_model_summary(), "foreground_structure_not_deterministic", {}
        if self.model is None:
            return None, self._empty_model_summary(), "extraction_model_not_configured", {}

        visible = ModelExtractionInput.from_trusted_request(request)
        started = monotonic()
        last_error = "model_output_invalid"
        response_meta: dict[str, object] = {}
        for attempt in range(2):
            try:
                model_response = self.model.extract(visible)
                response_meta = {
                    "model_version": model_response.model_version,
                    "prompt_version": model_response.prompt_version,
                    "provider_kind": model_response.metadata.provider_kind,
                    "http_status": model_response.metadata.http_status,
                    "response_envelope_shape": (model_response.metadata.response_envelope_shape),
                    "content_present": model_response.metadata.content_present,
                    "content_byte_length": model_response.metadata.content_byte_length,
                    "response_content_hash": (model_response.metadata.response_content_hash),
                    "sanitized_provider_error_code": (
                        model_response.metadata.sanitized_failure_code
                    ),
                    "provider_timeout_stage": model_response.metadata.timeout_stage,
                }
                response = parse_model_output(model_response.raw_output)
                raw_hash = model_response.metadata.response_content_hash
                if raw_hash is None:
                    if isinstance(model_response.raw_output, bytes):
                        encoded = model_response.raw_output
                    elif isinstance(model_response.raw_output, str):
                        encoded = model_response.raw_output.encode("utf-8")
                    else:
                        encoded = json.dumps(
                            model_response.raw_output,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    raw_hash = hashlib.sha256(encoded).hexdigest()
                latency = int((monotonic() - started) * 1000)
                response_meta["model_latency_ms"] = latency
                response_meta["json_parse_result"] = "parsed"
                response_meta["schema_validation_result"] = "valid"
                response_meta["schema_error_codes"] = ()
                return (
                    response,
                    ModelProposalSummary(
                        called=True,
                        assertion_count=len(response.assertions),
                        retraction_count=len(response.retractions),
                        exclusion_count=len(response.exclusions),
                        raw_output_hash=raw_hash,
                    ),
                    None,
                    response_meta,
                )
            except ExtractionModelTimeout as exc:
                if exc.metadata is not None:
                    response_meta.update(
                        {
                            "provider_kind": exc.metadata.provider_kind,
                            "http_status": exc.metadata.http_status,
                            "response_envelope_shape": exc.metadata.response_envelope_shape,
                            "content_present": exc.metadata.content_present,
                            "content_byte_length": exc.metadata.content_byte_length,
                            "response_content_hash": exc.metadata.response_content_hash,
                            "sanitized_provider_error_code": (exc.metadata.sanitized_failure_code),
                            "provider_timeout_stage": exc.metadata.timeout_stage,
                        }
                    )
                response_meta["json_parse_result"] = "not_attempted"
                response_meta["schema_validation_result"] = "not_attempted"
                response_meta["model_latency_ms"] = int((monotonic() - started) * 1000)
                return (
                    None,
                    self._empty_model_summary(
                        called=True,
                        raw_output_hash=response_meta.get("response_content_hash"),
                    ),
                    exc.code,
                    response_meta,
                )
            except ExtractionModelError as exc:
                if exc.metadata is not None:
                    response_meta.update(
                        {
                            "provider_kind": exc.metadata.provider_kind,
                            "http_status": exc.metadata.http_status,
                            "response_envelope_shape": exc.metadata.response_envelope_shape,
                            "content_present": exc.metadata.content_present,
                            "content_byte_length": exc.metadata.content_byte_length,
                            "response_content_hash": exc.metadata.response_content_hash,
                            "sanitized_provider_error_code": (exc.metadata.sanitized_failure_code),
                            "provider_timeout_stage": exc.metadata.timeout_stage,
                        }
                    )
                response_meta["json_parse_result"] = "not_attempted"
                response_meta["schema_validation_result"] = "not_attempted"
                response_meta["model_latency_ms"] = int((monotonic() - started) * 1000)
                return (
                    None,
                    self._empty_model_summary(
                        called=True,
                        raw_output_hash=response_meta.get("response_content_hash"),
                    ),
                    exc.code,
                    response_meta,
                )
            except ModelOutputError as exc:
                last_error = exc.code
                response_meta["json_parse_result"] = (
                    "parsed" if exc.code == "invalid_model_schema" else "invalid"
                )
                response_meta["schema_validation_result"] = (
                    "invalid" if exc.code == "invalid_model_schema" else "not_attempted"
                )
                response_meta["schema_error_codes"] = exc.schema_error_codes
                if attempt == 0:
                    continue
        response_meta["model_latency_ms"] = int((monotonic() - started) * 1000)
        return (
            None,
            self._empty_model_summary(
                called=True,
                raw_output_hash=response_meta.get("response_content_hash"),
            ),
            last_error,
            response_meta,
        )

    def _persist_grounded_preparse_review(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
        parsed: PreparseResult,
        *,
        timing: str,
        model_summary: ModelProposalSummary,
        model_error: str,
        model_meta: dict[str, object],
        sensitivity: Sensitivity,
    ) -> ExtractionResult | None:
        if parsed.kind is not PreparseKind.AMBIGUOUS or not parsed.assertions:
            return None
        fallback = deterministic_model_response(parsed)
        grounding: list[GroundingDecision] = []
        decisions: list[ExtractionCandidateDecision] = []
        positive = None
        override_builder = CurrentTurnOverrideBuilder(
            owner_id=request.owner_id,
            source_message_id=request.message_id,
        )
        for source_hint, proposal in zip(parsed.assertions, fallback.assertions, strict=True):
            proposal = proposal.model_copy(update={"confidence": 0.5})
            grounded = ground_assertion(request, proposal)
            grounding.append(grounded)
            built = build_candidate(request, proposal, grounded, ())
            if built.candidate is None:
                continue
            category_unresolved = parsed.reason == "category_reference_unresolved"
            domain_unresolved = source_hint.domain_hint is None or category_unresolved
            slot_unresolved = source_hint.slot_hint is None or category_unresolved
            reason = f"grounded_preparse_review_after_{model_error}"
            persisted = self._persist_candidate(
                request,
                context,
                built.candidate,
                (),
                {},
                model_summary,
                needs_review=True,
                reason=reason,
                additional_grounding_evidence={
                    "model_failure_reason": model_error,
                    "preparse_review_fallback": True,
                    "domain_unresolved": domain_unresolved,
                    "slot_unresolved": slot_unresolved,
                },
            )
            if persisted is None:
                continue
            positive = proposal.typed_value
            override_builder.record_unresolved_hint(source_hint.normalized_value)
            decisions.append(
                ExtractionCandidateDecision(
                    candidate_id=persisted,
                    action=CandidateAction.REVIEW,
                    reason=reason,
                    review_required=True,
                    proposed_memory_type=proposal.memory_type_hint.value,
                    proposed_domain_hint=(None if domain_unresolved else source_hint.domain_hint),
                    proposed_slot_hint=(None if slot_unresolved else source_hint.slot_hint),
                    domain_unresolved=domain_unresolved,
                    slot_unresolved=slot_unresolved,
                    model_failure_reason=model_error,
                )
            )
        if not decisions:
            return None
        override = override_builder.build(
            status=ExtractionStatus.NEEDS_REVIEW,
            sensitivity=sensitivity,
            positive_current_assertion=positive,
            confidence=0.5,
        )
        return self._finish(
            request,
            parsed,
            timing=timing,
            status=ExtractionStatus.NEEDS_REVIEW,
            model_summary=model_summary,
            grounding=tuple(grounding),
            decisions=tuple(decisions),
            override=override,
            reason_codes=(model_error, "grounded_preparse_review_persisted"),
            model_meta=model_meta,
            sensitivity=sensitivity,
        )

    @staticmethod
    def _drop_nested_assertions(
        response: ModelProposalResponse,
    ) -> tuple[ModelProposalResponse, int]:
        """Drop an assertion whose evidence sits wholly inside another's.

        Asked to record "simple 25-minute practice sessions with perspective
        steps, line-control drills, shading notes, and progress tracking", the
        model proposes both the whole preference and "perspective steps" cut out
        of the middle of it.  The fragment is not a second preference the user
        holds, it is part of the first, and storing it produced a spurious extra
        memory and an exclusive-slot conflict that landed in review.

        Only same-type neighbours are compared, so a goal quoted inside a wide
        preference span survives: a fact of a different kind is never merely a
        piece of the one around it.  Assertions citing equal spans both survive,
        because neither is the container.
        """

        assertions = response.assertions
        if len(assertions) < 2:
            return response, 0

        def envelopes(item) -> dict[str, tuple[int, int]]:
            bounds: dict[str, tuple[int, int]] = {}
            for span in item.source_spans:
                start, end = bounds.get(span.message_id, (span.start, span.end))
                bounds[span.message_id] = (min(start, span.start), max(end, span.end))
            return bounds

        spans = {item.proposal_id: envelopes(item) for item in assertions}
        kept = []
        for item in assertions:
            mine = spans[item.proposal_id]
            nested = False
            for other in assertions:
                if other.proposal_id == item.proposal_id:
                    continue
                if other.memory_type_hint is not item.memory_type_hint:
                    continue
                theirs = spans[other.proposal_id]
                if not mine or set(mine) - set(theirs):
                    continue
                inside = all(
                    theirs[key][0] <= mine[key][0] and mine[key][1] <= theirs[key][1]
                    for key in mine
                )
                narrower = any(
                    theirs[key][0] < mine[key][0] or mine[key][1] < theirs[key][1]
                    for key in mine
                )
                if inside and narrower:
                    nested = True
                    break
            if not nested:
                kept.append(item)
        if len(kept) == len(assertions):
            return response, 0
        return response.model_copy(update={"assertions": tuple(kept)}), len(assertions) - len(kept)

    @staticmethod
    def _apply_candidate_cap(
        request: ExtractionRequest,
        response: ModelProposalResponse,
    ) -> tuple[ModelProposalResponse, int]:
        limit = request.maximum_candidates
        if len(response.assertions) <= limit:
            return response, 0
        ranked = sorted(
            response.assertions,
            key=lambda item: (
                item.durability.value != "durable",
                -item.confidence,
                item.proposal_id,
            ),
        )
        kept = tuple(ranked[:limit])
        groups = {item.correction_group for item in kept if item.correction_group}
        retractions = tuple(
            item
            for item in response.retractions
            if not item.correction_group or item.correction_group in groups
        )
        return (
            response.model_copy(update={"assertions": kept, "retractions": retractions}),
            len(response.assertions) - len(kept),
        )

    def _persist_candidate(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
        candidate: NormalizedExtractionCandidate,
        retractions: tuple[ModelRetractionProposal, ...],
        retraction_grounding: dict[str, GroundingDecision],
        model_summary: ModelProposalSummary,
        *,
        needs_review: bool,
        reason: str,
        additional_grounding_evidence: dict[str, object] | None = None,
    ) -> UUID | None:
        linked = tuple(
            item
            for item in retractions
            if candidate.correction_group and item.correction_group == candidate.correction_group
        )
        spans = [*candidate.grounding_spans]
        for item in linked:
            spans.extend(retraction_grounding[item.proposal_id].spans)
        old_hashes = [
            hashlib.sha256(item.old_value_hint.casefold().encode()).hexdigest() for item in linked
        ]
        command = PersistExtractionCandidateCommand(
            owner_id=request.owner_id,
            candidate=candidate.proposal,
            state=(
                CandidateLifecycleState.NEEDS_REVIEW
                if needs_review
                else CandidateLifecycleState.VALIDATED
            ),
            decision_outcome=MemoryOutcome.NEEDS_REVIEW if needs_review else None,
            rejection_code=(MemoryRejectionCode.AMBIGUOUS_CONFLICT if needs_review else None),
            decision_reason=reason,
            source_message_id=request.message_id,
            source_spans=tuple(spans),
            predecessor_evidence={
                "correction_group": candidate.correction_group,
                "old_value_hashes": old_hashes,
            },
            grounding_evidence={
                "exact_span_count": len(spans),
                "speaker": "user",
                "source_content_hash": request.source_content_hash,
                **(
                    {"expires_at": candidate.expires_at.isoformat()}
                    if candidate.expires_at is not None
                    else {}
                ),
                **(additional_grounding_evidence or {}),
            },
            extractor_name=(
                "memory-model" if model_summary.called else "memory-deterministic-preparser"
            ),
            extractor_version=EXTRACTOR_VERSION,
            raw_output_hash=model_summary.raw_output_hash,
        )
        result = self.adapter.persist_extraction_candidate(context, command)
        if result.outcome in {
            CandidatePersistenceOutcome.PERSISTED,
            CandidatePersistenceOutcome.ALREADY_EXISTS,
        }:
            return result.candidate_id
        return None

    def _apply_candidate(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
        candidate: NormalizedExtractionCandidate,
        resolution: CorrectionResolutionKind,
        targets,
        *,
        transport: str,
    ) -> ExtractionCandidateDecision:
        proposal = candidate.proposal
        item = StructuredMemoryInput(
            memory_type=proposal.memory_type,
            domain_key=proposal.domain_key,
            slot_key=proposal.slot_key,
            cardinality=proposal.cardinality,
            canonical_value=proposal.canonical_value,
            display_text=proposal.display_text,
            # Carry the resolved scope through to the stored record.  Omitting
            # it silently fell back to the "global" default, so a project
            # memory was written as readable from every chat even though the
            # candidate row had already recorded the correct project scope.
            scope_type=proposal.scope_type,
            scope_project_id=proposal.scope_project_id,
            sensitivity=proposal.sensitivity,
            confidence=proposal.confidence,
            importance=proposal.importance,
            explicit_user_request=proposal.explicit_user_request,
            subject_key=proposal.subject_key,
            proposal_id=proposal.proposal_id,
            value_schema_version=proposal.value_schema_version,
            evidence=proposal.evidence,
        )
        effective_context = replace(
            context,
            source_kind=(
                SourceKind.DIRECT_COMMAND
                if request.mode
                in {ExtractionMode.FOREGROUND_DETERMINISTIC, ExtractionMode.EXPLICIT_BATCH}
                else SourceKind.AUTOMATIC_EXTRACTION
            ),
            source_id=EXTRACTOR_VERSION,
            message_id=request.message_id,
            evidence=(),
        )
        if resolution is CorrectionResolutionKind.REPLACE:
            result = self.adapter.apply_structured_replacement(
                effective_context,
                item,
                tuple(
                    TargetRevision(
                        memory_id=target.memory_id,
                        expected_revision=target.revision,
                    )
                    for target in targets
                ),
                extraction_version=EXTRACTOR_VERSION,
                candidate_key=str(proposal.proposal_id),
                transport=transport,
                explicit_domain_change=candidate.explicit_domain_change,
                explicit_slot_change=candidate.explicit_slot_change,
            )
            action = CandidateAction.REPLACE
        else:
            result = self.adapter.apply_structured_candidate(
                effective_context,
                item,
                extraction_version=EXTRACTOR_VERSION,
                candidate_key=str(proposal.proposal_id),
                transport=transport,
            )
            action = (
                CandidateAction.RECONFIRM
                if resolution is CorrectionResolutionKind.RECONFIRM
                else CandidateAction.CREATE
            )
        mutation = result.mutation
        return ExtractionCandidateDecision(
            candidate_id=proposal.proposal_id,
            action=action,
            reason="phase3_command_completed",
            review_required=bool(mutation and mutation.outcome is MemoryOutcome.NEEDS_REVIEW),
            memory_ids=mutation.affected_memory_ids if mutation else (),
            operation_id=mutation.operation_id if mutation else None,
            outcome=mutation.outcome.value if mutation else "disabled",
        )

    def _apply_retraction(
        self,
        request: ExtractionRequest,
        context: MemoryAdapterContext,
        retraction: ModelRetractionProposal,
        memory_id: UUID,
        revision: int,
        lifecycle_hint: LifecycleHint | None,
    ) -> ExtractionCandidateDecision:
        authorized = request.authorized_user_messages()
        evidence = tuple(
            EvidenceSpan(
                role=EvidenceRole.RETRACTION,
                text=authorized[item.message_id][item.start : item.end],
                start=item.start,
                end=item.end,
            )
            for item in retraction.source_spans
        )
        effective_context = replace(
            context,
            source_kind=SourceKind.DIRECT_COMMAND,
            source_id=EXTRACTOR_VERSION,
            message_id=request.message_id,
            evidence=evidence,
        )
        # The target is part of the key.  One retraction can resolve to several
        # memories -- "forget that I use Python" when the value is stored twice --
        # and this runs once per target.  Keyed on the proposal alone, every
        # target computed the same key, so the second forget replayed the first
        # operation's record, found it named a different memory, and returned
        # FAILED while the turn still reported success.
        key = MemoryIdempotency.chat(
            request.owner_id,
            request.message_id,
            EXTRACTOR_VERSION,
            f"{retraction.proposal_id}:{memory_id}",
        )
        target = TargetRevision(memory_id=memory_id, expected_revision=revision)
        action = CandidateAction.RETRACT
        if lifecycle_hint is LifecycleHint.FORGET or retraction.explicit_forget:
            result = self.adapter.forget(effective_context, target, idempotency_key=key)
            action = CandidateAction.FORGET
        elif lifecycle_hint is LifecycleHint.ERASE_PERMANENTLY:
            result = self.adapter.erase_permanently(effective_context, target, idempotency_key=key)
            action = CandidateAction.FORGET
        elif lifecycle_hint is LifecycleHint.RESTORE:
            result = self.adapter.restore(effective_context, target, idempotency_key=key)
        else:
            result = self.adapter.archive(effective_context, target, idempotency_key=key)
        return ExtractionCandidateDecision(
            action=action,
            reason="phase3_lifecycle_command_completed",
            memory_ids=(memory_id,),
            operation_id=result.mutation.operation_id if result.mutation else None,
            outcome=(result.mutation.outcome.value if result.mutation else "disabled"),
        )

    @staticmethod
    def _timing(request: ExtractionRequest, parsed: PreparseResult) -> str:
        if request.mode is ExtractionMode.POST_TURN_AUTOMATIC:
            trigger = ExtractionTrigger.AUTOMATIC_LLM
        elif parsed.kind is PreparseKind.DETERMINISTIC_CORRECTION:
            trigger = ExtractionTrigger.DETERMINISTIC_CORRECTION
        else:
            trigger = ExtractionTrigger.EXPLICIT_MEMORY_COMMAND
        decision = extraction_timing_policy(trigger)
        return decision.timing.value

    @staticmethod
    def _empty_model_summary(
        *, called: bool = False, raw_output_hash: object = None
    ) -> ModelProposalSummary:
        return ModelProposalSummary(
            called=called,
            assertion_count=0,
            retraction_count=0,
            exclusion_count=0,
            raw_output_hash=(str(raw_output_hash) if raw_output_hash else None),
        )

    def _finish(
        self,
        request: ExtractionRequest,
        parsed: PreparseResult,
        *,
        timing: str,
        status: ExtractionStatus,
        model_summary: ModelProposalSummary,
        grounding: tuple[GroundingDecision, ...] = (),
        decisions: tuple[ExtractionCandidateDecision, ...] = (),
        override: CurrentTurnOverride | None = None,
        reason_codes: tuple[str, ...] = (),
        model_meta: dict[str, object] | None = None,
        sensitivity: Sensitivity = Sensitivity.NORMAL,
    ) -> ExtractionResult:
        if sensitivity is not Sensitivity.NORMAL:
            parsed = parsed.model_copy(
                update={
                    "assertions": (),
                    "retractions": (),
                    "sensitive_content_redacted": True,
                }
            )
            model_summary = model_summary.model_copy(update={"raw_output_hash": None})
            grounding = tuple(item.model_copy(update={"spans": ()}) for item in grounding)
            model_meta = {**(model_meta or {}), "response_content_hash": None}
        if override is None:
            builder = CurrentTurnOverrideBuilder(
                owner_id=request.owner_id,
                source_message_id=request.message_id,
            )
            if status is ExtractionStatus.NEEDS_REVIEW:
                for item in parsed.retractions:
                    builder.record_unresolved_hint(item.normalized_value)
            override = builder.build(
                status=status,
                sensitivity=sensitivity,
                positive_current_assertion=(
                    parsed.assertions[0].normalized_value if parsed.assertions else None
                ),
                confidence=(0.99 if parsed.deterministic else (0.5 if parsed.assertions else 0.0)),
            )
        meta = model_meta or {}
        diagnostic = ExtractionDiagnostic(
            request_id=request.request_id,
            owner_id=request.owner_id,
            message_id=request.message_id,
            extractor_version=EXTRACTOR_VERSION,
            model_version=(str(meta["model_version"]) if meta.get("model_version") else None),
            prompt_version=(str(meta["prompt_version"]) if meta.get("prompt_version") else None),
            model_latency_ms=(
                int(meta["model_latency_ms"]) if meta.get("model_latency_ms") is not None else None
            ),
            provider_kind=(str(meta["provider_kind"]) if meta.get("provider_kind") else None),
            http_status=(int(meta["http_status"]) if meta.get("http_status") else None),
            response_envelope_shape=(
                str(meta["response_envelope_shape"])
                if meta.get("response_envelope_shape")
                else None
            ),
            content_present=(
                bool(meta["content_present"]) if meta.get("content_present") is not None else None
            ),
            content_byte_length=(
                int(meta["content_byte_length"])
                if meta.get("content_byte_length") is not None
                else None
            ),
            response_content_hash=(
                str(meta["response_content_hash"]) if meta.get("response_content_hash") else None
            ),
            sanitized_provider_error_code=(
                str(meta["sanitized_provider_error_code"])
                if meta.get("sanitized_provider_error_code")
                else None
            ),
            provider_timeout_stage=(
                str(meta["provider_timeout_stage"]) if meta.get("provider_timeout_stage") else None
            ),
            json_parse_result=(
                str(meta["json_parse_result"]) if meta.get("json_parse_result") else None
            ),
            schema_validation_result=(
                str(meta["schema_validation_result"])
                if meta.get("schema_validation_result")
                else None
            ),
            schema_error_codes=tuple(str(item) for item in meta.get("schema_error_codes", ())),
            parse_outcome=status.value,
            proposal_count=model_summary.assertion_count + model_summary.retraction_count,
            accepted_count=sum(
                item.action
                in {
                    CandidateAction.CREATE,
                    CandidateAction.RECONFIRM,
                    CandidateAction.REPLACE,
                    CandidateAction.RETRACT,
                    CandidateAction.FORGET,
                    CandidateAction.IDEMPOTENT_REPLAY,
                }
                for item in decisions
            ),
            rejected_count=sum(item.action is CandidateAction.REJECT for item in decisions),
            review_count=sum(item.review_required for item in decisions),
            reason_codes=reason_codes,
            operation_ids=tuple(
                item.operation_id for item in decisions if item.operation_id is not None
            ),
            suppressed_memory_ids=override.suppressed_memory_ids,
            candidate_target_memory_ids=override.candidate_target_memory_ids,
            unresolved_conflict_slot_keys=override.unresolved_conflict_slot_keys,
        )
        self.diagnostics.record(diagnostic)
        return ExtractionResult(
            request_id=request.request_id,
            owner_id=request.owner_id,
            message_id=request.message_id,
            status=status,
            timing=timing,
            preparse=parsed,
            model_summary=model_summary,
            grounding=grounding,
            decisions=decisions,
            current_turn_override=override,
            diagnostic=diagnostic,
        )
