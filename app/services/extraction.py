from __future__ import annotations

import json
import re
from datetime import date

from pydantic import BaseModel, Field

from app.models import MemoryCandidate
from app.models.enums import CandidateStatus, CandidateType
from app.repositories.memory_store import MemoryStore
from app.services.scoring import score_importance


class ConversationMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1)


class ExtractionRequest(BaseModel):
    text: str | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)
    persist: bool = True


class ExtractedItem(BaseModel):
    candidate_type: CandidateType
    text: str
    confidence: float = Field(default=0.75, ge=0, le=1)
    importance: int = Field(default=5, ge=1, le=10)
    attributes: dict[str, str | int | float | None] = Field(default_factory=dict)
    reasoning: str


class ExtractionResult(BaseModel):
    identity: list[ExtractedItem] = Field(default_factory=list)
    preferences: list[ExtractedItem] = Field(default_factory=list)
    goals: list[ExtractedItem] = Field(default_factory=list)
    projects: list[ExtractedItem] = Field(default_factory=list)
    events: list[ExtractedItem] = Field(default_factory=list)
    memories: list[ExtractedItem] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)
    candidate_ids: list[int] = Field(default_factory=list)

    @property
    def items(self) -> list[ExtractedItem]:
        return [
            *self.identity,
            *self.preferences,
            *self.goals,
            *self.projects,
            *self.events,
            *self.memories,
        ]


class MemoryExtractionService:
    """Extract durable memory candidates from conversation text."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        text = self._request_text(request)
        result = ExtractionResult()
        if not text.strip():
            return result

        for sentence in self._sentences(text):
            matched = False
            for extractor in (
                self._extract_identity,
                self._extract_preference,
                self._extract_goal,
                self._extract_project,
                self._extract_event,
                self._extract_memory,
            ):
                item = extractor(sentence)
                if item is not None:
                    self._append(result, item)
                    matched = True
                    break
            if not matched:
                result.ignored.append(sentence)

        return result

    def persist_candidates(
        self,
        store: MemoryStore,
        extraction: ExtractionResult,
    ) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        for item in extraction.items:
            candidate = MemoryCandidate(
                candidate_text=item.text,
                candidate_type=item.candidate_type,
                confidence=item.confidence,
                importance=item.importance,
                reasoning=json.dumps(
                    {"reasoning": item.reasoning, "attributes": item.attributes},
                    sort_keys=True,
                ),
                status=CandidateStatus.PENDING,
            )
            candidates.append(store.add(candidate))
        extraction.candidate_ids = [candidate.id for candidate in candidates]
        return candidates

    def _request_text(self, request: ExtractionRequest) -> str:
        if request.text:
            return request.text
        return "\n".join(message.content for message in request.messages if message.role == "user")

    def _sentences(self, text: str) -> list[str]:
        parts = re.split(r"(?<=[.!?])\s+|\n+", text)
        return [part.strip(" .\t\r\n") for part in parts if part.strip(" .\t\r\n")]

    def _extract_identity(self, sentence: str) -> ExtractedItem | None:
        patterns = [
            (r"\bmy name is (?P<value>[A-Z][A-Za-z .'-]{1,80})", "name"),
            (r"\bi am (?P<value>a |an )?(?P<occupation>[^.]{3,80})", "occupation"),
            (r"\bi'?m (?P<value>a |an )?(?P<occupation>[^.]{3,80})", "occupation"),
            (r"\bi live in (?P<value>[A-Za-z ,'-]{2,80})", "location"),
            (r"\bi study at (?P<value>[A-Za-z0-9 ,.'-]{2,120})", "education"),
        ]
        for pattern, key in patterns:
            match = re.search(pattern, sentence, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.groupdict().get("occupation") or match.groupdict().get("value")
            if not value:
                continue
            value = re.sub(r"^(a|an)\s+", "", value.strip(), flags=re.IGNORECASE)
            text = f"{key} = {value}"
            return ExtractedItem(
                candidate_type=CandidateType.IDENTITY,
                text=text,
                confidence=0.82,
                importance=score_importance(text),
                attributes={"key": key, "value": value},
                reasoning="Detected durable identity statement.",
            )
        return None

    def _extract_preference(self, sentence: str) -> ExtractedItem | None:
        match = re.search(
            r"\bi prefer (?P<value>[^.]{3,160})|\bi like (?P<like>[^.]{3,160})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        value = (match.group("value") or match.group("like")).strip()
        category = self._preference_category(value)
        text = f"{category} = {value}"
        return ExtractedItem(
            candidate_type=CandidateType.PREFERENCE,
            text=text,
            confidence=0.78,
            importance=score_importance(text),
            attributes={"category": category, "value": value},
            reasoning="Detected user preference.",
        )

    def _extract_goal(self, sentence: str) -> ExtractedItem | None:
        match = re.search(
            r"\b(i want to|my goal is to|i need to|i plan to)\s+(?P<goal>[^.]{3,180})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        goal = match.group("goal").strip()
        explicit_priority = 10 if "highest" in sentence.lower() else None
        priority = score_importance(goal, explicit_priority=explicit_priority)
        return ExtractedItem(
            candidate_type=CandidateType.GOAL,
            text=goal,
            confidence=0.8,
            importance=priority,
            attributes={"goal": goal, "priority": priority},
            reasoning="Detected active or intended user goal.",
        )

    def _extract_project(self, sentence: str) -> ExtractedItem | None:
        match = re.search(
            r"\b(project|building|working on)\s+(?P<name>[A-Z][A-Za-z0-9 _-]{1,80})",
            sentence,
        )
        if not match:
            return None
        name = match.group("name").strip()
        return ExtractedItem(
            candidate_type=CandidateType.PROJECT,
            text=name,
            confidence=0.72,
            importance=score_importance(sentence),
            attributes={"name": name, "description": sentence},
            reasoning="Detected project reference.",
        )

    def _extract_event(self, sentence: str) -> ExtractedItem | None:
        event_pattern = r"\b(started|finished|completed|graduated|moved|built|launched)\b"
        if not re.search(event_pattern, sentence, re.I):
            return None
        event_date = self._extract_iso_date(sentence)
        event_date_value = event_date.isoformat() if event_date else None
        return ExtractedItem(
            candidate_type=CandidateType.EVENT,
            text=sentence,
            confidence=0.65,
            importance=score_importance(sentence),
            attributes={"event": sentence, "event_date": event_date_value},
            reasoning="Detected timeline event.",
        )

    def _extract_memory(self, sentence: str) -> ExtractedItem | None:
        memory_pattern = r"\b(always|never|important|remember that|long[- ]term)\b"
        if not re.search(memory_pattern, sentence, re.I):
            return None
        return ExtractedItem(
            candidate_type=CandidateType.MEMORY,
            text=sentence,
            confidence=0.68,
            importance=score_importance(sentence),
            attributes={"memory_text": sentence},
            reasoning="Detected durable general memory candidate.",
        )

    def _append(self, result: ExtractionResult, item: ExtractedItem) -> None:
        target = {
            CandidateType.IDENTITY: result.identity,
            CandidateType.PREFERENCE: result.preferences,
            CandidateType.GOAL: result.goals,
            CandidateType.PROJECT: result.projects,
            CandidateType.EVENT: result.events,
            CandidateType.MEMORY: result.memories,
        }[item.candidate_type]
        target.append(item)

    def _preference_category(self, value: str) -> str:
        normalized = value.lower()
        if "explanation" in normalized or "answer" in normalized:
            return "response_style"
        if "python" in normalized or "typescript" in normalized:
            return "technology"
        return "general"

    def _extract_iso_date(self, text: str) -> date | None:
        match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
        if not match:
            return None
        return date.fromisoformat(match.group(0))
