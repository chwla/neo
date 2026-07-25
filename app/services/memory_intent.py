from __future__ import annotations

import re
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryIntentKind(StrEnum):
    STORE = "memory_store"
    UPDATE = "memory_update"
    EXCLUDE = "memory_exclude"
    REMOVE = "memory_remove"
    QUERY = "memory_query"
    NONE = "memory_none"


class ResolvedMemoryIntent(BaseModel):
    kind: MemoryIntentKind
    confidence: float = Field(ge=0, le=1)
    reason: str
    target_text: str | None = None


class MemoryMutationResult(BaseModel):
    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    intent: MemoryIntentKind
    attempted: bool = False
    succeeded: bool = True
    saved_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    excluded_count: int = 0
    candidate_count: int = 0
    candidate_ids: list[int] = Field(default_factory=list)
    memory_ids: list[int] = Field(default_factory=list)
    review_decisions: list[str] = Field(default_factory=list)
    persistence_status: str = "not_attempted"
    response_status_source: str = "backend"
    report_status: bool = False
    error: str | None = None

    @property
    def durable_mutation_succeeded(self) -> bool:
        return self.succeeded and bool(
            self.saved_count or self.updated_count or self.removed_count
        )

    def acknowledgement(self) -> str | None:
        if not self.report_status:
            return None
        if not self.succeeded:
            return (
                "I could not complete the durable-memory operation. "
                "Nothing was reported as saved, updated, or removed."
            )
        if self.removed_count:
            noun = "memory" if self.removed_count == 1 else "memories"
            return f"Removed {self.removed_count} matching durable {noun}."
        if self.saved_count or self.updated_count:
            if self.saved_count and not self.updated_count:
                noun = "memory" if self.saved_count == 1 else "memories"
                return (
                    f"Saved {self.saved_count} durable {noun} after extraction and review."
                )
            if self.updated_count and not self.saved_count:
                noun = "memory" if self.updated_count == 1 else "memories"
                return (
                    f"Updated {self.updated_count} durable {noun} after extraction and review."
                )
            return (
                f"Memory operation confirmed: saved {self.saved_count} and updated "
                f"{self.updated_count} durable memories after extraction and review."
            )
        if self.excluded_count:
            return (
                "Understood — I’ll keep that in this conversation only. "
                "It was not added to durable memory."
            )
        return None


_EXCLUSION_PATTERNS = (
    r"\bfor\s+this\s+(?:message|conversation|chat|turn)\s+only\b",
    r"\bthis\s+is\s+temporary\b",
    r"\buse\s+this\s+only\s+for\s+(?:this|the current)\s+"
    r"(?:conversation|chat|turn)\b",
    r"\b(?:do\s+not|don't|never|please\s+don't)\s+"
    r"(?:remember|save|store|keep|add)\b",
    r"\bdo\s+not\s+add\b.*\b(?:memory|memories)\b",
)

_REMOVAL_PATTERNS = (
    r"\b(?:forget|delete)\b.*\b(?:that|this|memory|memories|fact|preference|information)\b",
    r"\bremove\b.*\b(?:from\s+(?:my\s+)?memory|memory|memories|preference|fact)\b",
    r"\bstop\s+remembering\b",
)


def resolve_memory_intent(text: str) -> ResolvedMemoryIntent:
    normalized = " ".join(text.strip().split())
    lowered = normalized.casefold()
    if not normalized:
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.NONE,
            confidence=1.0,
            reason="Empty input has no memory intent.",
        )

    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _EXCLUSION_PATTERNS):
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.EXCLUDE,
            confidence=0.99,
            reason="Explicitly scoped the information to temporary conversation context.",
        )

    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _REMOVAL_PATTERNS):
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.REMOVE,
            confidence=0.99,
            reason="Explicitly requested removal of existing durable memory.",
            target_text=extract_memory_removal_target(normalized),
        )

    if re.search(
        r"\b(?:what|which|show|list|summari[sz]e|tell\s+me)\b.*"
        r"\b(?:remember|memory|memories|saved\s+facts?|preferences?)\b|"
        r"\bbased\s+only\s+on\b.*\bmemor(?:y|ies)\b",
        lowered,
    ):
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.QUERY,
            confidence=0.98,
            reason="Requested recall from the durable memory store.",
        )

    if re.search(
        r"\b(?:remember|save|store|add\s+to\s+(?:my\s+)?memory)\s+"
        r"(?:that\s+)?",
        lowered,
    ):
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.STORE,
            confidence=0.97,
            reason="Explicitly requested durable storage.",
        )

    if re.search(
        r"^\s*(?:correction|actually|update|change)\b|"
        r"\b(?:i\s+now\s+prefer|my\s+preference\s+is\s+now)\b",
        lowered,
    ):
        return ResolvedMemoryIntent(
            kind=MemoryIntentKind.UPDATE,
            confidence=0.9,
            reason="Expressed a correction or replacement for an existing memory.",
        )

    return ResolvedMemoryIntent(
        kind=MemoryIntentKind.NONE,
        confidence=1.0,
        reason="No explicit memory operation was requested.",
    )


def extract_memory_removal_target(text: str) -> str | None:
    match = re.search(
        r"\b(?:forget|stop\s+remembering)\s+(?:that\s+)?(?P<forget>[^.!?]+)|"
        r"\b(?:remove|delete)\s+"
        r"(?:(?:the|this|that|my|a|an|saved)\s+)*"
        r"(?:memory|memories|fact|facts|preference|information)"
        r"(?:\s+(?:saying|that))?\s+(?P<named>[^.!?]+)|"
        r"\b(?:remove|delete)\s+(?:that\s+)?(?P<fallback>[^.!?]+?)"
        r"(?:\s+from\s+(?:my\s+)?memory)?$",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    target = next(
        (
            value
            for value in (
                match.group("forget"),
                match.group("named"),
                match.group("fallback"),
            )
            if value
        ),
        "",
    ).strip(" .,!?:;")
    if re.fullmatch(
        r"(?:(?:that|this|the)\s+)?(?:memory|memories|fact|facts|preference|information)|"
        r"(?:that|this)(?:\s+from\s+(?:my\s+)?memory)?|"
        r"from\s+(?:my\s+)?memory",
        target,
        flags=re.IGNORECASE,
    ):
        return None
    return target or None
