from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import Reflection
from app.models.enums import CandidateType
from app.repositories.memory_store import MemoryStore
from app.services.extraction import ExtractedItem, ExtractionResult
from app.services.scoring import score_importance


class ReflectionRunRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=30)
    generate_candidates: bool = True


class ReflectionRunResult(BaseModel):
    reflection_id: int
    reflection: str
    candidate_ids: list[int] = Field(default_factory=list)


class ReflectionService:
    """Generate high-level observations from current structured memory."""

    def run(self, store: MemoryStore, request: ReflectionRunRequest) -> ReflectionRunResult:
        goals = store.list_goals()
        projects = store.list_projects()
        memories = store.list_memories(limit=20)

        active_goal = goals[0].goal if goals else "No active goal recorded"
        active_project = projects[0].name if projects else "No active project recorded"
        focus_terms = self._focus_terms([memory.memory_text for memory in memories])

        reflection_text = (
            f"Current focus appears to be '{active_goal}', with '{active_project}' as a key "
            f"project. Repeated themes: {', '.join(focus_terms) if focus_terms else 'none yet'}."
        )
        reflection = store.add(
            Reflection(
                reflection=reflection_text,
                importance=score_importance(reflection_text),
            )
        )

        candidate_ids: list[int] = []
        if request.generate_candidates:
            from app.services.extraction import MemoryExtractionService

            extraction = ExtractionResult(
                memories=[
                    ExtractedItem(
                        candidate_type=CandidateType.MEMORY,
                        text=reflection_text,
                        confidence=0.6,
                        importance=reflection.importance,
                        attributes={"memory_text": reflection_text},
                        reasoning="Generated from reflection over current memory state.",
                    )
                ]
            )
            candidates = MemoryExtractionService().persist_candidates(store, extraction)
            candidate_ids = [candidate.id for candidate in candidates]

        return ReflectionRunResult(
            reflection_id=reflection.id,
            reflection=reflection.reflection,
            candidate_ids=candidate_ids,
        )

    def _focus_terms(self, texts: list[str]) -> list[str]:
        counts: dict[str, int] = {}
        stopwords = {"the", "and", "that", "with", "user", "this", "from", "memory"}
        for text in texts:
            for word in text.lower().replace("=", " ").split():
                cleaned = "".join(char for char in word if char.isalnum())
                if len(cleaned) < 4 or cleaned in stopwords:
                    continue
                counts[cleaned] = counts.get(cleaned, 0) + 1
        sorted_terms = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return [word for word, _ in sorted_terms[:5]]
