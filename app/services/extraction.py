from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.models import MemoryCandidate
from app.models.enums import CandidateStatus, CandidateType
from app.repositories.memory_store import MemoryStore
from app.services.identity_facts import is_durable_identity_fact, normalize_identity_value
from app.services.llm import LLMClient
from app.services.llm import LLMMessage as OllamaMessage
from app.services.scoring import score_importance


class ConversationMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1)


class ExtractionRequest(BaseModel):
    text: str | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)
    persist: bool = True
    source_conversation_id: int | None = None
    source_message_id: int | None = None
    source_timestamp: datetime | None = None


class ExtractedItem(BaseModel):
    candidate_type: CandidateType
    text: str
    confidence: float = Field(default=0.75, ge=0, le=1)
    importance: int = Field(default=5, ge=1, le=10)
    attributes: dict[str, str | int | float | None] = Field(default_factory=dict)
    reasoning: str


class ExtractionResult(BaseModel):
    identity: list[ExtractedItem] = Field(default_factory=list)
    education: list[ExtractedItem] = Field(default_factory=list)
    preferences: list[ExtractedItem] = Field(default_factory=list)
    goals: list[ExtractedItem] = Field(default_factory=list)
    projects: list[ExtractedItem] = Field(default_factory=list)
    activities: list[ExtractedItem] = Field(default_factory=list)
    events: list[ExtractedItem] = Field(default_factory=list)
    memories: list[ExtractedItem] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)
    candidate_ids: list[int] = Field(default_factory=list)

    @property
    def items(self) -> list[ExtractedItem]:
        return [
            *self.identity,
            *self.education,
            *self.preferences,
            *self.goals,
            *self.projects,
            *self.activities,
            *self.events,
            *self.memories,
        ]


class MemoryExtractionService:
    """Extract durable memory candidates from conversation text."""

    AUTO_ACCEPT_MIN_CONFIDENCE = 0.8
    LLM_SYSTEM_PROMPT = """
You extract durable user memory from conversations for a local personal assistant.
Return JSON only, with this shape:
{
  "items": [
    {
      "type": "identity|education|preference|goal|project|activity|event|memory",
      "text": "short human sentence",
      "source_span": "exact words from the user that support this item",
      "confidence": 0.0,
      "importance": 1,
      "attributes": {}
    }
  ]
}

Rules:
- Extract only stable facts about the user, their preferences, goals, projects, timeline events,
  and explicit instructions the assistant should remember.
- Do not store temporary requests, assistant claims, or facts not grounded in the user's words.
- For preferences such as "be concise", use type "preference", category "response_style",
  and value like "concise answers".
- For identity, set attributes {"key":"name|location|occupation|education|general","value":"..."}.
- For education, include institution, degree, field_of_study, and graduated only when stated.
- For goals, set attributes {"goal":"...","priority":1-10}.
- For projects, set attributes {"name":"...","description":"..."}.
- For current activities, set attributes {"category":"playing|reading|watching|working_on",
  "activity":"...","subject":"..."}. Do not infer an activity from a question.
- For events, set attributes {"event":"...","event_date":"YYYY-MM-DD"} when a date is explicit.
- For general memories, set attributes {"memory_text":"..."}.
- Every item must be grounded by source_span copied exactly from the user's message.
- If nothing durable should be stored, return {"items":[]}.
""".strip()

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        text = self._request_text(request)
        result = ExtractionResult()
        if not text.strip():
            return result

        self._extract_structured_profile(text, result)

        for sentence in self._sentences(text):
            if self._structural_fragment(sentence) or self._non_memory_sentence(sentence):
                continue
            project_items = self._extract_active_project_list(sentence)
            if project_items:
                for item in project_items:
                    self._append_unique(result, item)
                continue
            matched = False
            education_items = self._extract_education_items(sentence)
            for item in education_items:
                self._append_unique(result, item)
                matched = True
            for item in self._extract_identity_items(sentence):
                self._append_unique(result, item)
                matched = True
            for extractor in (
                self._extract_preference,
                lambda value: self._extract_goal(value, request.source_timestamp),
                self._extract_project,
                lambda value: self._extract_activity(value, request.source_timestamp),
            ):
                item = extractor(sentence)
                if item is not None:
                    self._append_unique(result, item)
                    matched = True
            if not education_items:
                event = self._extract_event(sentence)
                if event is not None:
                    self._append_unique(result, event)
                    matched = True
            if not matched:
                for extractor in (self._extract_hardware, self._extract_memory):
                    item = extractor(sentence)
                    if item is not None:
                        self._append_unique(result, item)
                        matched = True
                        break
            if not matched:
                result.ignored.append(sentence)

        self._stamp_source_context(result, request)
        return result

    def is_pure_personal_declaration(
        self,
        request: ExtractionRequest,
        extraction: ExtractionResult | None = None,
    ) -> bool:
        """Whether a turn contains only explicit user-memory declarations."""

        extraction = extraction or self.extract(request)
        if not extraction.items or extraction.ignored:
            return False
        sentences = self._sentences(self._request_text(request))
        return bool(sentences) and all(
            not self._non_memory_sentence(sentence) for sentence in sentences
        )

    def format_persisted_acknowledgement(
        self,
        request: ExtractionRequest,
        extraction: ExtractionResult,
        candidates: list[MemoryCandidate],
    ) -> str | None:
        """Return a truthful acknowledgement only after every candidate was accepted."""

        if not self.is_pure_personal_declaration(request, extraction):
            return None
        if not candidates or any(
            candidate.status != CandidateStatus.ACCEPTED for candidate in candidates
        ):
            return None
        return "Got it — I’ve saved that to your memory."

    def _extract_active_project_list(self, sentence: str) -> list[ExtractedItem]:
        """Extract explicitly named projects without treating work as a person's name."""

        called_project = re.search(
            r"^\s*i(?:\s+am|'m)?\s+(?:currently\s+)?"
            r"(?:building|developing|creating|working\s+on)\s+"
            r"(?:a\s+)?project\s+(?:called|named)\s+"
            r"(?P<name>[A-Za-z][A-Za-z0-9_.+ -]{0,80})\s*$",
            sentence,
            flags=re.IGNORECASE,
        )
        if called_project:
            name = called_project.group("name").strip(" .")
            if name.islower():
                name = " ".join(part.capitalize() for part in name.split())
            return [
                self._project_item(
                    name,
                    f"Currently building {name}",
                    "Detected an explicitly named active project.",
                )
            ]

        project_list = re.search(
            r"^\s*my\s+(?:active\s+|current\s+)?projects?\s+(?:are|include)\s+"
            r"(?P<names>[A-Za-z][A-Za-z0-9_.+-]*(?:"
            r"\s*(?:,\s*(?:and\s+)?|&\s*|\band\s+)"
            r"[A-Za-z][A-Za-z0-9_.+-]*){0,7})\s*$",
            sentence,
            flags=re.IGNORECASE,
        )
        match = re.search(
            r"^\s*i(?:\s+am|'m)?\s+(?:currently\s+)?"
            r"(?:building|developing|creating|working\s+on)\s+"
            r"(?P<names>[A-Za-z][A-Za-z0-9_.+-]*(?:"
            r"\s*(?:,\s*(?:and\s+)?|&\s*|\band\s+)"
            r"[A-Za-z][A-Za-z0-9_.+-]*){0,7})\s*$",
            sentence,
            flags=re.IGNORECASE,
        )
        match = match or project_list
        if not match:
            return []

        names = re.sub(r",\s+and\s+", ", ", match.group("names"), flags=re.IGNORECASE)
        raw_names = re.split(
            r"\s*(?:,|&|\band\b)\s*",
            names,
            flags=re.IGNORECASE,
        )
        items: list[ExtractedItem] = []
        for raw_name in raw_names:
            name = raw_name.strip(" .")
            if not name:
                continue
            if name.islower():
                name = name[0].upper() + name[1:]
            items.append(
                self._project_item(
                    name,
                    f"Currently building {name}",
                    "Detected an explicitly named active project.",
                )
            )
        return items

    def extract_with_llm(
        self,
        request: ExtractionRequest,
        ollama: LLMClient | None,
    ) -> ExtractionResult:
        deterministic = self.extract(request)
        if ollama is None:
            return deterministic
        if not deterministic.ignored:
            return deterministic
        user_text = self._request_text(request)
        if not re.search(r"\b(?:i|i'm|i’ve|i've|my|me)\b", user_text, re.IGNORECASE):
            return deterministic

        try:
            response = ollama.chat(
                [
                    OllamaMessage(role="system", content=self.LLM_SYSTEM_PROMPT),
                    OllamaMessage(
                        role="user",
                        content=f"Conversation:\n{self._conversation_text(request)}",
                    ),
                ],
                temperature=0.0,
            )
            llm_result = self._result_from_llm_response(response)
        except Exception:
            return deterministic

        for item in llm_result.items:
            if not self._valid_model_item(item, user_text):
                continue
            item.attributes["auto_accept"] = 0
            item.reasoning = f"{item.reasoning} Pending review: model-only extraction."
            self._append_unique(deterministic, item)
        self._stamp_source_context(deterministic, request)
        return deterministic

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

    def persist_and_accept(
        self,
        store: MemoryStore,
        extraction: ExtractionResult,
    ) -> list[MemoryCandidate]:
        from app.services.review import MemoryReviewRequest, MemoryReviewService

        candidates = self.persist_candidates(store, extraction)
        reviewer = MemoryReviewService()
        for item, candidate in zip(extraction.items, candidates, strict=True):
            if (
                item.attributes.get("auto_accept") == 0
                or item.confidence < self.AUTO_ACCEPT_MIN_CONFIDENCE
            ):
                continue
            reviewer.review(
                store,
                MemoryReviewRequest(
                    candidate_id=candidate.id,
                    decision=CandidateStatus.ACCEPTED,
                ),
            )
        return candidates

    def _request_text(self, request: ExtractionRequest) -> str:
        if request.text:
            return request.text
        return "\n".join(message.content for message in request.messages if message.role == "user")

    def _fallback_request(self, request: ExtractionRequest) -> ExtractionRequest:
        return ExtractionRequest(
            text=self._request_text(request),
            persist=request.persist,
            source_conversation_id=request.source_conversation_id,
            source_message_id=request.source_message_id,
            source_timestamp=request.source_timestamp,
        )

    def _conversation_text(self, request: ExtractionRequest) -> str:
        if request.messages:
            return "\n".join(
                f"user: {message.content}" for message in request.messages if message.role == "user"
            )
        return request.text or ""

    def _sentences(self, text: str) -> list[str]:
        parts = re.split(
            r"(?<=[.!?;])\s+|,\s+(?=(?:who|what|when|where|why|how|which)\b)|\n+",
            text,
            flags=re.IGNORECASE,
        )
        clauses: list[str] = []
        clause_pattern = (
            r"\s+\band\s+"
            r"(?=(?:my name is|my age is|call me|i go by|i am|i'm|i live in|"
            r"i am based in|i'm based in|i moved to|i study at|"
            r"i prefer|i like|i want to|my goal is|i need to|i plan to)\b)"
        )
        for part in parts:
            clauses.extend(re.split(clause_pattern, part, flags=re.IGNORECASE))
        return [clause.strip(" .;\t\r\n") for clause in clauses if clause.strip(" .;\t\r\n")]

    def _structural_fragment(self, text: str) -> bool:
        stripped = text.strip()
        return bool(
            re.match(r"^[A-Za-z][A-Za-z0-9 /&+.'-]{1,80}:$", stripped)
            or re.match(r"^(?:[-*\u2022]\s+|\d+[.)]\s+)", stripped)
        )

    def _non_memory_sentence(self, sentence: str) -> bool:
        stripped = sentence.strip()
        if not stripped:
            return True
        if "?" in stripped or re.match(
            r"^(?:who|what|when|where|why|how|which|do|does|did|can|could|"
            r"should|would|will|is|are|am|was|were|please|tell|show|find|search|"
            r"explain|describe|compare|write|open|run|check|try)\b",
            stripped,
            flags=re.IGNORECASE,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:he|she|they|someone|the assistant)\s+(?:said|says|claimed|"
                r"told me)\b",
                stripped,
                flags=re.IGNORECASE,
            )
            or re.match(r"^[\"“'][^\"”']+[\"”']$", stripped)
        )

    def _extract_structured_profile(self, text: str, result: ExtractionResult) -> None:
        lines = text.splitlines()
        section: str | None = None
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue

            label = self._label_line(line)
            if label is not None:
                section_name, value = label
                if value:
                    item = self._item_from_labeled_value(section_name, value)
                    if item is not None:
                        self._append_unique(result, item)
                    section = None
                else:
                    section = section_name
                index += 1
                continue

            list_item = self._list_item_text(line)
            if section and list_item:
                if section == "main projects":
                    descriptions: list[str] = []
                    cursor = index + 1
                    while cursor < len(lines):
                        next_line = lines[cursor].strip()
                        if not next_line:
                            cursor += 1
                            continue
                        if self._label_line(next_line) is not None:
                            break
                        if re.match(r"^\d+[.)]\s+", next_line):
                            break
                        next_item = self._list_item_text(next_line)
                        if next_item:
                            descriptions.append(next_item)
                            cursor += 1
                            continue
                        break
                    description = "; ".join(descriptions) or list_item
                    self._append_unique(
                        result,
                        self._project_item(
                            list_item, description, "Structured main project entry."
                        ),
                    )
                    index = cursor
                    continue

                item = self._item_from_section(section, list_item)
                if item is None and section in {"remember these", "remember this", "facts"}:
                    item = self._item_from_remembered_list_entry(list_item)
                if item is not None:
                    self._append_unique(result, item)

            index += 1

    def _label_line(self, line: str) -> tuple[str, str] | None:
        match = re.match(
            r"^(?P<label>[A-Za-z][A-Za-z0-9 /&+.'-]{1,80}):\s*(?P<value>.*)$",
            line,
        )
        if not match:
            return None
        label = re.sub(r"\s+", " ", match.group("label").strip().lower())
        value = match.group("value").strip()
        return label, value

    def _list_item_text(self, line: str) -> str | None:
        cleaned = re.sub(r"^\s*(?:[-*\u2022]\s+|\d+[.)]\s+)", "", line).strip()
        cleaned = cleaned.strip(" .\t\r\n")
        return cleaned or None

    def _item_from_labeled_value(self, label: str, value: str) -> ExtractedItem | None:
        identity_keys = {
            "name": "name",
            "location": "location",
            "education": "education",
            "degree": "education",
            "age": "age",
            "occupation": "occupation",
        }
        if label in identity_keys:
            key = identity_keys[label]
            text = f"{key} = {value}"
            return ExtractedItem(
                candidate_type=CandidateType.IDENTITY,
                text=text,
                confidence=0.9,
                importance=score_importance(text),
                attributes={"key": key, "value": value},
                reasoning="Detected structured profile field.",
            )

        if label in {"hardware interest"}:
            return self._memory_item(
                f"Hardware interest: {value}",
                confidence=0.82,
                importance=5,
                reasoning="Detected structured hardware interest.",
            )

        if label in {"hardware", "current hardware", "hardware setup", "computer", "laptop", "pc"}:
            return self._hardware_item(value)

        return self._item_from_section(label, value)

    def _item_from_section(self, section: str, value: str) -> ExtractedItem | None:
        if section == "primary goals":
            return ExtractedItem(
                candidate_type=CandidateType.GOAL,
                text=value,
                confidence=0.86,
                importance=score_importance(value, explicit_priority=8),
                attributes={"goal": value, "priority": 8},
                reasoning="Detected structured primary goal.",
            )

        if section == "main projects":
            return self._project_item(value, value, "Detected structured project.")

        memory_prefixes = {
            "current focus": ("Current focus", 7),
            "interested fields": ("Interested field", 6),
            "languages": ("Programming language", 6),
            "preferred stack": ("Preferred stack", 7),
            "career history": ("Career history", 6),
            "long-term interests": ("Long-term interest", 6),
            "hardware interest": ("Hardware interest", 5),
            "personality patterns": ("Personality pattern", 5),
            "skills": ("Skill", 6),
            "tools": ("Tool", 6),
        }
        if section in memory_prefixes:
            prefix, importance = memory_prefixes[section]
            return self._memory_item(
                f"{prefix}: {value}",
                confidence=0.82,
                importance=importance,
                reasoning=f"Detected structured {section} entry.",
            )

        return None

    def _item_from_remembered_list_entry(self, value: str) -> ExtractedItem:
        education_items = self._extract_education_items(value)
        if education_items:
            return education_items[0]
        for extractor in (
            self._extract_identity,
            self._extract_preference,
            self._extract_goal,
            self._extract_project,
            self._extract_activity,
            self._extract_event,
            self._extract_hardware,
            self._extract_memory,
        ):
            item = extractor(value)
            if item is not None:
                return item
        return self._memory_item(
            value,
            confidence=0.76,
            importance=score_importance(value),
            reasoning="Detected remembered list entry.",
        )

    def _project_item(self, name: str, description: str, reasoning: str) -> ExtractedItem:
        project_name = name.strip(" .")
        project_description = description.strip(" .")
        text = (
            f"{project_name}: {project_description}"
            if project_description and project_description != project_name
            else project_name
        )
        return ExtractedItem(
            candidate_type=CandidateType.PROJECT,
            text=text,
            confidence=0.84,
            importance=score_importance(text, explicit_priority=8),
            attributes={"name": project_name, "description": project_description or project_name},
            reasoning=reasoning,
        )

    def _memory_item(
        self,
        text: str,
        confidence: float,
        importance: int,
        reasoning: str,
    ) -> ExtractedItem:
        return ExtractedItem(
            candidate_type=CandidateType.MEMORY,
            text=text,
            confidence=confidence,
            importance=importance,
            attributes={"memory_text": text},
            reasoning=reasoning,
        )

    def _extract_education_items(self, sentence: str) -> list[ExtractedItem]:
        graduated = re.search(
            r"\bi (?:recently\s+)?graduated from (?P<institution>.+?)"
            r"(?:\s+with\s+(?P<qualification>.+))?$",
            sentence,
            flags=re.IGNORECASE,
        )
        if not graduated:
            return []

        institution = self._normalize_education_name(graduated.group("institution"))
        qualification = (graduated.group("qualification") or "").strip(" .;,")
        degree, field = self._split_qualification(qualification)
        education_text = self._education_description(institution, degree, field)
        education = ExtractedItem(
            candidate_type=CandidateType.EDUCATION,
            text=education_text,
            confidence=0.94,
            importance=8,
            attributes={
                "institution": institution,
                "degree": degree,
                "field_of_study": field,
                "graduated": 1,
                "graduation_date": None,
            },
            reasoning="Detected an explicit completed education statement.",
        )
        event_text = f"Graduated from {institution}"
        event = ExtractedItem(
            candidate_type=CandidateType.EVENT,
            text=event_text,
            confidence=0.92,
            importance=8,
            attributes={
                "event": event_text,
                "description": education_text,
                "event_date": None,
            },
            reasoning="Detected the graduation timeline event from explicit user text.",
        )
        return [education, event]

    def _extract_identity_items(self, sentence: str) -> list[ExtractedItem]:
        location = re.search(
            r"\b(?:i(?:\s+currently)?\s+live in|"
            r"i(?:\s*am|'m|have been)\s+(?:currently\s+)?(?:based|located) in|"
            r"i(?:\s*am|'m) from|i moved to|my location is)\s+"
            r"(?P<value>[A-Za-z ,'-]{2,80})",
            sentence,
            flags=re.IGNORECASE,
        )
        if location:
            values = [
                self._normalize_place(part)
                for part in location.group("value").split(",")
                if part.strip()
            ]
            if not values:
                return []
            items = [self._identity_item("location", values[0])]
            if len(values) > 1:
                items.append(self._identity_item("country", values[-1]))
            return items

        item = self._extract_identity(sentence)
        return [item] if item is not None else []

    def _identity_item(self, key: str, value: str) -> ExtractedItem:
        normalized = normalize_identity_value(key, value)
        text = f"{key} = {normalized}"
        return ExtractedItem(
            candidate_type=CandidateType.IDENTITY,
            text=text,
            confidence=0.9,
            importance=score_importance(text),
            attributes={"key": key, "value": normalized},
            reasoning="Detected durable identity statement.",
        )

    def _normalize_place(self, value: str) -> str:
        cleaned = " ".join(value.strip(" .;,").split())
        if cleaned.islower():
            return " ".join(part.capitalize() for part in cleaned.split())
        return cleaned

    def _normalize_education_name(self, value: str) -> str:
        cleaned = " ".join(value.strip(" .;,").split())
        aliases = {
            "bits pilani": "BITS Pilani",
            "birla institute of technology and science pilani": "BITS Pilani",
        }
        return aliases.get(cleaned.casefold(), self._normalize_place(cleaned))

    def _split_qualification(self, value: str) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        match = re.match(
            r"(?P<degree>bachelors?'?s?|bachelor|masters?'?s?|master)"
            r"\s+of\s+(?P<kind>engineering|science|arts|technology)"
            r"(?:\s+in\s+(?P<field>.+))?$",
            value,
            flags=re.IGNORECASE,
        )
        if not match:
            return self._normalize_place(value), None
        level = "Bachelor" if match.group("degree").lower().startswith("bachelor") else "Master"
        kind = self._normalize_place(match.group("kind"))
        field = match.group("field")
        return (
            f"{level} of {kind}",
            self._normalize_place(field) if field else None,
        )

    def _education_description(
        self,
        institution: str,
        degree: str | None,
        field: str | None,
    ) -> str:
        qualification = degree or "Education"
        if field:
            qualification = f"{qualification} in {field}"
        return f"{qualification} at {institution}"

    def _extract_identity(self, sentence: str) -> ExtractedItem | None:
        patterns = [
            (
                r"\bmy name is (?P<value>[A-Z][A-Za-z .'-]{1,80}?)(?=\s*(?:,|;|\band\b|$))",
                "name",
            ),
            (
                r"^\s*(?:actually[,:\s]+)?(?:please\s+)?"
                r"call me\s+(?P<value>[A-Za-z][A-Za-z .'-]{1,80})\s*$",
                "name",
            ),
            (
                r"^\s*i go by\s+(?P<value>[A-Za-z][A-Za-z .'-]{1,80})\s*$",
                "name",
            ),
            (
                r"^\s*(?:i\s*am|i['’]m)\s+(?P<value>[A-Za-z][A-Za-z' -]{1,80})\s*$",
                "name",
            ),
            (r"\bmy age is (?P<value>\d{1,3})\b", "age"),
            (r"\bi am (?P<value>\d{1,3})\s+years? old\b", "age"),
            (r"\bi'?m (?P<value>\d{1,3})\s+years? old\b", "age"),
            (r"\bi turned (?P<value>\d{1,3})(?:\s+years? old)?\b", "age"),
            (r"\bmy occupation is (?P<occupation>[^.]{2,120})", "occupation"),
            (r"\bmy job is (?P<occupation>[^.]{2,120})", "occupation"),
            (r"\bi work as (?:a |an )?(?P<occupation>[^.]{2,120})", "occupation"),
            (
                r"\bi(?:\s*am|'m) (?:currently\s+)?(?:a |an )"
                r"(?P<occupation>[^.]{2,120})",
                "occupation",
            ),
            (
                r"\bi(?:\s+currently)?\s+live in (?P<value>[A-Za-z ,'-]{2,80})",
                "location",
            ),
            (
                r"\bi(?:\s*am|'m|have been)\s+(?:currently\s+)?"
                r"(?:based|located) in (?P<value>[A-Za-z ,'-]{2,80})",
                "location",
            ),
            (r"\bi(?:\s*am|'m) from (?P<value>[A-Za-z ,'-]{2,80})", "location"),
            (r"\bi moved to (?P<value>[A-Za-z ,'-]{2,80})", "location"),
            (r"\bmy location is (?P<value>[A-Za-z ,'-]{2,80})", "location"),
            (r"\bmy country is (?P<value>[A-Za-z ,'-]{2,80})", "country"),
            (r"\bmy nationality is (?P<value>[A-Za-z ,'-]{2,80})", "nationality"),
            (r"\bi study at (?P<value>[A-Za-z0-9 ,.'-]{2,120})", "education"),
            (r"\bi attend (?P<value>[A-Za-z0-9 ,.'-]{2,120})", "education"),
        ]
        for pattern, key in patterns:
            match = re.search(pattern, sentence, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.groupdict().get("occupation") or match.groupdict().get("value")
            if not value:
                continue
            value = re.sub(r"^(a|an)\s+", "", value.strip(), flags=re.IGNORECASE)
            value = re.split(r"\s+\band\b\s+", value, maxsplit=1, flags=re.IGNORECASE)[0]
            value = value.strip(" ,;")
            if not is_durable_identity_fact(key, value):
                continue
            value = normalize_identity_value(key, value)
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
        language_priority = re.search(
            r"\bi priorit(?:i|ise|ize|is)e?\s+(?:working|coding|programming)\s+with\s+"
            r"(?P<languages>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if language_priority:
            languages = self._ordered_languages(language_priority.group("languages"))
            if languages:
                return self._preference_item(
                    "programming_language_priority",
                    ", ".join(languages),
                    confidence=0.92,
                    reasoning="Detected an explicit ordered programming-language priority.",
                    canonical_slot="preference:programming_language_priority",
                )

        reverse_favorite = re.search(
            r"\b(?P<value>[A-Za-z][A-Za-z .'-]{1,100}?)\s+was\s+my\s+"
            r"fa(?:vour|vor|bour)ite\s+(?P<subject>[A-Za-z /+.-]{2,60})",
            sentence,
            flags=re.IGNORECASE,
        )
        if reverse_favorite:
            subject = reverse_favorite.group("subject").strip()
            value = self._display_name(reverse_favorite.group("value"))
            return self._preference_item(
                self._favorite_category(subject, value),
                value,
                confidence=0.9,
                reasoning="Detected a favorite preference despite a common spelling error.",
            )

        explicit_interest = re.search(
            r"\bi find (?P<value>[^.]{2,120}?)\s+(?:to be\s+)?interesting\b",
            sentence,
            flags=re.IGNORECASE,
        )
        if explicit_interest:
            value = explicit_interest.group("value").strip(" ,;")
            return self._preference_item(
                "interest",
                value,
                confidence=0.86,
                reasoning="Detected an explicit personal interest.",
                canonical_slot=f"preference:interest:{self._slug(value)}",
                additive=True,
            )

        typo_love = re.search(
            r"\bi\s+love+\s+(?P<value>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if typo_love:
            value = typo_love.group("value").strip(" ,;")
            if not re.fullmatch(
                r"(?:this|that|it|this answer|that answer|this response|that response)",
                value,
                flags=re.IGNORECASE,
            ):
                return self._preference_item(
                    "interest",
                    value,
                    confidence=0.84,
                    reasoning="Detected a positive personal interest despite a repeated letter.",
                    canonical_slot=f"preference:interest:{self._slug(value)}",
                    additive=True,
                )

        context_preference = re.search(
            r"\bfor (?P<context>[A-Za-z /+.-]{2,80}),?\s+(?:"
            r"i prefer (?P<direct_value>[^.]{2,120})|"
            r"my (?:preferred|favorite|go-to) (?P<subject>[A-Za-z /+.-]{2,60}?) is "
            r"(?P<subject_value>[^.]{2,120}))",
            sentence,
            flags=re.IGNORECASE,
        )
        if context_preference:
            context = context_preference.group("context").strip()
            raw_value = (
                context_preference.group("direct_value")
                or context_preference.group("subject_value")
                or ""
            )
            value = self._preferred_side(raw_value.strip(" ,;"))
            return self._preference_item(
                self._contextual_preference_category(context, value),
                value,
                confidence=0.82,
                reasoning="Detected context-specific user preference.",
            )

        favorite = re.search(
            r"\b(?:remember that\s+|actually,?\s+)?my favorite (?P<subject>[A-Za-z /+.-]{2,60}?) "
            r"is (?:now )?(?P<value>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not favorite:
            favorite = re.search(
                r"\b(?:remember that\s+|actually,?\s+)?my preferred "
                r"(?P<subject>[A-Za-z /+.-]{2,60}?) "
                r"is (?:now )?(?P<value>[^.]{2,120})",
                sentence,
                flags=re.IGNORECASE,
            )
        if not favorite:
            favorite = re.search(
                r"\b(?:remember that\s+|actually,?\s+)?my go-to (?P<subject>[A-Za-z /+.-]{2,60}?) "
                r"is (?:now )?(?P<value>[^.]{2,120})",
                sentence,
                flags=re.IGNORECASE,
            )
        if not favorite:
            favorite = re.search(
                r"\b(?P<value>[A-Za-z0-9+# .-]{2,80}?) is (?:now )?my favorite "
                r"(?P<subject>[A-Za-z /+.-]{2,60})",
                sentence,
                flags=re.IGNORECASE,
            )
        if not favorite:
            favorite = re.search(
                r"\bswitched my favorite (?P<subject>[A-Za-z /+.-]{2,60}?) "
                r"from (?P<old>[^.]{2,80}?) to (?P<value>[^.]{2,120})",
                sentence,
                flags=re.IGNORECASE,
            )
        if favorite:
            subject = favorite.group("subject").strip()
            value = favorite.group("value").strip(" ,;")
            return self._preference_item(
                self._favorite_category(subject, value),
                value,
                confidence=0.86,
                reasoning="Detected favorite preference statement.",
            )

        context_preference = re.search(
            r"\bfor (?P<context>[A-Za-z /+.-]{2,80}),?\s+i prefer (?P<value>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not context_preference:
            context_preference = re.search(
                r"\bfor (?P<context>[A-Za-z /+.-]{2,80}),?\s+my preferred "
                r"(?P<subject>[A-Za-z /+.-]{2,60}?) is (?P<value>[^.]{2,120})",
                sentence,
                flags=re.IGNORECASE,
            )
        if context_preference:
            context = context_preference.group("context").strip()
            value = self._preferred_side(context_preference.group("value").strip(" ,;"))
            return self._preference_item(
                self._contextual_preference_category(context, value),
                value,
                confidence=0.82,
                reasoning="Detected context-specific user preference.",
            )

        editor_use = re.search(
            r"\b(?:i (?:mainly|mostly|primarily) use (?P<used>[^.]{2,80})|"
            r"my (?:primary |main )?editor is (?P<my_editor>[^.]{2,80})|"
            r"(?P<primary>[^.]{2,80}?) is my primary editor|"
            r"i use (?P<as_editor>[^.]{2,80}?) as my (?:main|primary) editor|"
            r"i (?:code|work) (?:mostly |mainly |primarily )?in (?P<work_in>[^.]{2,80}))\b",
            sentence,
            flags=re.IGNORECASE,
        )
        if editor_use:
            value = (
                editor_use.group("used")
                or editor_use.group("my_editor")
                or editor_use.group("primary")
                or editor_use.group("as_editor")
                or editor_use.group("work_in")
                or ""
            ).strip(" ,;")
            if self._looks_like_editor(value):
                return self._preference_item(
                    "editor",
                    value,
                    confidence=0.82,
                    reasoning="Detected primary editor preference.",
                )

        prefer_over = re.search(
            r"\bi prefer (?P<preferred>[^.]{2,80}?) over (?P<other>[^.]{2,80})",
            sentence,
            flags=re.IGNORECASE,
        )
        if prefer_over:
            value = prefer_over.group("preferred").strip(" ,;")
            return self._preference_item(
                self._preference_category(value),
                value,
                confidence=0.8,
                reasoning="Detected comparative user preference.",
            )

        no_longer_like = re.search(
            r"\bi (?:do not|don't|no longer) like (?P<value>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if no_longer_like:
            value = no_longer_like.group("value").strip(" ,;")
            return self._preference_item(
                self._sentiment_category(value),
                f"dislike {value}",
                confidence=0.76,
                reasoning="Detected negated user sentiment.",
            )

        sentiment = re.search(
            r"\bi (?P<sentiment>love|like|hate|dislike) (?P<value>[^.]{2,120})",
            sentence,
            flags=re.IGNORECASE,
        )
        if sentiment:
            value = sentiment.group("value").strip(" ,;")
            if re.fullmatch(
                r"(?:this|that|it|this answer|that answer|this response|that response)",
                value,
                flags=re.IGNORECASE,
            ):
                return None
            sentiment_value = f"{sentiment.group('sentiment').lower()} {value}"
            return self._preference_item(
                self._sentiment_category(value),
                sentiment_value,
                confidence=0.76,
                reasoning="Detected positive or negative user sentiment.",
            )

        match = re.search(
            r"\bi prefer (?P<value>[^.]{3,160})|\bi like (?P<like>[^.]{3,160})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        value = (match.group("value") or match.group("like")).strip()
        category = self._preference_category(value)
        return self._preference_item(
            category,
            value,
            confidence=0.78,
            reasoning="Detected user preference.",
        )

    def _extract_goal(
        self,
        sentence: str,
        source_timestamp: datetime | None = None,
    ) -> ExtractedItem | None:
        match = re.search(
            r"\b(?:my (?:current |long[- ]term )?goal is to|"
            r"i (?:plan|intend|aim|hope) to|i am working toward|i want to)\s+"
            r"(?P<goal>[^.]{3,180})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        goal = match.group("goal").strip(" ,;")
        if sentence.lower().find("i want to") >= 0 and not self._durable_want_goal(goal):
            return None
        horizon_months = self._goal_horizon_months(goal)
        goal = re.sub(
            r"\s+in\s+\d{1,3}\s+months?\s*$",
            "",
            goal,
            flags=re.IGNORECASE,
        ).strip()
        explicit_priority = 10 if "highest" in sentence.lower() else None
        priority = score_importance(goal, explicit_priority=explicit_priority)
        attributes: dict[str, str | int | float | None] = {
            "goal": goal,
            "priority": priority,
            "horizon_months": horizon_months,
        }
        if horizon_months:
            anchor = self._source_datetime(source_timestamp)
            attributes["target_date"] = self._add_months(anchor.date(), horizon_months).isoformat()
        return ExtractedItem(
            candidate_type=CandidateType.GOAL,
            text=goal,
            confidence=0.8,
            importance=priority,
            attributes=attributes,
            reasoning="Detected active or intended user goal.",
        )

    def _extract_activity(
        self,
        sentence: str,
        source_timestamp: datetime | None = None,
    ) -> ExtractedItem | None:
        match = re.search(
            r"\bi(?:\s+am|'m)\s+(?:currently\s+)?"
            r"(?P<verb>playing|reading|watching|learning)\s+"
            r"(?P<subject>[^.]{2,140})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        verb = match.group("verb").lower()
        subject = self._display_name(match.group("subject"))
        category = {
            "playing": "game",
            "reading": "book",
            "watching": "media",
            "learning": "learning",
        }[verb]
        started_at = self._source_datetime(source_timestamp)
        expires_at = started_at + timedelta(days=30)
        activity = f"{verb} {subject}"
        return ExtractedItem(
            candidate_type=CandidateType.ACTIVITY,
            text=activity,
            confidence=0.9,
            importance=5,
            attributes={
                "category": category,
                "activity": activity,
                "subject": subject,
                "started_at": started_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            reasoning="Detected an explicit time-bounded current activity.",
        )

    def _extract_project(self, sentence: str) -> ExtractedItem | None:
        main_project = re.search(
            r"\bmy (?:main |primary |current )?project is (?P<name>[A-Z][A-Za-z0-9 _-]{1,80})"
            r"(?:,?\s+(?P<description>[^.]{3,220}))?",
            sentence,
            flags=re.IGNORECASE,
        )
        if main_project:
            name = main_project.group("name").strip(" ,;")
            description = (main_project.group("description") or sentence).strip(" ,;")
            return self._project_item(name, description, "Detected main project statement.")

        owned_project_update = re.search(
            r"\bmy project (?P<name>[A-Z][A-Za-z0-9_-]{1,60})\s+"
            r"(?P<description>[^.]{3,220})",
            sentence,
            flags=re.IGNORECASE,
        )
        if owned_project_update:
            name = owned_project_update.group("name").strip(" ,;")
            description = owned_project_update.group("description").strip(" ,;")
            return self._project_item(name, description, "Detected owned project update.")

        versioned_project_update = re.search(
            r"\b(?P<name>[A-Z][A-Za-z0-9_-]{1,60})\s+v(?P<version>\d+(?:\.\d+)?)\s+"
            r"(?P<description>[^.]{3,220})",
            sentence,
        )
        if versioned_project_update:
            name = versioned_project_update.group("name").strip(" ,;")
            description = (
                f"v{versioned_project_update.group('version')} "
                f"{versioned_project_update.group('description').strip(' ,;')}"
            )
            return self._project_item(name, description, "Detected versioned project update.")

        match = re.search(
            r"\b(?:project|building|working on)\s+"
            r"(?P<name>[A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*){0,3})(?=\s*(?:,|$))",
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

    def _extract_hardware(self, sentence: str) -> ExtractedItem | None:
        if "?" in sentence or re.match(
            r"^\s*(?:do|does|did|can|could|should|would|what|which|where|when|why|how)\b",
            sentence,
            flags=re.IGNORECASE,
        ):
            return None
        hardware_terms = (
            r"\b("
            r"dell|inspiron|laptop|computer|pc|machine|ram|ssd|hdd|processor|cpu|gpu|"
            r"graphics?|graphic card|integrated graphics?|nvidia|rtx|intel|i3|i5|i7|i9|ryzen"
            r")\b"
        )
        if not re.search(hardware_terms, sentence, re.IGNORECASE):
            return None

        match = re.search(
            r"\b(?:i currently have|i have|my (?:laptop|computer|pc|machine) specs(?: are|:)?|"
            r"my (?:current )?(?:hardware setup|laptop|computer|pc|machine)"
            r"(?: is| has|:)?)\s+(?P<value>[^.]{8,240})",
            sentence,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        value = match.group("value").strip(" ,;")
        if re.search(r"\b(drinking|eating|wearing|watching|listening)\b", value, re.IGNORECASE):
            return None
        return self._hardware_item(value)

    def _hardware_item(self, value: str) -> ExtractedItem:
        normalized = self._normalize_hardware(value)
        return self._memory_item(
            f"Current hardware: {normalized}",
            confidence=0.84,
            importance=6,
            reasoning="Detected durable current hardware setup.",
        )

    def _normalize_hardware(self, value: str) -> str:
        cleaned = " ".join(value.strip(" .,\t\r\n").split())
        cleaned = re.sub(r"^(?:a|an)\s+", "", cleaned, flags=re.IGNORECASE)
        replacements = {
            r"\b16\s*gb\s+ram\b": "16GB RAM",
            r"\b512\s*gb\s+ssd\b": "512GB SSD",
            r"\bi7\s+11th\s+gen\b": "Intel i7 11th gen",
            r"\bintegrated graphics? card\b": "integrated graphics",
        }
        for pattern, replacement in replacements.items():
            cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bdell\b", "Dell", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\binspiron\b", "Inspiron", cleaned, flags=re.IGNORECASE)
        return cleaned

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
            CandidateType.EDUCATION: result.education,
            CandidateType.PREFERENCE: result.preferences,
            CandidateType.GOAL: result.goals,
            CandidateType.PROJECT: result.projects,
            CandidateType.ACTIVITY: result.activities,
            CandidateType.EVENT: result.events,
            CandidateType.MEMORY: result.memories,
        }[item.candidate_type]
        target.append(item)

    def _append_unique(self, result: ExtractionResult, item: ExtractedItem) -> None:
        normalized_text = " ".join(item.text.lower().split())
        for existing in result.items:
            if existing.candidate_type == item.candidate_type:
                existing_text = " ".join(existing.text.lower().split())
                if existing_text == normalized_text:
                    return
        self._append(result, item)

    def _stamp_source_context(self, result: ExtractionResult, request: ExtractionRequest) -> None:
        for item in result.items:
            item.attributes.setdefault("source_sentence", self._source_sentence(item, request))
            if request.source_conversation_id is not None:
                item.attributes.setdefault("source_conversation_id", request.source_conversation_id)
            if request.source_message_id is not None:
                item.attributes.setdefault("source_message_id", request.source_message_id)
            if request.source_timestamp is not None:
                item.attributes.setdefault(
                    "source_timestamp",
                    self._source_datetime(request.source_timestamp).isoformat(),
                )
            if not item.attributes.get("canonical_slot"):
                item.attributes["canonical_slot"] = self._canonical_slot(item)

    def _source_sentence(self, item: ExtractedItem, request: ExtractionRequest) -> str:
        text = self._request_text(request)
        for sentence in self._sentences(text):
            if item.text.lower() in sentence.lower() or self._source_sentence_matches_item(
                sentence, item
            ):
                return sentence
        return text.strip() or item.text

    def _source_sentence_matches_item(self, sentence: str, item: ExtractedItem) -> bool:
        for value in item.attributes.values():
            if isinstance(value, str) and value and value.lower() in sentence.lower():
                return True
        return False

    def _canonical_slot(self, item: ExtractedItem) -> str | None:
        explicit = item.attributes.get("canonical_slot")
        if explicit:
            return str(explicit)
        if item.candidate_type == CandidateType.IDENTITY:
            key = item.attributes.get("key")
            return f"identity:{key}" if key else "identity"
        if item.candidate_type == CandidateType.PREFERENCE:
            category = item.attributes.get("category")
            return f"preference:{category}" if category else "preference"
        if item.candidate_type == CandidateType.EDUCATION:
            institution = item.attributes.get("institution")
            return f"education:{self._slug(str(institution))}"
        if item.candidate_type == CandidateType.GOAL:
            return f"goal:{self._slug(str(item.attributes.get('goal') or item.text))}"
        if item.candidate_type == CandidateType.PROJECT:
            name = item.attributes.get("name")
            return f"project:{str(name).lower()}" if name else "project"
        if item.candidate_type == CandidateType.ACTIVITY:
            category = item.attributes.get("category")
            return f"activity:{category}" if category else "activity"
        if item.candidate_type == CandidateType.EVENT:
            return f"event:{self._slug(str(item.attributes.get('event') or item.text))}"
        if item.text.lower().startswith("current hardware:"):
            return "current_hardware"
        return item.candidate_type.value

    def _result_from_llm_response(self, response: str) -> ExtractionResult:
        payload = self._json_payload(response)
        data = json.loads(payload)
        raw_items = data if isinstance(data, list) else data.get("items", [])
        result = ExtractionResult()
        if not isinstance(raw_items, list):
            return result

        for raw_item in raw_items:
            item = self._item_from_llm_dict(raw_item)
            if item is not None:
                self._append_unique(result, item)
        return result

    def _item_from_llm_dict(self, raw_item: Any) -> ExtractedItem | None:
        if not isinstance(raw_item, dict):
            return None
        try:
            candidate_type = CandidateType(str(raw_item.get("type", "")).strip().lower())
        except ValueError:
            return None
        if candidate_type == CandidateType.NONE:
            return None

        attributes = raw_item.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        text = str(
            raw_item.get("text")
            or attributes.get("memory_text")
            or attributes.get("value")
            or attributes.get("goal")
            or attributes.get("name")
            or attributes.get("event")
            or ""
        ).strip()
        if not text:
            return None

        confidence = self._bounded_float(raw_item.get("confidence"), default=0.78)
        importance = self._bounded_int(raw_item.get("importance"), default=score_importance(text))
        return ExtractedItem(
            candidate_type=candidate_type,
            text=text,
            confidence=confidence,
            importance=importance,
            attributes=attributes,
            reasoning=str(raw_item.get("reasoning") or "LLM extracted durable memory."),
        )

    def _llm_item_is_user_grounded(self, item: ExtractedItem, user_text: str) -> bool:
        normalized_source = self._grounding_text(user_text)
        values = [
            value
            for key, value in item.attributes.items()
            if key
            not in {
                "priority",
                "horizon_months",
                "target_date",
                "event_date",
                "graduation_date",
                "confidence",
            }
            and isinstance(value, str)
            and len(value.strip()) >= 3
        ]
        values.append(item.text)
        return any(
            self._grounding_text(value) in normalized_source
            for value in values
            if self._grounding_text(value)
        )

    def _valid_model_item(self, item: ExtractedItem, user_text: str) -> bool:
        """Validate model-only candidates before exposing them for human review."""

        if not self._llm_item_is_user_grounded(item, user_text):
            return False
        attributes = item.attributes
        if item.candidate_type == CandidateType.IDENTITY:
            key = str(attributes.get("key") or "").strip().lower()
            value = str(attributes.get("value") or "").strip()
            return is_durable_identity_fact(key, value)
        if item.candidate_type == CandidateType.EDUCATION:
            return self._grounded_required_value(attributes, "institution", user_text)
        if item.candidate_type == CandidateType.PREFERENCE:
            return self._grounded_required_value(attributes, "value", user_text)
        if item.candidate_type == CandidateType.GOAL:
            return self._grounded_required_value(attributes, "goal", user_text)
        if item.candidate_type == CandidateType.PROJECT:
            return self._grounded_required_value(attributes, "name", user_text)
        if item.candidate_type == CandidateType.ACTIVITY:
            category = str(attributes.get("category") or "").strip().lower()
            return category in {"playing", "reading", "watching", "working_on"} and (
                self._grounded_required_value(attributes, "subject", user_text)
                or self._grounded_required_value(attributes, "activity", user_text)
            )
        if item.candidate_type == CandidateType.EVENT:
            return self._grounded_required_value(attributes, "event", user_text)
        if item.candidate_type == CandidateType.MEMORY:
            value = attributes.get("memory_text") or item.text
            return self._grounding_text(str(value)) in self._grounding_text(user_text)
        return False

    def _grounded_required_value(
        self,
        attributes: dict[str, str | int | float | None],
        key: str,
        user_text: str,
    ) -> bool:
        value = attributes.get(key)
        if not isinstance(value, str) or len(value.strip()) < 2:
            return False
        return self._grounding_text(value) in self._grounding_text(user_text)

    def _json_payload(self, response: str) -> str:
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        if cleaned.startswith("{") or cleaned.startswith("["):
            return cleaned
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.DOTALL)
        if not match:
            raise json.JSONDecodeError("No JSON object found", cleaned, 0)
        return match.group(1)

    def _bounded_float(self, value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0.0, min(1.0, parsed))

    def _bounded_int(self, value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(10, parsed))

    def _preference_item(
        self,
        category: str,
        value: str,
        confidence: float,
        reasoning: str,
        canonical_slot: str | None = None,
        additive: bool = False,
    ) -> ExtractedItem:
        normalized_value = self._normalize_preference_value(value)
        text = f"{category} = {normalized_value}"
        attributes: dict[str, str | int | float | None] = {
            "category": category,
            "value": normalized_value,
            "additive": int(additive),
        }
        if canonical_slot:
            attributes["canonical_slot"] = canonical_slot
        return ExtractedItem(
            candidate_type=CandidateType.PREFERENCE,
            text=text,
            confidence=confidence,
            importance=score_importance(text),
            attributes=attributes,
            reasoning=reasoning,
        )

    def _favorite_category(self, subject: str, value: str) -> str:
        normalized_subject = subject.lower()
        if "programming" in normalized_subject or "language" in normalized_subject:
            return "favorite_programming_language"
        if "editor" in normalized_subject or "ide" in normalized_subject:
            return "editor"
        if "framework" in normalized_subject:
            return "favorite_framework"
        if "database" in normalized_subject:
            return "favorite_database"
        return f"favorite_{self._slug(normalized_subject)}"

    def _contextual_preference_category(self, context: str, value: str) -> str:
        context_slug = self._context_slug(context)
        if self._looks_like_programming_language(value):
            return f"{context_slug}_language"
        if self._looks_like_editor(value):
            return f"{context_slug}_editor"
        return f"{context_slug}_preference"

    def _sentiment_category(self, value: str) -> str:
        normalized_value = self._normalize_preference_value(value)
        return f"sentiment_{self._slug(normalized_value)}"

    def _preference_category(self, value: str) -> str:
        normalized = value.lower()
        if "explanation" in normalized or "answer" in normalized:
            return "response_style"
        if self._looks_like_editor(value):
            return "editor"
        if self._looks_like_programming_language(value):
            return "programming_language"
        return "general"

    def _normalize_preference_value(self, value: str) -> str:
        cleaned = " ".join(value.strip(" .,\t\r\n").split())
        cleaned = re.sub(r"\s+(?:anymore|these days|now)$", "", cleaned, flags=re.IGNORECASE)
        replacements = {
            r"\bvisual studio code\b": "VS Code",
            r"\bvscode\b": "VS Code",
            r"\bvs code\b": "VS Code",
            r"\bc\+\+\b": "C++",
            r"\btypescript\b": "TypeScript",
            r"\bjavascript\b": "JavaScript",
            r"\bpython\b": "Python",
            r"\brust\b": "Rust",
            r"\bjava\b": "Java",
            r"\bgo\b": "Go",
        }
        for pattern, replacement in replacements.items():
            cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
        return cleaned

    def _ordered_languages(self, value: str) -> list[str]:
        raw_values = re.split(r"\s*(?:,|/|\band\b|&)\s*", value, flags=re.IGNORECASE)
        aliases = {
            "c": "C",
            "c++": "C++",
            "cpp": "C++",
            "python": "Python",
            "javascript": "JavaScript",
            "typescript": "TypeScript",
            "rust": "Rust",
            "java": "Java",
            "go": "Go",
            "sql": "SQL",
        }
        languages: list[str] = []
        for raw_value in raw_values:
            normalized = aliases.get(raw_value.strip(" .;").casefold())
            if normalized and normalized not in languages:
                languages.append(normalized)
        return languages

    def _durable_want_goal(self, goal: str) -> bool:
        normalized = goal.strip().lower()
        if re.match(
            r"(?:know|search|find|check|see|ask|tell|show|open|try|look up|"
            r"get (?:the )?(?:latest|current)|buy|order|watch|read)\b",
            normalized,
        ):
            return False
        return bool(
            re.match(
                r"(?:master|become|build|create|launch|finish|complete|achieve|"
                r"improve|learn|study|practice|get into|join|move|graduate|earn|"
                r"transition|switch|prepare|qualify|reach|save)\b",
                normalized,
            )
            or re.search(r"\bin\s+\d{1,3}\s+months?\s*$", normalized)
        )

    def _goal_horizon_months(self, goal: str) -> int | None:
        match = re.search(r"\bin\s+(?P<months>\d{1,3})\s+months?\s*$", goal, re.IGNORECASE)
        if not match:
            return None
        months = int(match.group("months"))
        return months if 1 <= months <= 120 else None

    def _source_datetime(self, value: datetime | None = None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            return resolved.replace(tzinfo=UTC)
        return resolved.astimezone(UTC)

    def _add_months(self, value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, monthrange(year, month)[1])
        return date(year, month, day)

    def _display_name(self, value: str) -> str:
        cleaned = " ".join(value.strip(" .;,").split())
        if not cleaned.islower():
            return cleaned
        small_words = {"a", "an", "and", "at", "for", "in", "of", "on", "the", "to"}
        words = cleaned.split()
        return " ".join(
            word if index > 0 and word in small_words else word.capitalize()
            for index, word in enumerate(words)
        )

    def _grounding_text(self, value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9+#]+", value.casefold()))

    def _preferred_side(self, value: str) -> str:
        match = re.match(r"(?P<preferred>.+?)\s+over\s+.+", value, flags=re.IGNORECASE)
        if match:
            return match.group("preferred").strip(" ,;")
        return value

    def _looks_like_programming_language(self, value: str) -> bool:
        normalized = value.lower()
        return bool(
            re.search(
                r"\b(python|typescript|javascript|rust|java|go|c\+\+|cpp|c#|c|sql)\b",
                normalized,
            )
        )

    def _looks_like_editor(self, value: str) -> bool:
        normalized = value.lower()
        return bool(
            re.search(
                r"\b(vs\s*code|vscode|visual studio code|neovim|vim|pycharm|intellij|webstorm)\b",
                normalized,
            )
        )

    def _slug(self, value: str) -> str:
        return "_".join(re.findall(r"[a-z0-9+#]+", value.lower())) or "general"

    def _context_slug(self, value: str) -> str:
        slug = self._slug(value)
        aliases = {
            "cp": "competitive_programming",
            "competitive_programming": "competitive_programming",
            "web_dev": "web_development",
            "web_development": "web_development",
            "backend": "backend_development",
            "backend_development": "backend_development",
        }
        return aliases.get(slug, slug)

    def _extract_iso_date(self, text: str) -> date | None:
        match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
        if not match:
            return None
        return date.fromisoformat(match.group(0))
