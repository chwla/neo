from __future__ import annotations

import re
from uuid import UUID

from app.services.memory.prompt import RecallPromptOrchestrator
from app.services.memory.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallPromptSelection,
    RecallQuery,
)
from app.services.memory.taxonomy import CORE_IDENTITY_SLOT_KEYS, MemoryType

# "Who am I" names no attribute, so every keyword rule below missed it and the
# question that most obviously belongs to this service was the one it declined to
# answer.  It is not a broad request either: somebody asking who they are wants
# their profile, not their goals and preferences, so it is routed to the core
# identity slots as a group.
_IDENTITY_PROFILE_QUESTION = re.compile(
    r"\bwho\s+(?:am\s+i|i\s+am)\b"
    r"|\btell\s+me\s+about\s+my\s*self\b"
    r"|\bdescribe\s+me\b",
    re.IGNORECASE,
)


class DirectMemoryAnswerService:
    """Answer explicit personal-memory questions from canonical recall only."""

    def __init__(
        self,
        orchestrator: RecallPromptOrchestrator | None,
        *,
        enabled: bool = True,
    ) -> None:
        self.orchestrator = orchestrator
        self.enabled = enabled
        self.last_canonical_ids: tuple[UUID, ...] = ()
        self.last_selection: RecallPromptSelection | None = None

    def answer(self, query: str, *, context: MemoryQueryContext | None) -> str | None:
        self.last_canonical_ids = ()
        self.last_selection = None
        if not self.enabled or self.orchestrator is None or context is None:
            return None
        if not context.memory_enabled or context.incognito:
            return None
        lowered = query.casefold()
        broad = bool(
            re.search(
                r"\b(?:what do you remember|what do you know about me"
                r"|show|list|summari[sz]e)\b",
                lowered,
            )
        )
        if not broad and not self._is_personal_memory_question(lowered):
            return None
        profile_question = bool(_IDENTITY_PROFILE_QUESTION.search(lowered))
        memory_type = MemoryType.IDENTITY if profile_question else self._memory_type(lowered)
        if memory_type is None and not broad:
            return None
        mode = RecallMode.BROAD if broad else RecallMode.DETERMINISTIC
        selection = self.orchestrator.build(
            RecallQuery(
                context=context.model_copy(update={"mode": mode}),
                text=query,
                memory_type=memory_type,
                trusted_slot_keys=(
                    tuple(sorted(CORE_IDENTITY_SLOT_KEYS))
                    if profile_question
                    else self._trusted_slots(lowered)
                ),
            ),
            purpose="direct_answer",
        )
        self.last_selection = selection
        final_ids = selection.serialized.canonical_ids if selection.serialized is not None else ()
        self.last_canonical_ids = final_ids
        items = tuple(
            item for item in selection.recall.items if item.memory.canonical_id in final_ids
        )
        if not items:
            return "I do not have an active saved memory that answers that yet."
        values = [item.memory.display_text.strip() for item in items]
        if len(values) == 1:
            return f"From your saved memory: {values[0]}"
        return "From your saved memories:\n" + "\n".join(f"- {value}" for value in values)

    @staticmethod
    def _is_personal_memory_question(text: str) -> bool:
        """Keep durable first-person assertions out of the recall-only path."""

        if not re.search(r"\b(?:my|mine|me|i)\b", text):
            return False
        return bool(
            text.rstrip().endswith("?")
            or re.match(
                r"^\s*(?:what(?:'s|s)?|who(?:'s|s)?|where(?:'s|s)?|which|how|"
                r"do|did|have|can|could|would|"
                r"tell me|remind me)\b",
                text,
            )
        )

    @staticmethod
    def _memory_type(text: str) -> MemoryType | None:
        # Each alternation is grouped so the word boundaries apply to every
        # branch.  Without the group, "age" matched inside words such as
        # "manage" and mis-routed unrelated questions to identity recall.
        if re.search(r"\b(?:goals?|trying to|want to create)\b", text):
            return MemoryType.GOAL
        if re.search(r"\b(?:preferences?|prefer|like)\b", text):
            return MemoryType.PREFERENCE
        if re.search(r"\b(?:projects?|working on)\b", text):
            return MemoryType.PROJECT
        if re.search(
            r"\b(?:name|age|old|born|live|living|location|city|country|from|"
            r"hometown|occupation|profession|job|employer|company|work|"
            r"education|study|studied)\b",
            text,
        ):
            return MemoryType.IDENTITY
        if re.search(r"\b(?:activities?|doing currently)\b", text):
            return MemoryType.ACTIVITY
        return None

    @staticmethod
    def _trusted_slots(text: str) -> tuple[str, ...]:
        if re.search(r"\bname\b", text):
            return ("identity:global:name",)
        if re.search(r"\b(?:age|old|born)\b", text):
            return ("identity:global:age",)
        if re.search(r"\b(?:hometown|from)\b", text):
            return ("identity:global:origin",)
        if re.search(r"\b(?:employer|company|work|job)\b", text):
            return ("identity:global:employer", "identity:global:occupation")
        if re.search(r"\b(?:occupation|profession)\b", text):
            return ("identity:global:occupation", "identity:global:employer")
        if re.search(r"\b(?:live|living|location|city)\b", text):
            return ("identity:global:current_location",)
        return ()
