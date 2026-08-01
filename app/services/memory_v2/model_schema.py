"""Strict model-visible schema for Phase 4 extraction proposals."""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue, ValidationError, model_validator

from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.extraction_contracts import (
    MODEL_SCHEMA_VERSION,
    ExtractionContractModel,
)
from app.services.memory_v2.taxonomy import MemoryType

MAX_MODEL_OUTPUT_BYTES = 128_000


class DurabilityHint(StrEnum):
    DURABLE = "durable"
    TEMPORARY = "temporary"
    UNCERTAIN = "uncertain"


class SubjectHint(StrEnum):
    USER = "user"
    OTHER = "other"
    AMBIGUOUS = "ambiguous"


class ModelSourceSpan(ExtractionContractModel):
    message_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quoted_text: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_offsets(self) -> ModelSourceSpan:
        if self.end <= self.start:
            raise ValueError("span_end_must_follow_start")
        return self


class ModelAssertionProposal(ExtractionContractModel):
    proposal_id: str = Field(min_length=1, max_length=160)
    source_spans: tuple[ModelSourceSpan, ...] = Field(min_length=1, max_length=4)
    subject_hint: SubjectHint
    memory_type_hint: MemoryType
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    slot_hint: str | None = Field(default=None, min_length=1, max_length=200)
    typed_value: JsonValue
    display_hint: str = Field(min_length=1, max_length=4_000)
    durability: DurabilityHint
    confidence: float = Field(ge=0, le=1)
    sensitivity_hint: Sensitivity
    correction_group: str | None = Field(default=None, min_length=1, max_length=160)
    explicit_type_change: bool = False
    explicit_domain_change: bool = False
    explicit_slot_change: bool = False
    additive: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_expiry_timezone(self) -> ModelAssertionProposal:
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expiry_must_include_timezone")
        return self


class ModelRetractionProposal(ExtractionContractModel):
    proposal_id: str = Field(min_length=1, max_length=160)
    source_spans: tuple[ModelSourceSpan, ...] = Field(min_length=1, max_length=4)
    subject_hint: SubjectHint
    old_value_hint: str = Field(min_length=1, max_length=4_000)
    memory_type_hint: MemoryType | None = None
    domain_hint: str | None = Field(default=None, min_length=1, max_length=200)
    slot_hint: str | None = Field(default=None, min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    correction_group: str | None = Field(default=None, min_length=1, max_length=160)
    explicit_forget: bool = False


class ModelExclusion(ExtractionContractModel):
    proposal_id: str | None = Field(default=None, max_length=160)
    reason: str = Field(min_length=1, max_length=160)


class ModelProposalResponse(ExtractionContractModel):
    schema_version: Literal[MODEL_SCHEMA_VERSION] = MODEL_SCHEMA_VERSION
    assertions: tuple[ModelAssertionProposal, ...] = Field(default=(), max_length=50)
    retractions: tuple[ModelRetractionProposal, ...] = Field(default=(), max_length=50)
    exclusions: tuple[ModelExclusion, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_local_ids(self) -> ModelProposalResponse:
        identifiers = [
            *(item.proposal_id for item in self.assertions),
            *(item.proposal_id for item in self.retractions),
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("model_proposal_ids_must_be_unique")
        return self


class ModelOutputError(ValueError):
    def __init__(self, code: str, *, schema_error_codes: tuple[str, ...] = ()) -> None:
        self.code = code
        self.schema_error_codes = schema_error_codes
        super().__init__(code)


_JSON_CODE_FENCE = re.compile(
    r"\A\s*```json[ \t]*\r?\n(?P<body>.*?)\r?\n```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _schema_error_codes(exc: ValidationError) -> tuple[str, ...]:
    mapping = {
        "extra_forbidden": "schema_unknown_field",
        "missing": "schema_required_field_missing",
        "literal_error": "schema_literal_invalid",
        "enum": "schema_enum_invalid",
        "too_long": "schema_collection_limit_exceeded",
        "list_too_long": "schema_collection_limit_exceeded",
        "tuple_too_long": "schema_collection_limit_exceeded",
    }
    return tuple(
        sorted(
            {
                mapping.get(str(item.get("type")), "schema_field_invalid")
                for item in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            }
        )
    )


def parse_model_output(raw: str | bytes | dict) -> ModelProposalResponse:
    if isinstance(raw, bytes):
        if len(raw) > MAX_MODEL_OUTPUT_BYTES:
            raise ModelOutputError("model_output_too_large")
        try:
            material = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ModelOutputError("model_output_not_utf8") from None
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_MODEL_OUTPUT_BYTES:
            raise ModelOutputError("model_output_too_large")
        material = raw
    elif isinstance(raw, dict):
        material = raw
    else:
        raise ModelOutputError("model_output_type_invalid")
    if isinstance(material, str):
        fence = _JSON_CODE_FENCE.fullmatch(material)
        if fence:
            material = fence.group("body")
        try:
            material = json.loads(material)
        except (TypeError, ValueError):
            raise ModelOutputError("malformed_model_json") from None
    try:
        return ModelProposalResponse.model_validate(material)
    except ValidationError as exc:
        raise ModelOutputError(
            "invalid_model_schema",
            schema_error_codes=_schema_error_codes(exc),
        ) from None
