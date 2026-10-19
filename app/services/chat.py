from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from threading import Thread
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import build_engine
from app.models import ChatGeneration, ChatMessage
from app.repositories.app_store import AppStore
from app.services.agent_core.guidance import agent_run_guidance
from app.services.calendar.intent import is_social_pleasantry
from app.services.calendar.service import (
    CalendarContextService,
    CalendarTurnExecution,
    describe_calendar_draft,
)
from app.services.chat_intent import resolve_internal_chat_intent
from app.services.code_index.service import CodeIndexService
from app.services.context import ContextPackage
from app.services.current_time import today_for_prompt
from app.services.files.service import WorkspaceFilesService
from app.services.gallery.chat_lookup import GalleryMatches, wants_gallery
from app.services.gallery.chat_lookup import lookup as gallery_lookup
from app.services.git.service import GitContextService
from app.services.llm import ChatTurn, LLMChatResult, LLMClient, LLMMessage
from app.services.memory.contracts import SourceKind
from app.services.memory.direct_answer import DirectMemoryAnswerService
from app.services.memory.extraction_contracts import (
    EXTRACTION_WINDOW_MAX_CHARS,
    ConversationRole,
    CurrentTurnOverride,
    ExtractionMode,
    ExtractionRequest,
    TrustedConversationMessage,
)
from app.services.memory.factory import MemoryRuntime
from app.services.memory.policy import turn_may_contain_memory
from app.services.memory.runtime import drain_memory_outbox
from app.services.memory_chat import (
    STABLE_MEMORY_POLICY,
    MemoryQueryContext,
    RecallPromptOrchestrator,
    RecallPromptSelection,
    RecallQuery,
)
from app.services.pending_action import (
    PROPOSAL_REASK_KIND,
    PendingCalendarProposal,
    calendar_proposal_status,
    find_pending_calendar_proposal,
    resolve_pending_action_reply,
)
from app.services.projects import ProjectContextService
from app.services.rules.resolver import RuleResolver
from app.services.search.citations import validate_citation_markers
from app.services.search.content import FactResult, extract_release_date, run_extractors
from app.services.search.intent import (
    SearchIntentResolver,
    is_bare_search_command,
    resolve_search_intent,
)
from app.services.search.live_data import (
    FrankfurterClient,
    LiveDataError,
    OpenMeteoClient,
    local_datetime_answer,
)
from app.services.search.types import ResolvedSearchIntent, SearchIntentKind, SearchOptions
from app.services.source_citations import CitationFormatter
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.tasks import TaskContextService
from app.services.test_runner.service import TestRunnerContextService
from app.services.web_search import (
    WebContext,
    WebSearchService,
)

_ROUTING_LOG = logging.getLogger("neo.chat.routing")
_MEMORY_SAVE_OUTCOMES = frozenset(
    {"created", "reconfirmed", "refined", "replaced", "merged", "restored"}
)
_MEMORY_FORGET_OUTCOMES = frozenset({"archived", "forgotten", "erased_permanently"})
# Replayed in place of a turn that removed a memory.  Deleting a memory has to
# survive the rest of the conversation: the forget request and its confirmation
# both spell out the value that was just removed, so replaying them verbatim let
# the model read the deleted fact straight off the transcript and repeat it back
# while insisting it had forgotten it.
_FORGOTTEN_TURN_PLACEHOLDER = "[A memory was removed in this turn. Its content is unavailable.]"

#: A calendar proposal that was never carried out is an *expired offer*, not a
#: record that anything was written. Replayed verbatim it supplies both the
#: event details and an open "Want me to create it?" question sitting directly
#: above the user's next reply -- which is how "I've added the dentist
#: appointment to your calendar" got produced on a turn that executed nothing.
#: Annotating the text was tried first and was not enough: the model kept
#: completing the offer/answer pattern from the details still in front of it.
#: So the expired offer is replaced the same way a forgotten memory is, for
#: the same reason -- the fact that an offer happened survives, the material
#: that lets it be restated as an accomplished change does not. A proposal
#: that *was* carried out is left untouched: the mutation result that follows
#: it is the truthful record, and blanking the offer would contradict it.
_EXPIRED_CALENDAR_OFFER_PLACEHOLDER = (
    "[Earlier I offered to make a calendar change and asked the user to "
    "confirm. That offer was never confirmed, so no calendar change was made "
    "and the offer is no longer open.]"
)

#: The other half of the same rule. An approval is a click on the proposal
#: card, which stamps the proposal message itself and writes nothing to the
#: transcript -- so without this the model reads an offer with no answer
#: after it and is told, by the placeholder above, that nothing happened.
#: That was false for every event the user actually approved.
_APPROVED_CALENDAR_OFFER_PLACEHOLDER = (
    "[Earlier I offered to make a calendar change and the user approved it. "
    "The change was made and the offer is no longer open.]"
)

_CALENDAR_EXECUTION_STATEMENT: dict[str, str] = {
    "none": ("No calendar event was created, updated, or deleted during this turn."),
    "create": "A calendar event was created during this turn.",
    "update": "A calendar event was updated during this turn.",
    "delete": "A calendar event was deleted during this turn.",
    "failed": ("A calendar change was attempted during this turn and did not succeed."),
}

#: The whole answer for a turn whose authoritative execution state is
#: ``"failed"``. A calendar change was asked for, the application refused to
#: complete it, and the request itself is the only thing left in front of the
#: model -- which is exactly the material free-text generation completes into
#: "I've scheduled that for you". So generation is not given the turn at all;
#: the application states what it did, which it alone knows. Deliberately
#: says nothing about *which* event or *what* was wrong: the reasons are
#: several (no such event, unparseable time, no date at all) and guessing
#: between them would be inventing detail again.
_CALENDAR_NOT_COMPLETED_REPLY = (
    "I couldn't work out that calendar change, so I didn't make one -- nothing "
    "was added, changed, or removed. Tell me the event and the exact day and "
    "time you want and I'll set it up."
)

#: Recorded under the kind used for a change that was asked for and not
#: made, so this reply reads back through history, the API and the UI exactly
#: like every other unperformed calendar change -- and so it is never mistaken
#: for an executed one by ``_calendar_offer_outcomes``, which counts only a
#: proposal stamped ``status="approved"`` by its card.
_CALENDAR_NOT_COMPLETED_METADATA: dict[str, Any] = {
    "response_kind": "calendar_mutation_failed",
    "metadata": {"calendar_mutation": {"completed": False}},
}


#: Who Neo is and how it must handle not knowing. Every turn carries this.
_PROMPT_CORE = (
    "You are Neo, a local personal AI assistant. Use the provided memory context "
    "when it is relevant. Do not claim memories that are not present. If memory "
    "context conflicts, prefer active goals, active projects, current profile facts, "
    "and current preferences. For personal questions about the user's name, age, "
    "location, preferences, goals, or projects, answer only from memory context or "
    "conversation history. "
    "Answer the user's actual request directly. Give recommendations, explanations, "
    "comparisons, coding help, writing and opinions from your own knowledge; they do "
    "not need a lookup. State facts you know plainly. If a specific fact is genuinely "
    "unknown to you, say so in one sentence instead of guessing, and still give "
    "whatever help you can. Never reply with only an offer to search. Never say or "
    "imply that you searched, checked or verified anything unless web context below "
    "shows that a search ran. If the user asks for a list of things, name the things "
    "and briefly explain each choice. Number ranked lists; do not replace them with "
    "generic categories or advice. Use simple words unless the user asks for detail. "
)

#: How Low answers. Low is the default: fast, useful, and short by design.
_PROMPT_LOW = (
    "Reply concisely: lead with the answer, then only the details that matter. "
    "For an ordinary explanation, use two to four short sentences and everyday "
    "words. Do not write a step-by-step technical breakdown unless the user asks "
    "for one. If the user asks for a number of items, give the items even when "
    "that takes more space. If the "
    "answer depends on information that may have changed since your training and no "
    "web search has run, give your best answer, say in one short clause that it "
    "may be out of date, and offer to search the web. When a web search has run, "
    "add no such disclaimer and do not offer to search again. "
)

#: How High answers. It reasons first; this is about what the answer contains.
_PROMPT_HIGH = (
    "Give a complete, well-organized answer suited to the task. Work through difficult "
    "problems carefully and state key assumptions, trade-offs and edge cases when they "
    "matter. Keep simple answers short; do not pad. If the answer depends on "
    "information that may have changed since your training and no web search has "
    "run, say which parts may be out of date and offer to search the web. When a "
    "web search has run, add no such disclaimer and do not offer to search again. "
)

#: Used only when a search ran and returned nothing usable: the answer must say
#: so first, then help from general knowledge without presenting anything
#: time-sensitive as current.
_PROMPT_WEB_UNAVAILABLE = (
    "A web search ran for this message and returned no usable sources. Start with "
    "one short sentence saying you could not check current information online. Then "
    "still answer as usefully as you can from general knowledge: if the user asked "
    "for recommendations or a list, name specific items with a short reason for "
    "each; if they asked for a current fact, give the most recent answer you know "
    "and say it may have changed since. Do not just tell the user to look it up "
    "elsewhere. Do not present what you know as confirmed or current, and do not "
    "cite sources. "
)
#: Used whenever web search is switched off. Nothing can be checked online, so
#: the answer must not pretend it was, or offer a search that cannot run.
_PROMPT_WEB_DISABLED = (
    "Web search is turned off. If the answer depends on current information, say in "
    "one short clause that you could not check it online, then answer as usefully "
    "as you can from general knowledge. Do not offer to search the web, and do not "
    "claim you searched or verified anything. "
)

#: How to use and cite retrieved evidence. Meaningless on a turn that
#: retrieved none, and the largest block here -- so a low-effort turn without
#: web context leaves it out rather than re-reading it at 188 tok/s.
_PROMPT_WEB = (
    "Memory context and web context are separate. Use web context only for current, "
    "recent, or explicitly searched information. When web context is provided, cite "
    "web-grounded claims using bracket markers like [1]. Do not place raw URLs "
    "inline in your answer text; citations go in the Sources block appended after "
    "your answer. For web-grounded prompts, do not use memory, conversation history, "
    "or general knowledge to fill gaps in the retrieved web evidence. The web context "
    "contains extracted evidence only; do not infer beyond it. "
    "The web evidence is newer than your training data: when it disagrees with what you "
    "remember, follow the evidence and do not mention the older version as current. "
    "For time-sensitive facts, answer only from the web evidence. If the web evidence "
    "supports only part of a multi-item request, give the supported items and say "
    "how many you could verify; do not invent the rest. If it does not contain "
    "the answer at all, say only: I searched the web but could not find "
    "sufficiently reliable current sources. Do NOT add general knowledge or filler "
    "after that statement. Do NOT answer from your training data for time-sensitive "
    "questions. "
    "If the results describe different things that share a name, say so and group the "
    "results by entity. Do not merge unrelated entities into one answer. "
    "When the evidence covers several variants of what was asked, say which variant "
    "each figure applies to. "
    "For a list request, each numbered entry must name one specific item the evidence "
    "supports, with a short reason in everyday words and a citation. Never number "
    "categories, source names or headings as if they were items. "
    "Do NOT generate a Sources or References block yourself. The backend will "
    "append verified sources automatically. Do NOT invent URLs or cite pages that "
    "were not provided in the web context. "
    "Answer the user's question directly first, then provide brief supporting "
    "evidence. Do not output raw search-result titles or snippet labels. "
    "The web search for this message has already run: do not offer to search the web, "
    "and do not call the sources possibly out of date unless their own dates show it. "
)

#: How to treat project and task context. Left out when the turn carries
#: neither, on the same reasoning as the web block.
_PROMPT_WORKSPACE = (
    "Project context is a user-owned workspace layer separate from Memory. Use "
    "project context only when it is provided and relevant. Never write project "
    "details to memory automatically. Task context is also a user-owned workspace "
    "layer. Use it only when relevant, treat it as read-only, and never write task "
    "details to Memory automatically. "
)

#: How to answer from the gallery. Only present on a turn that searched it,
#: which is the only turn where the model has anything to say about images it
#: holds -- and without it the model, told elsewhere to answer personal
#: questions only from what it was given, says it keeps no images at all.
_PROMPT_GALLERY = (
    "Neo keeps a gallery of every image the user has shared or added. Gallery "
    "context below lists the images from it that answer this message, already "
    "searched by the application. Those images are displayed to the user "
    "directly beneath your reply, in the order listed, so do not output image "
    "links, markdown images, file paths, or ids. Refer to an image by its title "
    "and describe it only from the description given. If the gallery context "
    "says nothing matched, say so plainly. Never say that you cannot store, keep, "
    "or show images. "
)

#: What Neo may claim to have *done*. Never omitted: generation always sees
#: `calendar_execution == "none"` (every path that changes the calendar answers
#: its own turn), so this is the only thing standing between the model and a
#: confident lie about having saved or scheduled something.
_PROMPT_AUTHORITY = (
    "Memory persistence is controlled exclusively "
    "by the backend before this answer is generated. Never claim that you saved, "
    "updated, stored, noted, remembered, removed, deleted, or forgot user information. "
    "If no deterministic memory-status response was returned before reaching you, "
    "then no user-visible memory mutation is confirmed. "
    "Calendar changes are controlled by the backend the same way. The calendar "
    "state below is supplied by the application and is the only authority on "
    "what happened this turn. Never say you created, updated, deleted, "
    "scheduled, moved, or cancelled a calendar event unless that state says one "
    "happened. An earlier offer to change the calendar is not evidence that the "
    "change was carried out.\n\n"
)


class NeoChatService:
    """Connects memory context, Ollama generation, archiving, and extraction."""

    def __init__(
        self,
        db: Session,
        ollama: LLMClient | None = None,
        rule_result: dict[str, Any] | None = None,
        memory_orchestrator: RecallPromptOrchestrator | None = None,
        memory_context_factory: Callable[[str], MemoryQueryContext | None] | None = None,
        memory_enabled: bool | None = None,
        memory_runtime: MemoryRuntime | None = None,
        active_project_id: str | None = None,
        active_project_name: str | None = None,
        image_ids: list[str] | None = None,
        effort: str = "low",
    ) -> None:
        self.db = db
        self.store = AppStore(db)
        #: Effort changes how thoroughly a turn is processed, never what it may do.
        #: Both levels route the same way and reach the same capabilities. High
        #: reasons before substantive answers and researches more deeply when it
        #: searches; Low answers without extended reasoning and stays concise.
        self.effort = "high" if effort == "high" else "low"
        self.reasoning = self.effort == "high"
        self.low_effort = not self.reasoning
        #: Gallery items the user attached to this turn, already resolved to
        #: base64 so the provider layer never touches the gallery. Empty when the
        #: turn carries no image or when the routed model cannot see one.
        self.turn_images: list[str] = []
        self.turn_image_items: list[dict] = []
        self._pending_image_ids = list(image_ids or [])
        if ollama is None:
            from app.services.llm import get_llm_client

            ollama = get_llm_client(route_name="chat", disable_thinking=not self.reasoning)
        self.ollama = ollama
        self.memory_runtime = memory_runtime
        self.active_project_id = active_project_id
        self.active_project_name = active_project_name
        self.rule_result = rule_result or {
            "resolved_rules": {},
            "applied_profiles": [],
            "warnings": [],
        }
        self.settings = get_settings()
        # Helper calls -- routing, calendar extraction, query planning, place
        # checks -- never reason, whatever the answer may do: they emit a few
        # tokens of JSON, and reasoning first only spent their output limit and
        # seconds per call. A client without ``derive`` (a test double) is used
        # as it is.
        derive = getattr(ollama, "derive", None)
        if callable(derive):
            self.helper_llm = derive(
                disable_thinking=True, num_predict=self.settings.chat_helper_num_predict
            )
            self.calendar_llm = derive(
                disable_thinking=True,
                num_predict=self.settings.chat_calendar_helper_num_predict,
            )
            self.quick_llm = derive(disable_thinking=True)
        else:
            self.helper_llm = self.calendar_llm = self.quick_llm = ollama
        self._timings: dict[str, Any] = {}
        self._turn_timezone: str | None = None
        self.memory_enabled = (
            bool(
                self.settings.memory_enabled
                and memory_orchestrator is not None
                and memory_context_factory is not None
            )
            if memory_enabled is None
            else memory_enabled
        )
        self.memory_prompt_enabled = self.memory_enabled
        # Answering straight from memory bypasses the model entirely, so the
        # model never gets to weigh saved memory against search or its own
        # knowledge.  Recall still runs and is injected as context either way.
        self.memory_direct_answers_enabled = bool(
            self.memory_enabled and self.settings.memory_direct_answer_enabled
        )
        self.memory_orchestrator = memory_orchestrator
        self.memory_context_factory = memory_context_factory
        self.last_memory_selection: RecallPromptSelection | None = None
        self.current_turn_override: CurrentTurnOverride | None = None
        self.direct_answers = DirectMemoryAnswerService(
            memory_orchestrator,
            enabled=(self.memory_enabled and self.memory_direct_answers_enabled),
        )
        self.web_search = WebSearchService(llm=self.helper_llm)
        self.project_context = ProjectContextService()
        self.task_context = TaskContextService()
        self.calendar_context = CalendarContextService()
        self.file_context = WorkspaceFilesService()
        self._resolve_turn_images()
        self.code_index = CodeIndexService()
        self.symbol_awareness = SymbolAwarenessService()
        self.test_runner = TestRunnerContextService()
        self.git_context = GitContextService()
        self.citation_formatter = CitationFormatter()
        self.last_web_debug: dict[str, Any] = {}
        self.last_routing_debug: dict[str, Any] = {}
        self.last_search_intent: ResolvedSearchIntent | None = None
        self.search_intent_resolver = SearchIntentResolver()
        #: The single authoritative record of what this turn did to the
        #: calendar. Written only from the application's own deterministic
        #: outcomes -- a verified mutation, or a committed mutation the
        #: calendar layer refused to complete -- and never inferred from the
        #: conversation, from a reply's wording, or from the model. Every
        #: turn resets it, and generation is told its value.
        self.calendar_execution: CalendarTurnExecution = "none"

    def build_context(self, prompt: str) -> ContextPackage:
        return ContextPackage(
            profile=[],
            preferences=[],
            goals=[],
            projects=[],
            relevant_memories=[],
            events=[],
            archive_results=[],
        )

    def build_messages(
        self,
        prompt: str,
        history: list[ChatTurn],
        context: ContextPackage,
        web_context: WebContext | None = None,
        project_context: str | None = None,
        task_context: str | None = None,
        calendar_execution: CalendarTurnExecution = "none",
        images: list[str] | None = None,
        gallery_context: str | None = None,
    ) -> list[LLMMessage]:
        web_section = self._compact_web_context(web_context)
        project_section = project_context or "No project context loaded."
        task_section = task_context or "No task context loaded."
        rule_result = getattr(
            self,
            "rule_result",
            {"resolved_rules": {}, "applied_profiles": [], "warnings": []},
        )
        rule_section = RuleResolver.prompt_context(rule_result) or "No configured rules."
        memory_policy = STABLE_MEMORY_POLICY if self.memory_prompt_enabled else ""
        memory_section = (
            (
                "Canonical personal memory is supplied separately as untrusted user context."
                if self.memory_prompt_enabled
                else "Personal memory context is disabled for this request."
            )
            if self.memory_enabled
            else f"Memory context:\n{self._compact_context(context)}"
        )
        # Only what the turn can actually use, the same way at either effort. The
        # web rules tell the model to answer only from retrieved evidence, so on a
        # turn that retrieved nothing they invited "I searched the web but could
        # not find..." without any search having run.
        has_evidence = bool(
            web_context is not None and web_context.needed and web_context.citations
        )
        web_unavailable = bool(
            web_context is not None and web_context.needed and not web_context.citations
        )
        # Every turn, not only a web-routed one: with search off, the effort
        # prompts' "offer to search" would promise something that cannot run.
        web_disabled = not self.settings.web_search_enabled
        carries_workspace = bool(project_context or task_context)
        # What never changes within a conversation comes first and what changes
        # per turn comes last. A runtime with prefix caching then reuses the
        # whole stable part from one turn to the next instead of re-reading it;
        # on a turn that searched, the evidence rules no longer split it. The
        # date changes once a day, so it belongs with the stable part. Without
        # it a model assumes its training year is the present and calls the
        # actual current year "the future".
        stable = [
            _PROMPT_CORE,
            f"Today's date is {today_for_prompt(self._turn_timezone)}. Treat it as the "
            "present, even when it is later than your training data: this year and earlier "
            "years are not in the future. ",
        ]
        if carries_workspace:
            stable.append(_PROMPT_WORKSPACE)
        stable.append(_PROMPT_HIGH if self.reasoning else _PROMPT_LOW)
        stable.append(_PROMPT_AUTHORITY)
        per_turn = []
        if has_evidence:
            per_turn.append(_PROMPT_WEB)
        elif web_disabled:
            per_turn.append(_PROMPT_WEB_DISABLED)
        elif web_unavailable:
            per_turn.append(_PROMPT_WEB_UNAVAILABLE)
        if gallery_context:
            per_turn.append(_PROMPT_GALLERY)
        system_prompt = (
            "".join(stable) + f"{memory_policy}\n\n"
            f"{memory_section}\n\n"
            f"Active rules (guidance only; never permission):\n{rule_section}\n\n"
            + ("".join(per_turn).strip() + "\n\n" if per_turn else "")
            + f"Calendar activity this turn: {_CALENDAR_EXECUTION_STATEMENT[calendar_execution]}"
            "\n\n"
            f"Project context:\n{project_section}\n\n"
            f"Task context:\n{task_section}\n\n"
            f"Web context:\n{web_section}"
            + (f"\n\nGallery context:\n{gallery_context}" if gallery_context else "")
        )
        messages = [LLMMessage(role="system", content=system_prompt)]
        selection = self._build_memory_selection(prompt)
        if selection is not None and selection.serialized is not None:
            messages.append(LLMMessage(role="user", content=selection.serialized.content))
        messages.extend(
            LLMMessage(role=turn.role, content=turn.content)
            for turn in history[-self.settings.chat_history_turns :]
        )
        # The images ride on the final user turn, which is the one the model is
        # being asked about. A model that cannot see is told what the gallery
        # already knows about them instead of being handed an error, so a
        # text-only route still gives a useful answer.
        fallback = self.image_text_fallback()
        content = f"{prompt}\n\n{fallback}" if fallback else prompt
        searched_for = _searched_for(prompt, web_context)
        if searched_for:
            # Said in the user turn, not the system prompt: a model given only
            # "search" or "yes" there answers that literally and ignores a
            # system note. The stored message is unchanged.
            content = (
                f'{content}\n\n(The web search for this message looked up "{searched_for}". '
                "Use it to answer what this message asks, citing the web context; an "
                "instruction to search or an agreement refers to that question.)"
            )
        messages.append(
            LLMMessage(
                role="user",
                content=content,
                images=images or self.turn_images or None,
            )
        )
        return messages

    def _resolve_turn_images(self) -> None:
        """Load attached gallery images, if the model can actually see them.

        A model without vision is given nothing here; ``_image_text_fallback``
        supplies the captions instead, so the user gets a worse answer rather
        than an error. Any failure to read an image is skipped for the same
        reason -- one unreadable attachment must not cost the whole message.
        """

        if not self._pending_image_ids:
            return
        if not self._model_supports_vision():
            self.turn_image_items = self._image_items(self._pending_image_ids)
            return
        try:
            from app.services.gallery.service import GalleryService

            payloads, items = GalleryService().attachments_for_prompt(self._pending_image_ids)
        except Exception:
            return
        self.turn_images, self.turn_image_items = payloads, items

    def _model_supports_vision(self) -> bool:
        """The registry's flag first, then the provider probe.

        The checkbox is the user's own statement about a gateway Neo cannot
        interrogate, so it wins; the probe is what fills the flag in for Ollama,
        where it has always defaulted to false with nothing populating it.
        """

        probe = getattr(self.ollama, "supports_vision", None)
        try:
            return bool(probe()) if callable(probe) else False
        except Exception:
            return False

    @staticmethod
    def _image_items(image_ids: list[str]) -> list[dict]:
        try:
            from app.services.gallery.service import GalleryService

            service = GalleryService()
            items = []
            for image_id in image_ids:
                try:
                    items.append(service.get(image_id))
                except LookupError:
                    continue
            return items
        except Exception:
            return []

    def _record_image_appearances(self, chat_id: int, message_id: int) -> None:
        """Note where each attached image was seen.

        This is what makes "that image I showed you last week" answerable: the
        sighting, not the upload, is what the user remembers. Failures are
        swallowed -- an appearance is a convenience, and losing one must never
        cost the message it belongs to.
        """

        if not self._pending_image_ids:
            return
        try:
            from app.services.gallery import store as gallery_store

            for image_id in self._pending_image_ids:
                gallery_store.record_appearance(
                    {
                        "id": str(uuid.uuid4()),
                        "item_id": image_id,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "role": "user",
                    }
                )
        except Exception:
            return

    def image_text_fallback(self) -> str:
        """What a blind model is told about the images it cannot see."""

        if self.turn_images or not self.turn_image_items:
            return ""
        blocks = []
        for item in self.turn_image_items:
            described = item.get("caption") or "No description is available yet."
            block = f"[Attached image: {item.get('title') or item['id']}]\n{described}"
            if item.get("ocr_text"):
                block += f"\nText in image: {item['ocr_text'][:1200]}"
            blocks.append(block)
        return "\n\n".join(blocks)

    def _gallery_matches(self, prompt: str, chat_id: int) -> GalleryMatches | None:
        """The gallery's answer to this turn, or ``None`` when it has no part in it.

        Returned only when the turn should be answered from the gallery: images
        matched, or the message asked for them outright and "there are none" is
        the answer. A looser mention of an image that matched nothing goes back
        to the ordinary route. The per-chat Tools toggle governs this exactly as
        it governs the agent's ``search_gallery``, and any failure is swallowed:
        a gallery that cannot be read must not cost the user their message.
        """

        cue = wants_gallery(prompt, has_attachments=bool(self._pending_image_ids))
        if cue is None:
            return None
        try:
            chat = self.store.get_chat(chat_id)
            if chat is not None and "search_gallery" in (chat.disabled_tools or []):
                return None
        except Exception:
            return None
        matches = gallery_lookup(
            prompt,
            cue=cue,
            chat_id=chat_id,
            project_id=self.active_project_id,
            exclude_ids=self._pending_image_ids,
        )
        if matches is None or (not matches.items and matches.cue != "explicit"):
            return None
        return matches

    def _memory_query_context(self, prompt: str) -> MemoryQueryContext | None:
        if not self.memory_enabled or self.memory_context_factory is None:
            return None
        context = self.memory_context_factory(prompt)
        if context is not None and self.current_turn_override is not None:
            context = context.model_copy(
                update={"current_turn_override": self.current_turn_override}
            )
        return context

    def _build_memory_selection(
        self,
        prompt: str,
    ) -> RecallPromptSelection | None:
        """The one canonical recall path shared by sync and stream."""
        self.last_memory_selection = None
        if not self.memory_prompt_enabled or self.memory_orchestrator is None:
            return None
        context = self._memory_query_context(prompt)
        if context is None:
            return None
        selection = self.memory_orchestrator.build(
            RecallQuery(context=context, text=prompt),
            purpose="chat_prompt",
        )
        self.last_memory_selection = selection
        return selection

    def _finalize_memory_usage(self) -> None:
        """Persist same-session usage metadata before issuing the model request."""
        selection = self.last_memory_selection
        if self.memory_enabled and selection is not None and selection.usage_recorded:
            self.db.commit()
            return
        self.db.rollback()

    def _persist_memory_diagnostic(self, message_id: int) -> None:
        """Persist text-free recall facts for the read-only conversation inspector."""

        if not self.memory_enabled:
            return
        selection = self.last_memory_selection
        override = self.current_turn_override
        if selection is None and override is None:
            return
        message = self.db.get(ChatMessage, message_id)
        if message is None or message.role != "user":
            return
        try:
            metadata = json.loads(message.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        recalled_ids = (
            [str(item) for item in selection.recall.canonical_ids] if selection is not None else []
        )
        suppressed_ids = (
            [str(item) for item in selection.recall.diagnostic.suppressed_ids]
            if selection is not None
            else [str(item) for item in override.suppressed_memory_ids]
            if override is not None
            else []
        )
        final_ids = (
            [str(item) for item in selection.serialized.canonical_ids]
            if selection is not None and selection.serialized is not None
            else []
        )
        metadata["memory_diagnostic"] = {
            "recalled_ids": recalled_ids,
            "current_turn_suppressed_ids": suppressed_ids,
            "final_serialized_ids": final_ids,
        }
        message.metadata_json = json.dumps(metadata, sort_keys=True)
        self.db.flush()

    def _begin_turn(self, turn_started_at: datetime | None = None) -> None:
        """Start the clock the reply's reported duration is measured against.

        A turn is much more than the one generate call a provider times for us.
        Route selection, memory retrieval, web search and any refinement pass are
        all model or network work the user watches the composer count through, and
        several of them are extra provider calls in their own right. Reporting only
        the final call's own `total_duration` made a thirty second wait read as
        thirteen seconds. The clock starts when the turn was accepted -- including
        any time it sat queued, which the user also waited through -- so the badge
        on the reply matches the wait it describes.
        """
        self._turn_started = time.perf_counter()
        self._turn_queued_ms = 0
        self._timings = {}
        self._request_marks = {
            id(client): len(getattr(client, "request_ids", []) or []) for client in self._clients()
        }
        if turn_started_at is None:
            return
        # A stored timestamp comes back naive on SQLite, and every one this
        # codebase writes is UTC.
        accepted = (
            turn_started_at.replace(tzinfo=UTC)
            if turn_started_at.tzinfo is None
            else turn_started_at
        )
        self._turn_queued_ms = max(0, int((datetime.now(UTC) - accepted).total_seconds() * 1000))

    def _turn_duration_ms(self) -> int:
        """Wall-clock milliseconds from accepting this turn until now."""
        started = getattr(self, "_turn_started", None)
        if started is None:
            return 0
        return getattr(self, "_turn_queued_ms", 0) + int((time.perf_counter() - started) * 1000)

    def send_message(
        self,
        chat_id: int,
        prompt: str,
        *,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> str:
        self._begin_turn()
        self._turn_timezone = timezone
        self.calendar_execution = "none"
        persisted_messages = self.store.list_chat_messages(chat_id)
        history = self._history_turns(persisted_messages)
        search_intent = self._resolve_search_intent(
            prompt,
            persisted_messages,
            timezone=timezone,
            locale=locale,
        )
        user_message = self.store.add_chat_message(
            chat_id,
            "user",
            prompt,
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
                # Recorded on the turn so a reloaded transcript still shows the
                # images the user attached, rather than a message that reads as
                # if they had sent nothing.
                **({"image_ids": self._pending_image_ids} if self._pending_image_ids else {}),
            },
        )
        self.store.rename_chat_from_prompt(chat_id, prompt)
        self._record_image_appearances(chat_id, user_message.id)
        self.db.commit()
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=user_message.id,
            selected_route="pending",
            component="chat_submission",
            final_status="received",
        )

        active_rules_reply = self._active_rules_reply(prompt)
        if active_rules_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", active_rules_reply)
            self.db.commit()
            self.last_web_debug = {
                "rules_loaded": True,
                "rule_warnings": self.rule_result.get("warnings", []),
                "web_search_needed": False,
            }
            return active_rules_reply

        agent_guidance = agent_run_guidance(prompt)
        if agent_guidance is not None:
            self.store.add_chat_message(chat_id, "assistant", agent_guidance)
            self.db.commit()
            self.last_web_debug = {
                "agent_guidance": True,
                "web_search_needed": False,
            }
            return agent_guidance
        self.current_turn_override = self._analyze_current_turn(
            prompt,
            chat_id=chat_id,
            message_id=user_message.id,
            history=persisted_messages,
        )
        context = self.build_context(prompt)
        project_context = self.project_context.context_for_prompt(prompt)
        task_context = self.task_context.context_for_prompt(prompt)
        task_context = f"{task_context}\n\n{self.file_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.code_index.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.symbol_awareness.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.test_runner.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.git_context.context_for_prompt(prompt)}"
        internal_intent = resolve_internal_chat_intent(prompt)
        git_direct_reply = (
            self.git_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "git"
            else None
        )
        if git_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", git_direct_reply)
            self.db.commit()
            self.last_web_debug = {"git_context_loaded": True, "web_search_needed": False}
            return git_direct_reply
        test_direct_reply = (
            self.test_runner.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tests"
            else None
        )
        if test_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", test_direct_reply)
            self.db.commit()
            self.last_web_debug = {"test_context_loaded": True, "web_search_needed": False}
            return test_direct_reply
        task_direct_reply = (
            self.task_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tasks"
            else None
        )
        if task_direct_reply is not None:
            self.store.add_chat_message(chat_id, "assistant", task_direct_reply)
            self.db.commit()
            self.last_web_debug = {
                "task_context_loaded": True,
                "web_search_needed": False,
            }
            return task_direct_reply
        pending_calendar_result = self._handle_pending_calendar_reply(
            prompt,
            chat_id=chat_id,
            history=persisted_messages,
            llm=self.helper_llm,
            timezone=timezone,
            locale=locale,
        )
        if pending_calendar_result is not None:
            reply, metadata = pending_calendar_result
            self.store.add_chat_message(chat_id, "assistant", reply, **metadata)
            self.db.commit()
            self.last_web_debug = {"pending_action_resolved": True, "web_search_needed": False}
            return reply
        # Asked about saved images: the gallery answers, so nothing that would
        # answer instead -- live data, the web, a stored memory -- is consulted.
        gallery = self._gallery_matches(prompt, chat_id)
        if gallery is not None:
            search_intent = search_intent.model_copy(update={"route": "gallery"})
        if gallery is None and search_intent.route is None:
            search_intent = self._route_turn(
                prompt,
                persisted_messages,
                search_intent,
                timezone=timezone,
                locale=locale,
                message_id=user_message.id,
            )
        calendar_result = None
        if gallery is None and search_intent.kind is SearchIntentKind.CALENDAR:
            calendar_started = time.perf_counter()
            calendar_result = self.calendar_context.handle_prompt(
                prompt, llm=self.calendar_llm, timezone=timezone, locale=locale
            )
            self._timings["calendar_ms"] = int((time.perf_counter() - calendar_started) * 1000)
        if calendar_result is not None:
            reply, metadata = calendar_result
            metadata = _with_timings(metadata, self._timings_snapshot(search_intent))
            calendar_refinement = metadata.pop("_calendar_refinement", None)
            if calendar_refinement:
                self._routing_diagnostic(
                    chat_id,
                    prompt,
                    message_id=None,
                    selected_route="calendar",
                    component="calendar_declarative_refinement",
                    final_status="refined",
                    extra=calendar_refinement,
                )
            self.store.add_chat_message(chat_id, "assistant", reply, **metadata)
            self.db.commit()
            kind = metadata.get("response_kind")
            self.last_web_debug = {
                "calendar_context_loaded": kind == "calendar_read",
                "calendar_proposal": kind == "calendar_proposal",
                "web_search_needed": False,
            }
            return reply
        self._adopt_calendar_execution()
        if self.calendar_execution == "failed":
            self.store.add_chat_message(
                chat_id,
                "assistant",
                _CALENDAR_NOT_COMPLETED_REPLY,
                **_CALENDAR_NOT_COMPLETED_METADATA,
            )
            self.db.commit()
            self.last_web_debug = {
                "calendar_failed_closed": True,
                "web_search_needed": False,
            }
            return _CALENDAR_NOT_COMPLETED_REPLY
        structured_live = (
            None
            if gallery is not None
            else self._structured_live_answer(
                prompt,
                search_intent,
                timezone=timezone,
                locale=locale,
            )
        )
        if structured_live is not None:
            reply, metadata = structured_live
            metadata = _with_timings(metadata, self._timings_snapshot(search_intent))
            self.store.add_chat_message(chat_id, "assistant", reply, **metadata)
            self.db.commit()
            self.last_web_debug = {
                "web_search_needed": False,
                "structured_intent": search_intent.model_dump(mode="json"),
            }
            return reply
        if gallery is None and search_intent.kind in {
            SearchIntentKind.GENERAL_WEB,
            SearchIntentKind.RELEASE_DATE,
        }:
            web_started = time.perf_counter()
            web_query, web_hint = self._web_query_for(prompt, search_intent)
            web_context = self.web_search.build_context_forced(
                web_query, hint=web_hint, options=self._search_options()
            )
            self._timings["search_ms"] = int((time.perf_counter() - web_started) * 1000)
        else:
            web_query = prompt
            web_context = WebContext(query=prompt, needed=False)
        direct_reply = (
            None if web_context.needed or gallery is not None else self._direct_reply(prompt)
        )
        if direct_reply is not None:
            self._finalize_memory_usage()
            self._persist_memory_diagnostic(user_message.id)
            self.store.add_chat_message(
                chat_id,
                "assistant",
                direct_reply,
                response_kind="direct_memory",
                provider_name="Neo memory",
                route_name="memory",
                finish_reason="stop",
                duration_ms=self._turn_duration_ms(),
                metadata={"search_intent": search_intent.model_dump(mode="json")},
            )
            self.db.commit()
            self.last_web_debug = self._web_debug(
                web_context, context=context, final_answer=direct_reply
            )
            return direct_reply
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None and not self._answers_without_evidence(web_context):
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
            self.store.add_chat_message(
                chat_id,
                "assistant",
                web_failure,
                response_kind=self._web_response_kind(web_context),
                provider_name=(
                    web_context.search.provider if web_context.search is not None else None
                ),
                route_name="web_search",
                finish_reason="evidence_unavailable",
                duration_ms=self._turn_duration_ms(),
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            )
            self.db.commit()
            return web_failure
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            self.store.add_chat_message(
                chat_id,
                "assistant",
                direct_web_reply,
                response_kind=self._web_response_kind(web_context),
                provider_name=(
                    web_context.search.provider if web_context.search is not None else None
                ),
                route_name="web_search",
                finish_reason="stop",
                duration_ms=self._turn_duration_ms(),
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                },
            )
            self.db.commit()
            return direct_web_reply
        messages = self.build_messages(
            prompt,
            history,
            context,
            web_context,
            project_context,
            task_context,
            calendar_execution=self.calendar_execution,
            gallery_context=gallery.prompt_block() if gallery is not None else None,
        )
        self._finalize_memory_usage()
        self._persist_memory_diagnostic(user_message.id)
        # Release this session's write reservation before calling the provider.
        # The provider runtime records its request audit through a separate
        # SQLite connection to the same database, so holding the transaction
        # open here makes that second writer wait out its busy timeout and fail
        # the whole turn with "database is locked".  The streaming path performs
        # the same commit for the same reason.
        self.db.commit()

        result = None
        finish_reason = None
        provider_name = None
        model_name = None
        route_name = "web_search" if web_context.needed else "chat"
        trace_id = None
        recovery: dict[str, Any] | None = None
        answer_llm = self._answer_client(search_intent)
        budget = self._num_predict(prompt, context, reasoning=self._answer_reasons(search_intent))
        try:
            generation_started = time.perf_counter()
            result = self._generate_complete(messages, num_predict=budget, llm=answer_llm)
            self._timings["generation_ms"] = int((time.perf_counter() - generation_started) * 1000)
            if web_context.citations:
                cited = self._cited_web_reply(
                    prompt, result.content, web_context, messages, budget=budget
                )
                if cited is None:
                    web_context = _without_usable_evidence(web_context)
                    messages = self.build_messages(
                        prompt,
                        history,
                        context,
                        web_context,
                        project_context,
                        task_context,
                        calendar_execution=self.calendar_execution,
                        gallery_context=gallery.prompt_block() if gallery is not None else None,
                    )
                    result = self._generate_complete(messages, num_predict=budget, llm=answer_llm)
                    cited = self._with_web_citations(result.content, web_context)
                reply = cited
            else:
                reply = self._with_web_citations(result.content, web_context)
            failure = self._classify_answer_failure(
                reply,
                finish_reason=result.finish_reason,
                intent=search_intent,
                web_context=web_context,
                gallery=gallery,
            )
            if failure is not None:
                recovery_started = time.perf_counter()
                recovered = self._recover_answer(
                    failure,
                    prompt,
                    messages=messages,
                    context=context,
                    history=history,
                    project_context=project_context,
                    task_context=task_context,
                    llm=answer_llm,
                    budget=budget,
                    web_context=web_context,
                )
                recovery = {"kind": failure, **recovered["recovery"]}
                if recovered.get("reply") is not None:
                    reply = recovered["reply"]
                    web_context = recovered.get("web_context") or web_context
                    route_name = "web_search" if web_context.needed else route_name
                    if recovered.get("result") is not None:
                        result = recovered["result"]
                if failure == "empty" and not reply.strip():
                    reply = _EMPTY_ANSWER_REPLY
                if recovery.get("result") == "search_failed":
                    reply = f"{reply.rstrip()}\n\n{_SEARCH_FOUND_NOTHING_NOTE}"
                recovery["ms"] = int((time.perf_counter() - recovery_started) * 1000)
                self._timings["recovery"] = recovery
            prompt_tokens = result.prompt_tokens
            completion_tokens = result.completion_tokens
            total_tokens = result.total_tokens
            thinking = result.thinking
            self._note_unrequested_reasoning(answer_llm, thinking)
            finish_reason = result.finish_reason
            provider_name = result.provider_name or result.provider_id
            model_name = result.model_name or result.model_id
            route_name = result.route_name or route_name
            trace_id = result.provider_request_id
        except Exception as exc:
            if web_context.citations:
                reply = self._web_generation_fallback(prompt, web_context, exc)
                prompt_tokens = None
                completion_tokens = None
                total_tokens = None
                thinking = None
                finish_reason = "provider_error"
            else:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
        memory_extraction = {"status": "scheduled", "source_message_id": str(user_message.id)}
        if recovery is not None:
            self._routing_diagnostic(
                chat_id,
                prompt,
                message_id=user_message.id,
                selected_route="web_search" if recovery.get("kind") == "knowledge_gap" else "llm",
                component="answer_recovery",
                final_status=str(recovery.get("result")),
                extra=recovery,
            )
        assistant = self.store.add_chat_message(
            chat_id,
            "assistant",
            reply,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=self._turn_duration_ms(),
            thinking=thinking,
            response_kind=self._web_response_kind(web_context),
            provider_name=provider_name,
            model_name=model_name,
            route_name=route_name,
            finish_reason=finish_reason,
            trace_id=trace_id,
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
                "memory_extraction": memory_extraction,
                "web_debug": self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                    final_answer=reply,
                ),
                "timings": self._timings_snapshot(search_intent),
                **(
                    {"gallery_results": gallery.shown()}
                    if gallery is not None and gallery.items
                    else {}
                ),
            },
        )
        self.db.commit()
        self._extract_after_response(
            prompt,
            chat_id=chat_id,
            message_id=user_message.id,
            assistant_message_id=assistant.id,
            history=persisted_messages,
            transport="sync",
        )
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
        )
        return reply

    def _persist_stream_assistant(
        self,
        chat_id: int,
        content: str,
        *,
        generation_id: str | None,
        generation_lease_token: str | None,
        **metadata,
    ) -> ChatMessage:
        """Persist one streamed result and fence workers that lost their lease."""

        # Every route through the turn lands here exactly once, with its final
        # answer, so this is where the reported duration is settled -- the whole
        # turn, matching the timer the user watched, not whatever slice of it the
        # caller happened to have measured.
        metadata["duration_ms"] = self._turn_duration_ms()
        if generation_id is None:
            return self.store.add_chat_message(chat_id, "assistant", content, **metadata)
        if generation_lease_token is None:
            raise RuntimeError("A durable generation requires an active lease token.")
        lease = self.db.execute(
            update(ChatGeneration)
            .where(
                ChatGeneration.id == generation_id,
                ChatGeneration.chat_id == chat_id,
                ChatGeneration.status == "running",
                ChatGeneration.lease_token == generation_lease_token,
            )
            .values(heartbeat_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        if lease.rowcount != 1:
            self.db.rollback()
            raise RuntimeError("The generation lease is no longer active.")
        return self.store.upsert_generation_assistant(
            chat_id,
            generation_id,
            content,
            **metadata,
        )

    def stream_message(
        self,
        chat_id: int,
        prompt: str,
        after_reply: Callable[[str, str], None] | None = None,
        existing_user_message_id: int | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        generation_id: str | None = None,
        generation_lease_token: str | None = None,
        turn_started_at: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream one turn, timing the whole of it rather than its last model call.

        The duration a turn reports is decided here, on the way out, so every
        route through the body -- a direct memory answer, a web search, a plain
        generation -- is measured the same way: from when the turn was accepted
        to when its final event leaves.
        """
        self._begin_turn(turn_started_at)
        for event in self._stream_message_events(
            chat_id,
            prompt,
            after_reply=after_reply,
            existing_user_message_id=existing_user_message_id,
            timezone=timezone,
            locale=locale,
            generation_id=generation_id,
            generation_lease_token=generation_lease_token,
        ):
            kind = event.get("type")
            # Recorded as events leave, before the generator resumes, so the
            # message persisted at the end of the turn already carries them.
            if kind in {"status", "thinking", "chunk", "replace"}:
                self._timings.setdefault("first_event_ms", self._turn_duration_ms())
            if kind in {"chunk", "replace"} and str(event.get("content") or "").strip():
                self._timings.setdefault("first_content_ms", self._turn_duration_ms())
            if kind == "done":
                event = {**event, "duration_ms": self._turn_duration_ms()}
            yield event

    def _stream_message_events(
        self,
        chat_id: int,
        prompt: str,
        *,
        after_reply: Callable[[str, str], None] | None = None,
        existing_user_message_id: int | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        generation_id: str | None = None,
        generation_lease_token: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._turn_timezone = timezone
        self.calendar_execution = "none"

        def persist_assistant(content: str, **metadata) -> ChatMessage:
            return self._persist_stream_assistant(
                chat_id,
                content,
                generation_id=generation_id,
                generation_lease_token=generation_lease_token,
                **metadata,
            )

        persisted_messages = self.store.list_chat_messages(chat_id)
        history = self._history_turns(persisted_messages)
        if existing_user_message_id is not None:
            try:
                message_index = next(
                    index
                    for index, message in enumerate(persisted_messages)
                    if message.id == existing_user_message_id and message.role == "user"
                )
            except StopIteration as exc:
                raise ValueError("The edited user message no longer exists.") from exc
            history = history[:message_index]
            search_history = persisted_messages[:message_index]
            routing_message_id = existing_user_message_id
        else:
            search_history = persisted_messages
            search_intent = self._resolve_search_intent(
                prompt,
                search_history,
                timezone=timezone,
                locale=locale,
            )
            user_message = self.store.add_chat_message(
                chat_id,
                "user",
                prompt,
                metadata={
                    "search_intent": search_intent.model_dump(mode="json"),
                    **({"image_ids": self._pending_image_ids} if self._pending_image_ids else {}),
                },
            )
            self.store.rename_chat_from_prompt(chat_id, prompt)
            self._record_image_appearances(chat_id, user_message.id)
            self.db.commit()
            routing_message_id = user_message.id
        if existing_user_message_id is not None:
            search_intent = self._resolve_search_intent(
                prompt,
                search_history,
                timezone=timezone,
                locale=locale,
            )
            source_message = self.db.get(ChatMessage, existing_user_message_id)
            if source_message is not None:
                try:
                    source_metadata = json.loads(source_message.metadata_json or "{}")
                except (TypeError, ValueError):
                    source_metadata = {}
                if not isinstance(source_metadata, dict):
                    source_metadata = {}
                source_metadata["search_intent"] = search_intent.model_dump(mode="json")
                if generation_id is not None:
                    source_metadata["generation_id"] = generation_id
                source_message.metadata_json = json.dumps(source_metadata, sort_keys=True)
                self.db.commit()
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=routing_message_id,
            selected_route="pending",
            component="chat_submission",
            final_status="received",
        )

        active_rules_reply = self._active_rules_reply(prompt)
        if active_rules_reply is not None:
            assistant = persist_assistant(active_rules_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "rules_loaded": True,
                "rule_warnings": self.rule_result.get("warnings", []),
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": active_rules_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": active_rules_reply,
                "web_debug": self.last_web_debug,
            }
            return

        agent_guidance = agent_run_guidance(prompt)
        if agent_guidance is not None:
            assistant = persist_assistant(agent_guidance)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "agent_guidance": True,
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": agent_guidance}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": agent_guidance,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        self.current_turn_override = self._analyze_current_turn(
            prompt,
            chat_id=chat_id,
            message_id=routing_message_id,
            history=search_history,
        )
        context = self.build_context(prompt)
        project_context = self.project_context.context_for_prompt(prompt)
        task_context = self.task_context.context_for_prompt(prompt)
        task_context = f"{task_context}\n\n{self.file_context.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.code_index.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.symbol_awareness.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.test_runner.context_for_prompt(prompt)}"
        task_context = f"{task_context}\n\n{self.git_context.context_for_prompt(prompt)}"
        internal_intent = resolve_internal_chat_intent(prompt)
        git_direct_reply = (
            self.git_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "git"
            else None
        )
        if git_direct_reply is not None:
            assistant = persist_assistant(git_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"git_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": git_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": git_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        test_direct_reply = (
            self.test_runner.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tests"
            else None
        )
        if test_direct_reply is not None:
            assistant = persist_assistant(test_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"test_context_loaded": True, "web_search_needed": False}
            yield {"type": "chunk", "content": test_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": test_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        task_direct_reply = (
            self.task_context.answer_for_prompt(prompt)
            if internal_intent is not None and internal_intent.feature == "tasks"
            else None
        )
        if task_direct_reply is not None:
            assistant = persist_assistant(task_direct_reply)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "task_context_loaded": True,
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": task_direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": task_direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        pending_calendar_result = self._handle_pending_calendar_reply(
            prompt,
            chat_id=chat_id,
            history=search_history,
            llm=self.helper_llm,
            timezone=timezone,
            locale=locale,
        )
        if pending_calendar_result is not None:
            reply, metadata = pending_calendar_result
            assistant = persist_assistant(reply, **metadata)
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {"pending_action_resolved": True, "web_search_needed": False}
            yield {"type": "chunk", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
            }
            return
        # Asked about saved images: the gallery answers, so nothing that would
        # answer instead -- live data, the web, a stored memory -- is consulted.
        gallery = self._gallery_matches(prompt, chat_id)
        if gallery is not None:
            search_intent = search_intent.model_copy(update={"route": "gallery"})
        if gallery is None and search_intent.route is None:
            yield {"type": "status", "content": "Understanding request"}
            search_intent = self._route_turn(
                prompt,
                search_history,
                search_intent,
                timezone=timezone,
                locale=locale,
                message_id=routing_message_id,
            )
        calendar_result = None
        if gallery is None and search_intent.kind is SearchIntentKind.CALENDAR:
            calendar_started = time.perf_counter()
            calendar_result = self.calendar_context.handle_prompt(
                prompt, llm=self.calendar_llm, timezone=timezone, locale=locale
            )
            self._timings["calendar_ms"] = int((time.perf_counter() - calendar_started) * 1000)
        if calendar_result is not None:
            reply, metadata = calendar_result
            metadata = _with_timings(metadata, self._timings_snapshot(search_intent))
            calendar_refinement = metadata.pop("_calendar_refinement", None)
            if calendar_refinement:
                self._routing_diagnostic(
                    chat_id,
                    prompt,
                    message_id=None,
                    selected_route="calendar",
                    component="calendar_declarative_refinement",
                    final_status="refined",
                    extra=calendar_refinement,
                )
            assistant = persist_assistant(reply, **metadata)
            self.db.commit()
            self.db.refresh(assistant)
            kind = metadata.get("response_kind")
            self.last_web_debug = {
                "calendar_context_loaded": kind == "calendar_read",
                "calendar_proposal": kind == "calendar_proposal",
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in metadata.items() if key != "metadata"},
            }
            return
        self._adopt_calendar_execution()
        if self.calendar_execution == "failed":
            assistant = persist_assistant(
                _CALENDAR_NOT_COMPLETED_REPLY, **_CALENDAR_NOT_COMPLETED_METADATA
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "calendar_failed_closed": True,
                "web_search_needed": False,
            }
            yield {"type": "chunk", "content": _CALENDAR_NOT_COMPLETED_REPLY}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": _CALENDAR_NOT_COMPLETED_REPLY,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                "response_kind": _CALENDAR_NOT_COMPLETED_METADATA["response_kind"],
            }
            return
        structured_live = (
            None
            if gallery is not None
            else self._structured_live_answer(
                prompt,
                search_intent,
                timezone=timezone,
                locale=locale,
            )
        )
        if structured_live is not None:
            reply, metadata = structured_live
            metadata = _with_timings(metadata, self._timings_snapshot(search_intent))
            assistant = persist_assistant(
                reply,
                **metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = {
                "web_search_needed": False,
                "structured_intent": search_intent.model_dump(mode="json"),
            }
            yield {"type": "chunk", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in metadata.items() if key != "metadata"},
            }
            return
        web_started = time.perf_counter()
        if gallery is None and search_intent.kind in {
            SearchIntentKind.GENERAL_WEB,
            SearchIntentKind.RELEASE_DATE,
        }:
            web_query, web_hint = self._web_query_for(prompt, search_intent)
            if self.settings.web_search_enabled:
                yield {"type": "status", "content": "Searching the web"}
            web_context = self.web_search.build_context_forced(
                web_query, hint=web_hint, options=self._search_options()
            )
            self._timings["search_ms"] = int((time.perf_counter() - web_started) * 1000)
        else:
            web_query = prompt
            web_context = WebContext(query=prompt, needed=False)
        direct_reply = (
            None if web_context.needed or gallery is not None else self._direct_reply(prompt)
        )
        if direct_reply is not None:
            self._finalize_memory_usage()
            self._persist_memory_diagnostic(routing_message_id)
            direct_metadata = {
                "response_kind": "direct_memory",
                "provider_name": "Neo memory",
                "route_name": "memory",
                "finish_reason": "stop",
                "duration_ms": 0,
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                },
            }
            assistant = persist_assistant(
                direct_reply,
                **direct_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            self.last_web_debug = self._web_debug(
                web_context, context=context, final_answer=direct_reply
            )
            yield {"type": "chunk", "content": direct_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": direct_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in direct_metadata.items() if key != "metadata"},
            }
            return
        web_failure = self._web_failure_reply(web_context)
        if web_failure is not None and not self._answers_without_evidence(web_context):
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                final_answer=web_failure,
            )
            web_metadata = {
                "response_kind": self._web_response_kind(web_context),
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "evidence_unavailable",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                    "timings": self._timings_snapshot(search_intent),
                },
            }
            assistant = persist_assistant(
                web_failure,
                **web_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            yield {"type": "chunk", "content": web_failure}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": web_failure,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in web_metadata.items() if key != "metadata"},
            }
            return
        direct_web_reply = self._direct_web_reply(web_query, web_context)
        if direct_web_reply is not None:
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=True,
                final_answer=direct_web_reply,
            )
            web_metadata = {
                "response_kind": self._web_response_kind(web_context),
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "stop",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "web_debug": self.last_web_debug,
                    "timings": self._timings_snapshot(search_intent),
                },
            }
            assistant = persist_assistant(
                direct_web_reply,
                **web_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, direct_web_reply)
            yield {"type": "chunk", "content": direct_web_reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": direct_web_reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": web_metadata["duration_ms"],
                "web_debug": self.last_web_debug,
                "response_kind": web_metadata["response_kind"],
                "provider_name": web_metadata["provider_name"],
                "route_name": web_metadata["route_name"],
                "finish_reason": web_metadata["finish_reason"],
            }
            return
        messages = self.build_messages(
            prompt,
            history,
            context,
            web_context,
            project_context,
            task_context,
            calendar_execution=self.calendar_execution,
            gallery_context=gallery.prompt_block() if gallery is not None else None,
        )
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=routing_message_id,
            selected_route="llm",
            component="default_chat_route",
            matched_intent=(
                f"{internal_intent.feature}:{internal_intent.action}"
                if internal_intent is not None
                else None
            ),
            confidence=1.0 if internal_intent is not None else 0.0,
            provider_invoked=True,
            response_source="provider_pending",
            final_status="streaming",
        )
        self._finalize_memory_usage()
        self._persist_memory_diagnostic(routing_message_id)
        # The provider runtime records request/stream audit state through its
        # own short-lived SQLite connection.  Persisting the diagnostic above
        # performs a flush and therefore opens a write transaction on this
        # session; leaving it open while starting the provider makes the second
        # writer wait until SQLite's timeout and fail with "database is locked".
        self.db.commit()

        answer_llm = self._answer_client(search_intent)
        # Text that answers from web evidence is held back until its citations are
        # checked; everything else streams as it is written.
        buffer_for_validation = bool(web_context.needed and web_context.citations)
        if buffer_for_validation:
            yield {"type": "status", "content": _writing_from_sources(web_context)}
        elif web_context.needed and not self.settings.web_search_enabled:
            yield {"type": "status", "content": "Web search is off; answering from what I know"}
        output_budget = self._num_predict(
            prompt, context, reasoning=self._answer_reasons(search_intent)
        )
        generation_started = time.perf_counter()
        try:
            (
                raw_reply,
                final_metadata,
                streamed_reply,
                streamed_thinking,
            ) = yield from self._stream_answer(
                answer_llm, messages, output_budget, buffer_content=buffer_for_validation
            )
        except Exception as exc:
            if not web_context.citations:
                self.last_web_debug = self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                )
                raise
            reply = self._web_generation_fallback(prompt, web_context, exc)
            self.last_web_debug = self._web_debug(
                web_context,
                context=context,
                web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                final_answer=reply,
            )
            fallback_metadata = {
                "response_kind": self._web_response_kind(web_context),
                "provider_name": (
                    web_context.search.provider if web_context.search is not None else None
                ),
                "route_name": "web_search",
                "finish_reason": "provider_error",
                "duration_ms": int((time.perf_counter() - web_started) * 1000),
                "metadata": {
                    "search_intent": search_intent.model_dump(mode="json"),
                    "fallback": True,
                    "web_debug": self.last_web_debug,
                },
            }
            assistant = persist_assistant(
                reply,
                **fallback_metadata,
            )
            self.db.commit()
            self.db.refresh(assistant)
            if after_reply is not None:
                after_reply(prompt, reply)
            yield {"type": "replace", "content": reply}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": reply,
                "thinking": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "web_debug": self.last_web_debug,
                **{key: value for key, value in fallback_metadata.items() if key != "metadata"},
            }
            return

        self._timings["generation_ms"] = int((time.perf_counter() - generation_started) * 1000)
        incomplete = final_metadata.get("finish_reason") == "length"
        cleaned_reply = answer_llm.clean_response(raw_reply)
        if incomplete:
            reply = (
                "The selected model repeatedly reached its output limit before it "
                "could complete a reliable answer. I did not save the truncated text. "
                "Please narrow the request or increase the model output limit."
            )
            final_metadata["finish_reason"] = "incomplete_length"
        elif web_context.citations:
            if (
                cleaned_reply.strip()
                and not _SOURCES_LACK_ANSWER.search(cleaned_reply)
                and not self._has_web_citation_marker(cleaned_reply, web_context)
            ):
                yield {"type": "status", "content": "Adding sources to the answer"}
            cited = self._cited_web_reply(
                prompt, cleaned_reply, web_context, messages, budget=output_budget
            )
            if cited is None:
                yield {
                    "type": "status",
                    "content": "The sources didn't answer this; answering from what I know",
                }
                web_context = _without_usable_evidence(web_context)
                messages = self.build_messages(
                    prompt,
                    history,
                    context,
                    web_context,
                    project_context,
                    task_context,
                    calendar_execution=self.calendar_execution,
                    gallery_context=gallery.prompt_block() if gallery is not None else None,
                )
                (
                    general_raw,
                    general_metadata,
                    streamed_reply,
                    general_thinking,
                ) = yield from self._stream_answer(
                    answer_llm, messages, output_budget, buffer_content=False
                )
                final_metadata = _merge_generation_metadata(final_metadata, general_metadata)
                if general_thinking.strip():
                    streamed_thinking = f"{streamed_thinking}\n\n{general_thinking}".strip()
                cited = self._with_web_citations(
                    answer_llm.clean_response(general_raw), web_context
                )
            reply = cited
        else:
            reply = self._with_web_citations(cleaned_reply, web_context)
        failure = (
            None
            if incomplete
            else self._classify_answer_failure(
                reply,
                finish_reason=final_metadata.get("finish_reason"),
                intent=search_intent,
                web_context=web_context,
                gallery=gallery,
            )
        )
        recovery: dict[str, Any] | None = None
        if failure is not None:
            recovery_started = time.perf_counter()
            recovery = {"kind": failure}
            recovered_context = web_context
            recovery_messages = messages
            if failure == "knowledge_gap":
                recovery["action"] = "search"
                yield {"type": "status", "content": "Searching the web"}
                recovered_context = self.web_search.build_context_forced(
                    prompt, options=self._search_options()
                )
                direct = self._direct_web_reply(prompt, recovered_context)
                if direct is not None:
                    reply, web_context = direct, recovered_context
                    recovery["result"] = "direct"
                    recovery_messages = None
                elif self._web_failure_reply(recovered_context) is not None:
                    recovery["result"] = "search_failed"
                    recovery_messages = None
                else:
                    recovery_messages = self.build_messages(
                        prompt,
                        history,
                        context,
                        recovered_context,
                        project_context,
                        task_context,
                        calendar_execution=self.calendar_execution,
                    )
                    yield {"type": "replace", "content": ""}
                    yield {"type": "status", "content": _writing_from_sources(recovered_context)}
            else:
                recovery["action"] = "regenerate"
            if recovery_messages is not None:
                try:
                    (
                        recovered_raw,
                        recovered_meta,
                        _,
                        recovered_thinking,
                    ) = yield from self._stream_answer(
                        answer_llm,
                        recovery_messages,
                        output_budget,
                        buffer_content=True,
                    )
                except Exception:
                    recovered_raw, recovered_meta, recovered_thinking = "", {}, ""
                recovered = answer_llm.clean_response(recovered_raw)
                if recovered.strip() and recovered_meta.get("finish_reason") != "length":
                    if recovered_context.citations and not self._has_web_citation_marker(
                        recovered, recovered_context
                    ):
                        reply = self._web_generation_fallback(
                            prompt,
                            recovered_context,
                            RuntimeError("recovered answer lacked citation markers"),
                        )
                    else:
                        reply = self._with_web_citations(recovered, recovered_context)
                    web_context = recovered_context
                    final_metadata = _merge_generation_metadata(final_metadata, recovered_meta)
                    if recovered_thinking.strip():
                        streamed_thinking = f"{streamed_thinking}\n\n{recovered_thinking}".strip()
                    recovery["result"] = "generated"
                else:
                    recovery.setdefault("result", "unchanged")
            if failure == "empty" and not reply.strip():
                reply = _EMPTY_ANSWER_REPLY
            if recovery.get("result") == "search_failed":
                reply = f"{reply.rstrip()}\n\n{_SEARCH_FOUND_NOTHING_NOTE}"
            recovery["ms"] = int((time.perf_counter() - recovery_started) * 1000)
            self._timings["recovery"] = recovery
            self._routing_diagnostic(
                chat_id,
                prompt,
                message_id=routing_message_id,
                selected_route="web_search" if failure == "knowledge_gap" else "llm",
                component="answer_recovery",
                final_status=str(recovery.get("result")),
                extra=recovery,
            )
        memory_extraction = {"status": "scheduled", "source_message_id": str(routing_message_id)}
        if buffer_for_validation or reply != streamed_reply:
            yield {"type": "replace", "content": reply}
        thinking = (
            final_metadata.get("thinking")
            or streamed_thinking.strip()
            or answer_llm.extract_thinking(raw_reply)
        )
        self._note_unrequested_reasoning(answer_llm, thinking)
        assistant = persist_assistant(
            reply,
            prompt_tokens=final_metadata.get("prompt_tokens"),
            completion_tokens=final_metadata.get("completion_tokens"),
            total_tokens=final_metadata.get("total_tokens"),
            duration_ms=final_metadata.get("duration_ms"),
            thinking=thinking,
            response_kind=self._web_response_kind(web_context),
            provider_name=final_metadata.get("provider_name") or final_metadata.get("provider"),
            model_name=final_metadata.get("model_name") or final_metadata.get("model"),
            route_name=final_metadata.get("route_name") or "chat",
            finish_reason=final_metadata.get("finish_reason"),
            trace_id=final_metadata.get("provider_request_id"),
            metadata={
                "search_intent": search_intent.model_dump(mode="json"),
                "memory_extraction": memory_extraction,
                "web_debug": self._web_debug(
                    web_context,
                    context=context,
                    web_context_in_prompt=bool(web_context.needed and web_context.context_text),
                    final_answer=reply,
                ),
                "timings": self._timings_snapshot(search_intent),
                **(
                    {"gallery_results": gallery.shown()}
                    if gallery is not None and gallery.items
                    else {}
                ),
            },
        )
        self.db.commit()
        self.db.refresh(assistant)
        self._extract_after_response(
            prompt,
            chat_id=chat_id,
            message_id=routing_message_id,
            assistant_message_id=assistant.id,
            history=search_history,
            transport="stream",
        )
        if after_reply is not None:
            after_reply(prompt, reply)
        self.last_web_debug = self._web_debug(
            web_context,
            context=context,
            web_context_in_prompt=bool(web_context.needed and web_context.context_text),
            final_answer=reply,
        )
        self.last_web_debug["routing"] = self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=assistant.id,
            selected_route="llm",
            component="default_chat_route",
            matched_intent=(
                f"{internal_intent.feature}:{internal_intent.action}"
                if internal_intent is not None
                else None
            ),
            confidence=1.0 if internal_intent is not None else 0.0,
            provider_invoked=True,
            provider=final_metadata.get("provider")
            or getattr(self.ollama, "last_metadata", {}).get("provider"),
            model=final_metadata.get("model")
            or getattr(self.ollama, "last_metadata", {}).get("model"),
            fallback_reason=("provider_fallback" if final_metadata.get("fallback_used") else None),
            response_source="provider",
            final_status="completed",
        )
        yield {
            "type": "done",
            "message_id": assistant.id,
            "reply": reply,
            "thinking": thinking,
            "prompt_tokens": final_metadata.get("prompt_tokens"),
            "completion_tokens": final_metadata.get("completion_tokens"),
            "total_tokens": final_metadata.get("total_tokens"),
            "duration_ms": final_metadata.get("duration_ms"),
            "response_kind": self._web_response_kind(web_context),
            "provider_name": final_metadata.get("provider_name") or final_metadata.get("provider"),
            "model_name": final_metadata.get("model_name") or final_metadata.get("model"),
            "route_name": final_metadata.get("route_name") or "chat",
            "finish_reason": final_metadata.get("finish_reason"),
            "trace_id": final_metadata.get("provider_request_id"),
            "web_debug": self.last_web_debug,
        }

    def _active_rules_reply(self, prompt: str) -> str | None:
        if not re.search(
            r"\b(which|what|show|list).{0,30}\b(active |applied )?rules\b", prompt, re.I
        ):
            return None
        profiles = self.rule_result.get("applied_profiles", [])
        rules = self.rule_result.get("resolved_rules", {})
        lines = ["Active rules for this chat:"]
        if profiles:
            lines.extend(f"- {item['name']} ({item['scope_type']})" for item in profiles)
        else:
            lines.append("- Built-in safety rules only")
        guidance = [*rules.get("instructions", []), *rules.get("coding_style", [])]
        if guidance:
            lines.append("Guidance:")
            lines.extend(f"- {item}" for item in guidance)
        forbidden = rules.get("forbidden_paths", [])
        if forbidden:
            lines.append("Forbidden paths: " + ", ".join(forbidden))
        warnings = self.rule_result.get("warnings", [])
        if warnings:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in warnings)
        lines.append("Rules are guidance only and cannot grant permissions or disable safety.")
        return "\n".join(lines)

    def _extraction_request(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
        transport: str,
        mode: ExtractionMode,
    ) -> ExtractionRequest:
        # Extraction reads a bounded window, so a very long message is trimmed to fit
        # rather than rejected. The hash below covers the trimmed text, keeping the
        # recorded provenance aligned with what extraction actually saw.
        extraction_message = prompt[:EXTRACTION_WINDOW_MAX_CHARS]
        supporting = tuple(
            TrustedConversationMessage(
                message_id=str(item.id),
                role=ConversationRole(item.role),
                content=item.content,
            )
            for item in history[-12:]
            if item.role in {"user", "assistant"} and item.id != message_id
        )
        total = len(extraction_message)
        bounded: list[TrustedConversationMessage] = []
        for item in reversed(supporting):
            if total + len(item.content) > EXTRACTION_WINDOW_MAX_CHARS:
                continue
            bounded.append(item)
            total += len(item.content)
        bounded.reverse()
        return ExtractionRequest(
            request_id=f"chat:{chat_id}:{message_id}:{mode.value}",
            owner_id=self.memory_runtime.execution.owner_id,
            conversation_id=str(chat_id),
            active_project_id=self.active_project_id,
            active_project_name=self.active_project_name,
            session_id=f"profile:{self.memory_runtime.execution.profile_id}",
            message_id=str(message_id),
            user_message=extraction_message,
            supporting_window=tuple(bounded),
            explicit_memory_intent=bool(
                re.search(r"\b(?:remember|save|forget|correct|changed my mind)\b", prompt, re.I)
            ),
            incognito=self.memory_runtime.execution.is_incognito,
            memory_enabled=self.memory_enabled,
            mode=mode,
            source_content_hash=ExtractionRequest.content_hash(extraction_message),
        )

    def _run_extraction(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
        mode: ExtractionMode,
        transport: str,
    ):
        if not self.memory_enabled or self.memory_runtime is None:
            return None
        request = self._extraction_request(
            prompt,
            chat_id=chat_id,
            message_id=message_id,
            history=history,
            transport=transport,
            mode=mode,
        )
        context = self.memory_runtime.context(
            source_kind=SourceKind.CHAT_MESSAGE,
            source_id="chat",
            request_id=request.request_id,
            session_id=request.session_id,
            conversation_id=request.conversation_id,
            message_id=request.message_id,
        )
        return self.memory_runtime.extraction.process(request, context, transport=transport)

    def _analyze_current_turn(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        history: list[ChatMessage],
    ) -> CurrentTurnOverride | None:
        # A second model call before the first token, to catch a correction the
        # post-turn pass would catch anyway -- just one turn later.
        if self.low_effort:
            return None
        if not re.search(
            r"\b(?:changed my mind|instead|no longer|not anymore|forget|remove|correct)\b",
            prompt,
            re.I,
        ):
            return None
        try:
            result = self._run_extraction(
                prompt,
                chat_id=chat_id,
                message_id=message_id,
                history=history,
                mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
                transport="sync",
            )
            # "forget" matches the correction trigger, so a retraction is applied
            # here, in the foreground, and never appears in the post-turn result
            # that writes the message metadata.  Carry the count across, or the
            # turn is not recognised as a forget and its text keeps being
            # replayed into later prompts.
            self._foreground_forgotten = sum(
                getattr(decision, "outcome", None) in _MEMORY_FORGET_OUTCOMES
                for decision in (getattr(result, "decisions", ()) or ())
            )
            return result.current_turn_override if result is not None else None
        except Exception:
            return None

    def _extract_after_response(
        self,
        prompt: str,
        *,
        chat_id: int,
        message_id: int,
        assistant_message_id: int,
        history: list[ChatMessage],
        transport: str,
    ) -> dict[str, object]:
        if not self.memory_enabled or self.memory_runtime is None:
            return {"status": "disabled"}
        if not turn_may_contain_memory(prompt):
            # A turn made only of questions states nothing to remember, and
            # running extraction on it spent a local model call re-asserting
            # facts read out of the supporting window as new candidates.
            return {"status": "skipped", "reason": "no_memory_bearing_statement"}
        database = self.db.get_bind()
        # Snapshot every ORM-backed input while the chat session is still owned
        # by this worker. Background threads must never dereference ChatMessage
        # instances while the parent session is committing/closing.
        try:
            request = self._extraction_request(
                prompt,
                chat_id=chat_id,
                message_id=message_id,
                history=history,
                transport=transport,
                mode=ExtractionMode.POST_TURN_AUTOMATIC,
            )
            context = self.memory_runtime.context(
                source_kind=SourceKind.CHAT_MESSAGE,
                source_id="chat",
                request_id=request.request_id,
                session_id=request.session_id,
                conversation_id=request.conversation_id,
                message_id=request.message_id,
            )
        except Exception as exc:
            _ROUTING_LOG.exception(
                "memory_extraction_snapshot_failed chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self._record_memory_extraction_failure(
                database,
                assistant_message_id=assistant_message_id,
                error=exc,
            )
            return {"status": "failed", "source_message_id": str(message_id)}

        def run() -> None:
            try:
                result = self.memory_runtime.extraction.process(
                    request,
                    context,
                    transport=transport,
                )
                self._record_memory_extraction_result(
                    database,
                    assistant_message_id=assistant_message_id,
                    result=result,
                )
            except Exception as exc:
                _ROUTING_LOG.exception(
                    "memory_extraction_failed chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                try:
                    self._record_memory_extraction_failure(
                        database,
                        assistant_message_id=assistant_message_id,
                        error=exc,
                    )
                except Exception:
                    _ROUTING_LOG.exception(
                        "memory_extraction_failure_status_write_failed chat_id=%s message_id=%s",
                        chat_id,
                        message_id,
                    )
            finally:
                # Indexing ran only on the success path, so a turn that extracted
                # nothing (or failed) left the outbox untouched.  Because the queue
                # is drained by nothing else, one such turn stranded every earlier
                # write: profiles still hold `pending` events whose records were
                # written weeks ago and were never searchable.  Draining here means
                # any turn that reaches extraction clears the backlog, whatever this
                # turn itself produced.
                self._build_memory_indexes()

        # A thread starts with a *fresh* context, not a copy of its parent's, and
        # Neo selects the profile database through a ContextVar.  Without this
        # copy every ``get_settings()`` inside ``run`` resolves the base database
        # instead of this profile's -- so extraction, and the ``_build_memory_indexes``
        # in its ``finally``, would write one person's memories into another's
        # file.  The agent worker copies its context for exactly this reason
        # (``agent_core/worker.py``); this path was simply missed, and it fires
        # once per turn, so the exposure grows with every concurrent chat.
        context = contextvars.copy_context()
        Thread(
            target=lambda: context.run(run),
            name=f"memory-extraction-{chat_id}-{message_id}",
            daemon=True,
        ).start()
        return {"status": "scheduled", "source_message_id": str(message_id)}

    @staticmethod
    def _forgetting_turn(message: ChatMessage) -> bool:
        if message.role != "assistant":
            return False
        try:
            metadata = json.loads(message.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict):
            return False
        extraction = metadata.get("memory_extraction")
        if not isinstance(extraction, dict):
            return False
        return bool(extraction.get("forgotten_memories"))

    @classmethod
    def _history_turns(cls, messages: list[ChatMessage]) -> list[ChatTurn]:
        """Replay history with the turns that removed a memory blanked out.

        A delete only means something if the deleted value stops coming back.
        Purging the record is not enough while the request that named it, and the
        reply confirming it, are still replayed into every later prompt: the
        model reads the value there and repeats it, which is how "I no longer
        remember that you use a Garmin watch" got produced.  Both halves of the
        exchange go, since either one alone restates the fact.
        """

        redacted: set[int] = set()
        for index, message in enumerate(messages):
            if not cls._forgetting_turn(message):
                continue
            redacted.add(index)
            for previous in range(index - 1, -1, -1):
                if messages[previous].role == "user":
                    redacted.add(previous)
                    break
        expired, approved = cls._calendar_offer_outcomes(messages)
        turns: list[ChatTurn] = []
        for index, message in enumerate(messages):
            if index in redacted:
                content = _FORGOTTEN_TURN_PLACEHOLDER
            elif index in expired:
                content = _EXPIRED_CALENDAR_OFFER_PLACEHOLDER
            elif index in approved:
                content = _APPROVED_CALENDAR_OFFER_PLACEHOLDER
            else:
                content = message.content + cls._shown_images_note(message)
            turns.append(ChatTurn(role=message.role, content=content))
        return turns

    @staticmethod
    def _shown_images_note(message: ChatMessage) -> str:
        """Which gallery images a replayed reply carried.

        The gallery context a reply was written from is not replayed, so without
        this a follow-up such as "which one is he swinging in?" -- no image word,
        so no new lookup -- is asked of a model that cannot tell what was shown.
        """

        if message.role != "assistant" or not message.metadata_json:
            return ""
        try:
            shown = json.loads(message.metadata_json).get("gallery_results") or []
        except (TypeError, ValueError, AttributeError):
            return ""
        titles = [str(entry.get("title") or "untitled image") for entry in shown if entry]
        if not titles:
            return ""
        return f"\n\n[Images shown with this reply, in order: {'; '.join(titles)}]"

    @staticmethod
    def _calendar_offer_outcomes(
        messages: list[ChatMessage],
    ) -> tuple[set[int], set[int]]:
        """Indexes of proposals to rewrite before generation reads them.

        Returns ``(expired, approved)``. The authority is the application's
        own record of what it did, never an inference from wording: a
        proposal carries ``status`` on its own metadata the moment its card
        is clicked, so both answers are read straight from there.

        Rows written before resolution was stamped on the proposal are still
        handled the old way -- a following ``calendar_mutation_result`` was
        the record back then -- and, as before, such a proposal is left
        untouched rather than replaced, because the mutation result that
        follows it is already the truthful account.
        """
        expired: set[int] = set()
        approved: set[int] = set()
        for index, message in enumerate(messages):
            if message.role != "assistant" or message.response_kind != "calendar_proposal":
                continue
            status = calendar_proposal_status(message)
            if status == "approved":
                approved.add(index)
                continue
            if status is not None:
                # Declined: the existing placeholder is already accurate.
                expired.add(index)
                continue
            executed = False
            for following in messages[index + 1 :]:
                if following.role != "assistant":
                    continue
                executed = following.response_kind == "calendar_mutation_result"
                break
            if not executed:
                expired.add(index)
        return expired, approved

    def _record_memory_extraction_result(
        self,
        database,
        *,
        assistant_message_id: int,
        result,
    ) -> None:
        """Attach completed extraction status to View Thinking, never to the reply."""

        decisions = tuple(getattr(result, "decisions", ()) or ())
        saved = sum(
            getattr(decision, "outcome", None) in _MEMORY_SAVE_OUTCOMES for decision in decisions
        )
        factory = sessionmaker(bind=database, autoflush=False, expire_on_commit=False, future=True)
        with factory.begin() as database_session:
            message = database_session.get(ChatMessage, assistant_message_id)
            if message is None or message.role != "assistant":
                return
            try:
                metadata = json.loads(message.metadata_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            forgotten = sum(
                getattr(decision, "outcome", None) in _MEMORY_FORGET_OUTCOMES
                for decision in decisions
            ) + getattr(self, "_foreground_forgotten", 0)
            metadata["memory_extraction"] = {
                "status": "completed",
                "saved_durable_memories": saved,
                "forgotten_memories": forgotten,
                **self._extraction_reason_metadata(result),
            }
            message.metadata_json = json.dumps(metadata, sort_keys=True)
            if saved:
                summary = (
                    f"Saved {saved} durable memor{'y' if saved == 1 else 'ies'} "
                    "after extraction and review."
                )
                current = (message.thinking or "").strip()
                message.thinking = f"{current}\n\n{summary}" if current else summary

    @staticmethod
    def _extraction_reason_metadata(result) -> dict[str, object]:
        """Summarise why a completed extraction stored nothing.

        ``saved_durable_memories: 0`` reads identically whether the turn held no
        facts, the model returned an empty response, its output failed schema
        validation, or policy rejected every candidate.  The coordinator already
        builds an ``ExtractionDiagnostic`` carrying that answer, then files it in an
        in-memory deque that is rebuilt each turn and never read, so a fully broken
        extractor looked exactly like a quiet one for weeks.

        Only bounded codes and counts are copied here, never model output or user
        text, because this lands in chat metadata the user can read.
        """

        diagnostic = getattr(result, "diagnostic", None)
        summary = getattr(result, "model_summary", None)
        if diagnostic is None:
            return {}
        reasons = tuple(getattr(diagnostic, "reason_codes", ()) or ())
        schema_errors = tuple(getattr(diagnostic, "schema_error_codes", ()) or ())
        status = getattr(result, "status", None)
        payload: dict[str, object] = {
            "extraction_status": getattr(status, "value", status),
            "parse_outcome": getattr(diagnostic, "parse_outcome", None),
            "proposal_count": getattr(diagnostic, "proposal_count", None),
            "accepted_count": getattr(diagnostic, "accepted_count", None),
            "rejected_count": getattr(diagnostic, "rejected_count", None),
            "review_count": getattr(diagnostic, "review_count", None),
        }
        if reasons:
            payload["reason_codes"] = list(reasons[:20])
        if schema_errors:
            payload["schema_error_codes"] = list(schema_errors[:20])
        if summary is not None:
            payload["model_called"] = getattr(summary, "called", None)
            payload["model_assertion_count"] = getattr(summary, "assertion_count", None)
            payload["model_exclusion_count"] = getattr(summary, "exclusion_count", None)
        return {key: value for key, value in payload.items() if value is not None}

    def _build_memory_indexes(self) -> None:
        """Index memories just written so semantic recall can actually find them.

        Extraction only enqueues indexing work; nothing in the deployed app
        drained that queue, so the vector index stayed empty and recall fell
        back to literal word overlap.  This runs on the post-turn extraction
        thread, after the response has already been delivered, and never raises:
        an indexing failure must not lose a memory that was stored correctly.
        """

        if not self.memory_enabled or self.memory_runtime is None:
            return
        execution = self.memory_runtime.execution
        engine = build_engine(execution.database_url)
        try:
            drain_memory_outbox(
                engine,
                owner_id=execution.owner_id,
                database_identity=execution.database_identity,
                flags=self.memory_runtime.settings,
                settings=self.settings,
            )
        except Exception:
            _ROUTING_LOG.exception("memory_index_build_failed")
        finally:
            engine.dispose()

    @staticmethod
    def _record_memory_extraction_failure(
        database,
        *,
        assistant_message_id: int,
        error: Exception,
    ) -> None:
        """Persist a safe failure code so extraction never remains 'scheduled'."""

        factory = sessionmaker(bind=database, autoflush=False, expire_on_commit=False, future=True)
        with factory.begin() as database_session:
            message = database_session.get(ChatMessage, assistant_message_id)
            if message is None or message.role != "assistant":
                return
            try:
                metadata = json.loads(message.metadata_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["memory_extraction"] = {
                "status": "failed",
                "error_code": type(error).__name__,
            }
            message.metadata_json = json.dumps(metadata, sort_keys=True)

    def _direct_reply(self, prompt: str) -> str | None:
        if not self.memory_enabled or not self.memory_direct_answers_enabled:
            return None
        reply = self.direct_answers.answer(
            prompt,
            context=self._memory_query_context(prompt),
        )
        self.last_memory_selection = self.direct_answers.last_selection
        return reply

    def _resolve_search_intent(
        self,
        prompt: str,
        prior_messages: list[ChatMessage],
        *,
        timezone: str | None,
        locale: str | None,
    ) -> ResolvedSearchIntent:
        """Stage A: settle what is unambiguous without calling any model.

        A bare search command searches the earlier question; a greeting or
        courtesy is answered at once; an explicit command or a fully specified
        structured request (local time, a currency conversion, a release date, an
        instruction to search the web) goes straight to its capability. Anything
        else is left with ``route=None`` for the router, which runs later and only
        if the turn gets that far (``_route_turn``).
        """

        previous = self._previous_search_intent(prior_messages)
        cleaned = " ".join((prompt or "").split())
        common: dict[str, Any] = {
            "original_query": prompt,
            "timezone": timezone,
            "locale": locale,
            "decision_source": "structured",
        }
        intent: ResolvedSearchIntent | None = None
        if is_bare_search_command(cleaned):
            question = self._previous_substantive_question(prior_messages)
            intent = ResolvedSearchIntent(
                kind=SearchIntentKind.GENERAL_WEB if question else SearchIntentKind.NONE,
                resolved_query=question or cleaned,
                reason=(
                    "Search command: searching the earlier question."
                    if question
                    else "Search command with no earlier question to search."
                ),
                confidence=0.99,
                route="follow_up" if question else "direct",
                **common,
            )
        elif is_social_pleasantry(cleaned) and not self._last_reply_asked_question(prior_messages):
            intent = ResolvedSearchIntent(
                kind=SearchIntentKind.NONE,
                resolved_query=cleaned,
                reason="Greeting or courtesy.",
                confidence=0.99,
                route="greeting",
                **common,
            )
        else:
            intent = self.search_intent_resolver.resolve_explicit(
                prompt, previous=previous, timezone=timezone, locale=locale
            )
        if intent is None:
            intent = ResolvedSearchIntent(
                kind=SearchIntentKind.NONE,
                original_query=prompt,
                resolved_query=cleaned,
                reason="Awaiting the router.",
                confidence=0.7,
                timezone=timezone,
                locale=locale,
                decision_source="fallback",
                route=None,
            )
        self.last_search_intent = intent
        return intent

    def _route_turn(
        self,
        prompt: str,
        prior_messages: list[ChatMessage],
        intent: ResolvedSearchIntent,
        *,
        timezone: str | None,
        locale: str | None,
        message_id: int | None,
    ) -> ResolvedSearchIntent:
        """Stage B: let the router choose the capability, if Stage A did not.

        Called at the point where the capability is needed, after every earlier
        exit (rules, guidance, internal commands, a pending calendar proposal,
        the gallery), so a turn that ends earlier never pays for it. The final
        intent replaces the provisional one stored on the user message.
        """

        if intent.route is not None:
            return intent
        started = time.perf_counter()
        routed = self.search_intent_resolver.route_with_model(
            prompt,
            llm=self.helper_llm,
            context=self._router_context(prior_messages),
            previous=self._previous_search_intent(prior_messages),
            timezone=timezone,
            locale=locale,
            routes=self._enabled_routes(),
        )
        self._timings["router_ms"] = int((time.perf_counter() - started) * 1000)
        self._timings["router"] = dict(self.search_intent_resolver.last_router or {})
        if message_id is not None:
            self._store_search_intent(message_id, routed)
        self.last_search_intent = routed
        return routed

    def _enabled_routes(self) -> tuple[str, ...]:
        """The capabilities the router may choose between for this turn."""

        routes = ["direct", "calendar", "weather"]
        if self.settings.web_search_enabled:
            routes.append("web")
        if self.memory_enabled:
            routes.append("memory")
        return tuple(routes)

    def _router_context(self, prior_messages: list[ChatMessage]) -> str | None:
        """The previous exchange, short, for resolving references.

        Built from ``_history_turns`` so it carries the same redactions the model
        sees: a forgotten memory or an expired calendar offer is never shown to
        the router either.
        """

        turns = self._history_turns(prior_messages)
        last_reply_index = next(
            (i for i in range(len(turns) - 1, -1, -1) if turns[i].role == "assistant"), None
        )
        if last_reply_index is None:
            return None
        asked = next(
            (turns[i] for i in range(last_reply_index - 1, -1, -1) if turns[i].role == "user"),
            None,
        )
        lines = []
        if asked is not None:
            lines.append(f"User earlier: {_excerpt(asked.content)}")
        lines.append(
            f"Neo replied: {_excerpt(_strip_llm_sources_block(turns[last_reply_index].content))}"
        )
        used = _capability_used(
            next((m for m in reversed(prior_messages) if m.role == "assistant"), None)
        )
        if used:
            lines.append(f"That reply used: {used}.")
        return "\n".join(lines)

    def _store_search_intent(self, message_id: int, intent: ResolvedSearchIntent) -> None:
        """Record the final routing decision on the user message it belongs to."""

        message = self.db.get(ChatMessage, message_id)
        if message is None:
            return
        try:
            metadata = json.loads(message.metadata_json or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["search_intent"] = intent.model_dump(mode="json")
        message.metadata_json = json.dumps(metadata, sort_keys=True)
        self.db.commit()

    @staticmethod
    def _previous_substantive_question(prior_messages: list[ChatMessage]) -> str | None:
        """The most recent user message that asked for something."""

        for message in reversed(prior_messages):
            if message.role != "user":
                continue
            text = " ".join((message.content or "").split())
            if (
                not text
                or is_bare_search_command(text)
                or is_social_pleasantry(text)
                or _SHORT_AGREEMENT.match(text)
            ):
                continue
            return text
        return None

    @staticmethod
    def _last_reply_asked_question(prior_messages: list[ChatMessage]) -> bool:
        """Whether Neo's last reply ended by asking the user something."""

        for message in reversed(prior_messages):
            if message.role == "assistant":
                body = _strip_llm_sources_block(message.content or "").rstrip()
                lines = [line.strip() for line in body.splitlines() if line.strip()]
                return bool(lines) and lines[-1].endswith("?")
            if message.role == "user":
                return False
        return False

    def _search_options(self) -> SearchOptions:
        """Research depth for this turn's effort. Routing is unaffected.

        ``max_seconds`` bounds the whole search, providers and page fetches
        together, so an unreachable or slow provider cannot hold a turn for
        minutes; whatever was found by then is used.
        """

        settings = self.settings
        if self.reasoning:
            return SearchOptions(
                max_results=settings.web_search_max_results_high,
                max_pages=settings.web_fetch_max_pages_high,
                max_context_tokens=settings.web_context_max_tokens_high,
                max_seconds=35,
            )
        return SearchOptions(
            max_results=settings.web_search_max_results,
            max_pages=settings.web_fetch_max_pages,
            max_context_tokens=settings.web_context_max_tokens,
            max_seconds=25,
        )

    def _web_query_for(self, prompt: str, intent: ResolvedSearchIntent) -> tuple[str, str | None]:
        """The text to search and the planning hint, for a turn that searches.

        A follow-up searches the earlier question, and a router decision searches
        the standalone query the router wrote; an explicit command or a
        structured request searches the message itself, as it always did.
        """

        if intent.route == "follow_up" or (
            intent.decision_source == "model" and intent.kind is SearchIntentKind.GENERAL_WEB
        ):
            return intent.resolved_query or prompt, None
        return prompt, intent.resolved_query

    def _answer_reasons(self, intent: ResolvedSearchIntent | None) -> bool:
        """High reasons on every substantive answer; a greeting needs none."""

        return self.reasoning and not (intent is not None and intent.route == "greeting")

    def _answer_client(self, intent: ResolvedSearchIntent | None) -> LLMClient:
        if self.reasoning and not self._answer_reasons(intent):
            return self.quick_llm
        return self.ollama

    def _note_unrequested_reasoning(self, llm: LLMClient, thinking: str | None) -> None:
        """Record that the model reasoned although this answer asked it not to.

        Some models cannot switch reasoning off. The user's effort setting is
        left alone; the flag only lets the effort control say why Low is slower
        with this model. It is set from what the model did, never assumed.
        """

        if getattr(llm, "disable_thinking", False) and (thinking or "").strip():
            self._timings["model_always_reasons"] = True

    def _clients(self) -> list[LLMClient]:
        seen: list[LLMClient] = []
        for client in (self.helper_llm, self.calendar_llm, self.quick_llm, self.ollama):
            if all(client is not other for other in seen):
                seen.append(client)
        return seen

    def _timings_snapshot(self, intent: ResolvedSearchIntent | None = None) -> dict[str, Any]:
        """What this turn spent, for the message metadata and benchmarks."""

        data = dict(self._timings)
        data["effort"] = self.effort
        data["total_ms"] = self._turn_duration_ms()
        if intent is not None:
            data["route"] = intent.route or (
                "direct" if intent.kind is SearchIntentKind.NONE else intent.kind.value
            )
        marks = getattr(self, "_request_marks", {})
        request_ids: list[str] = []
        helper_calls = 0
        for client in self._clients():
            ids = list(getattr(client, "request_ids", []) or [])[marks.get(id(client), 0) :]
            request_ids.extend(ids)
            if client is not self.ollama:
                helper_calls += len(ids)
        data["request_ids"] = request_ids
        data["model_calls"] = len(request_ids)
        data["helper_calls"] = helper_calls
        return data

    def _classify_answer_failure(
        self,
        reply: str,
        *,
        finish_reason: str | None,
        intent: ResolvedSearchIntent,
        web_context: WebContext,
        gallery: GalleryMatches | None,
    ) -> str | None:
        """Why a finished answer failed, from signals already in hand, or ``None``.

        Only two failures are acted on after generation. A cut-off answer has
        already been continued by the generation loop, and tool failures, missing
        parameters and unsupported actions are answered by their capability
        before any generation runs.

        * ``empty``: the model stopped without writing anything visible.
        * ``knowledge_gap``: the reply is short and says the model lacks the
          current or specific information the question needs, on a turn that did
          not search and could have. Only this one leads to a search -- a weak
          answer for any other reason is not something a search can fix.
        """

        if finish_reason in {"length", "incomplete_length"}:
            return None
        text = (reply or "").strip()
        if not text:
            return "empty"
        if (
            gallery is None
            and not web_context.needed
            and intent.route in {"direct", "memory"}
            and self.settings.web_search_enabled
            and len(text) <= _KNOWLEDGE_GAP_MAX_CHARS
            and _KNOWLEDGE_GAP.search(text[:_KNOWLEDGE_GAP_SCAN_CHARS])
        ):
            return "knowledge_gap"
        return None

    def _previous_search_intent(
        self,
        messages: list[ChatMessage],
    ) -> ResolvedSearchIntent | None:
        for message in reversed(messages):
            if message.role != "user":
                continue
            if message.metadata_json:
                try:
                    payload = json.loads(message.metadata_json)
                    return ResolvedSearchIntent.model_validate(payload["search_intent"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    pass
            # Backward compatibility for the most recent turn created before
            # structured intent metadata existed.
            return resolve_search_intent(message.content)
        return None

    def _profile_timezone(self) -> str | None:
        return None

    def _structured_live_answer(
        self,
        prompt: str,
        intent: ResolvedSearchIntent,
        *,
        timezone: str | None,
        locale: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        started = time.perf_counter()
        if intent.kind == SearchIntentKind.CONNECTOR_TOOL:
            # Explaining the limitation is the honest answer: chat replies have no
            # connector runner, and generating anyway let the model describe a
            # call that never happened.
            return _CONNECTOR_UNSUPPORTED_REPLY, {
                "response_kind": "unsupported_action",
                "provider_name": "Neo",
                "route_name": "connector",
                "finish_reason": "unsupported",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {"search_intent": intent.model_dump(mode="json")},
            }
        if intent.kind == SearchIntentKind.WEATHER and not intent.location:
            # The one parameter a weather lookup cannot do without: ask for it
            # rather than guessing a place or answering without data.
            return _WEATHER_PLACE_QUESTION, {
                "response_kind": "structured_weather",
                "provider_name": "Neo",
                "route_name": "weather",
                "finish_reason": "needs_parameter",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {"search_intent": intent.model_dump(mode="json")},
            }
        if intent.kind == SearchIntentKind.LOCAL_DATETIME:
            result = local_datetime_answer(
                prompt,
                browser_timezone=timezone,
                profile_timezone=self._profile_timezone(),
                fallback_timezone=self.settings.default_timezone,
                locale=locale,
            )
            return result.answer, {
                "response_kind": "local_datetime",
                "provider_name": "Neo local clock",
                "route_name": "local_datetime",
                "finish_reason": "stop",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {
                    "timezone": result.timezone,
                    "locale": result.locale,
                    "used_web": False,
                    "search_intent": intent.model_dump(mode="json"),
                },
            }

        try:
            if intent.kind == SearchIntentKind.CURRENCY:
                if not intent.from_currency or not intent.to_currency or intent.amount is None:
                    return None
                quote = FrankfurterClient().convert(
                    intent.amount,
                    intent.from_currency,
                    intent.to_currency,
                )
                reply = (
                    f"{quote.amount} {quote.from_currency} is "
                    f"{quote.converted_amount:,.2f} {quote.to_currency}. "
                    f"The rate is 1 {quote.from_currency} = {quote.rate} "
                    f"{quote.to_currency}, dated {quote.reference_date}, from "
                    f"{quote.provider}.\n\nSource: {quote.source_url}"
                )
                return reply, {
                    "response_kind": "structured_currency",
                    "provider_name": quote.provider,
                    "route_name": "currency",
                    "finish_reason": "stop",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "metadata": {
                        "quote": quote.model_dump(mode="json"),
                        "search_intent": intent.model_dump(mode="json"),
                    },
                }

            if intent.kind == SearchIntentKind.WEATHER:
                if not intent.location:
                    return None
                weather = OpenMeteoClient()
                if intent.date == "tomorrow":
                    report = weather.forecast_weather(
                        intent.location,
                        day="tomorrow",
                        locale=locale or "en",
                        timezone=timezone or "auto",
                    )
                    place = ", ".join(item for item in (report.location, report.country) if item)
                    rain = (
                        f" The maximum precipitation probability is "
                        f"{report.precipitation_probability_max}%."
                        if report.precipitation_probability_max is not None
                        else ""
                    )
                    reply = (
                        f"The forecast for {place} on {report.forecast_date} is "
                        f"{report.condition}, with a low of {report.temperature_min_c}°C "
                        f"and a high of {report.temperature_max_c}°C.{rain} "
                        f"Provided by {report.provider}.\n\nSource: {report.source_url}"
                    )
                else:
                    report = weather.current_weather(
                        intent.location,
                        locale=locale or "en",
                        timezone=timezone or "auto",
                    )
                    apparent = (
                        f", feels like {report.apparent_temperature_c}°C"
                        if report.apparent_temperature_c is not None
                        else ""
                    )
                    place = ", ".join(item for item in (report.location, report.country) if item)
                    reply = (
                        f"In {place}, it is {report.temperature_c}°C{apparent} with "
                        f"{report.condition}. This observation is from "
                        f"{report.observed_at} ({report.timezone}), provided by "
                        f"{report.provider}.\n\nSource: {report.source_url}"
                    )
                return reply, {
                    "response_kind": "structured_weather",
                    "provider_name": report.provider,
                    "route_name": "weather",
                    "finish_reason": "stop",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "metadata": {
                        "weather": report.model_dump(mode="json"),
                        "search_intent": intent.model_dump(mode="json"),
                    },
                }
        except LiveDataError as exc:
            provider = "Frankfurter" if intent.kind == SearchIntentKind.CURRENCY else "Open-Meteo"
            return str(exc), {
                "response_kind": (
                    "structured_currency"
                    if intent.kind == SearchIntentKind.CURRENCY
                    else "structured_weather"
                ),
                "provider_name": provider,
                "route_name": intent.kind.value,
                "finish_reason": "provider_error",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "metadata": {
                    "search_intent": intent.model_dump(mode="json"),
                    "error": str(exc),
                },
            }
        return None

    def _adopt_calendar_execution(self) -> None:
        """Take the calendar layer's report of this turn as the turn's state.

        ``CalendarContextService`` knows whether its ``None`` meant "not a
        calendar message" or "a calendar change I refused to complete"; only
        this class knows what the whole turn did. Adopting rather than
        overwriting keeps a mutation already recorded by the confirmation
        path authoritative -- a failure to classify afterwards cannot erase
        a write that really happened.
        """
        if self.calendar_execution == "none":
            self.calendar_execution = self.calendar_context.last_execution

    def _proposal_reask(
        self, pending: PendingCalendarProposal, message: str
    ) -> tuple[str, dict[str, Any]]:
        """Point back at the card instead of acting on the proposal.

        Nothing typed can approve or decline -- the card's buttons are the
        only path, and they resolve the proposal message itself. So a "yes",
        a "no", or anything unplaceable gets an answer that says where the
        decision actually lives, and the draft is left exactly as it was.

        Crucially this does *not* re-persist the proposal. The older
        behaviour echoed the whole draft back under
        ``response_kind="calendar_proposal"`` to keep it reachable, which
        drew a second identical card every time and re-armed the same
        misclassification on the next message -- the loop that showed three
        dentist cards in a row. The reference below keeps the proposal
        reachable for one more typed message without duplicating anything,
        and ``MAX_REASKS`` bounds how long it can do so.
        """
        return message, {
            "response_kind": PROPOSAL_REASK_KIND,
            "metadata": {
                "calendar_proposal_ref": {
                    "message_id": pending.source_message_id,
                    "reask_count": pending.reask_count + 1,
                }
            },
        }

    def _handle_pending_calendar_reply(
        self,
        prompt: str,
        *,
        chat_id: int,
        history: list[ChatMessage],
        llm: LLMClient | None,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """What a typed reply means while a proposal is on screen.

        This method cannot write to the calendar, and no longer contains a
        path that could: approving is a click on the proposal card, which
        goes to ``POST /calendar/proposals/{id}/approve`` and is the only
        caller of ``execute_calendar_proposal``. The single thing decided
        here is whether the reply is an *edit* of the draft on screen -- the
        one case where Neo should build something new from it.

        Everything else either lets go of the turn (``new_request``,
        ``unrelated``: a fresh classification runs, which is how "schedule a
        haircut" stops being read as an edit to a dentist appointment) or
        points at the card without touching the draft.
        """
        pending = find_pending_calendar_proposal(history)
        if pending is None:
            return None
        decision = resolve_pending_action_reply(prompt, pending=pending, llm=llm)
        self._routing_diagnostic(
            chat_id,
            prompt,
            message_id=pending.source_message_id,
            selected_route="pending_action",
            component="pending_action_confirmation",
            matched_intent=pending.action,
            confidence=decision.confidence,
            final_status=decision.outcome,
            extra={
                "pending_action_outcome": decision.outcome,
                "reask_count": pending.reask_count,
            },
        )

        if decision.outcome in ("new_request", "unrelated"):
            # The user has moved on. Releasing the turn lets the ordinary
            # classifier read the message on its own terms; the proposal
            # retires by itself, since whatever answers now becomes the most
            # recent message and the old card is not referenced.
            return None

        title = (pending.draft or {}).get("title") or pending.event_title or "that event"
        summary = describe_calendar_draft(pending.action, title, pending.draft)

        if decision.outcome == "modify":
            # The old draft is never executed -- but the proposal the user is
            # looking at is what they're changing, so it is carried through as
            # the baseline rather than re-discovered from the calendar. The
            # result is always a fresh proposal needing its own approval.
            modified = self.calendar_context.handle_proposal_modification(
                prompt,
                llm=llm,
                timezone=timezone,
                locale=locale,
                action=pending.action,
                event_id=pending.event_id,
                event_title=pending.event_title,
                draft=pending.draft,
            )
            if modified is not None:
                return modified
            # Fail closed *inside* the pending-proposal flow. Falling through
            # to the ordinary context-free classification is exactly what let
            # a bare fragment get matched against an unrelated calendar event,
            # so an undetermined modification never leaves this branch.
            return self._proposal_reask(
                pending,
                f"I couldn't tell what you'd like to change. {summary} "
                "Tell me what to change and I'll update it, or use the buttons "
                "on the proposal above.",
            )

        if decision.outcome == "confirm":
            return self._proposal_reask(
                pending,
                f"{summary} Use **Approve** on the proposal above and I'll make the "
                "change -- that button is the only thing that can.",
            )
        if decision.outcome == "decline":
            return self._proposal_reask(
                pending,
                f"Use **Decline** on the proposal above and I'll leave **{title}** alone.",
            )
        # Ambiguous: re-ask rather than guess, and say where the decision lives.
        return self._proposal_reask(
            pending,
            f"{summary} Use **Approve** or **Decline** on the proposal above, "
            "or tell me what to change.",
        )

    def _routing_diagnostic(
        self,
        chat_id: int,
        prompt: str,
        *,
        message_id: int | None,
        selected_route: str,
        component: str,
        matched_intent: str | None = None,
        confidence: float | None = None,
        fuzzy_candidate: str | None = None,
        direct_feature_service: str | None = None,
        provider_invoked: bool = False,
        provider: str | None = None,
        model: str | None = None,
        fallback_reason: str | None = None,
        response_source: str | None = None,
        final_status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emit a privacy-safe trace for the production chat-routing decision.

        ``extra`` carries additional decision-specific fields (pending-action
        outcome, whether/why a refinement pass ran, ...) without widening the
        fixed field list every caller has to pass -- the same logger and
        payload shape, just with a few more distinctly-named keys merged in.
        """

        normalized = re.sub(r"\s+", " ", (prompt or "").strip())
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "normalized_input_length": len(normalized),
            "input_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            "selected_route": selected_route,
            "component": component,
            "matched_intent": matched_intent,
            "confidence": confidence,
            "fuzzy_candidate": fuzzy_candidate,
            "direct_feature_service": direct_feature_service,
            "provider_invoked": provider_invoked,
            "provider": provider,
            "model": model,
            "fallback_reason": fallback_reason,
            "response_source": response_source,
            "final_status": final_status,
        }
        if extra:
            payload.update(extra)
        self.last_routing_debug = payload
        _ROUTING_LOG.warning("chat_routing=%s", json.dumps(payload, sort_keys=True))
        return payload

    def _web_query_with_memory_region(self, query: str, context: ContextPackage) -> str:
        """Return the query unchanged; stored memory never reaches a search engine.

        This used to append the user's country, read out of memory, to release
        date lookups.  It sharpened a narrow class of query at the cost of
        sending a personal fact to a third-party search provider on a turn where
        the user asked only about a release date.  Memory is stored locally so it
        stays the user's; a small ranking gain does not justify spending it.
        """

        return query

    def _is_release_date_query(self, query: str) -> bool:
        return resolve_search_intent(query).kind == SearchIntentKind.RELEASE_DATE

    def _compact_context(self, context: ContextPackage) -> str:
        lines: list[str] = []
        lines.extend(f"profile: {item.key} = {item.value}" for item in context.profile)
        lines.extend(
            f"preference: {item.category} = {item.value} (importance {item.importance})"
            for item in context.preferences
        )
        lines.extend(
            f"goal: {item.goal}" + (f" - {item.description}" if item.description else "")
            for item in context.goals
        )
        lines.extend(
            f"project: {item.name}" + (f" - {item.description}" if item.description else "")
            for item in context.projects
        )
        lines.extend(f"memory #{item.id}: {item.memory_text}" for item in context.relevant_memories)
        lines.extend(f"event: {item.event}" for item in context.events)
        if not lines:
            return "No relevant personal memory loaded."
        return "\n".join(lines[:18])

    def _compact_web_context(self, web_context: WebContext | None) -> str:
        if web_context is None or not web_context.needed:
            return "No web context loaded."
        if web_context.warning and not web_context.context_text:
            if "disabled" in web_context.warning.lower():
                return "Web search is disabled; no search was run."
            return f"Web search attempted but unavailable: {web_context.warning}"
        if not web_context.context_text:
            return "Web search ran, but no usable page text was fetched."
        return web_context.context_text

    def _web_failure_reply(self, web_context: WebContext | None) -> str | None:
        if web_context is None or not web_context.needed:
            return None
        if web_context.citations:
            return None
        if "disabled" in (web_context.warning or "").lower():
            return "I can't check that online right now because web search is turned off."
        return "I searched the web, but couldn't find enough reliable information to answer that."

    def _web_response_kind(self, web_context: WebContext | None) -> str:
        """Only label a reply as web search when a search could actually run."""

        if (
            web_context is not None
            and web_context.needed
            and self.settings.web_search_enabled
            and "disabled" not in (web_context.warning or "").lower()
        ):
            return "web_search"
        return "normal_chat"

    def _with_web_citations(self, reply: str, web_context: WebContext | None) -> str:
        body = _strip_llm_sources_block(reply)
        if web_context is None or not web_context.needed:
            # No web search was involved, so a link here is ordinary content, a repo or
            # documentation URL the user asked for, not a citation claiming evidence.
            # Deleting those corrupted normal answers, so they are left alone.
            return self._strip_orphan_citation_markers(body)
        if not web_context.citations:
            # A search was attempted but produced nothing citable, so any source URL the
            # model offers cannot be backed by evidence and is dropped.
            body = self._strip_orphan_citation_markers(body)
            return _strip_fabricated_urls(body, set())
        valid_urls = {citation.url for citation in web_context.citations}
        body = _strip_fabricated_urls(body, valid_urls)
        citations = self._citations_for_body(body, web_context)
        if not citations:
            return self._strip_orphan_citation_markers(body)
        return f"{body.strip()}\n\n{citations}"

    def _citations_for_body(self, body: str, web_context: WebContext) -> str:
        """Format only the sources the answer actually cites.

        Listing every fetched page implies the answer rests on all of them. It
        also reads as padding: a four-line answer citing [1] and [2] would still
        print a third source it never used. Indices are left as they are rather
        than renumbered, so the markers already written in the body stay valid
        and a gap simply means that source went uncited.
        """
        referenced = _referenced_citation_indices(body)
        cited = [citation for citation in web_context.citations if citation.index in referenced]
        # An uncited answer should not silently lose its sources; callers guard
        # this path with _has_web_citation_marker, so this is belt and braces.
        return self.citation_formatter.format_citations(cited or web_context.citations)

    def _strip_orphan_citation_markers(self, reply: str) -> str:
        cleaned = re.sub(r"\s*\[(?:\d{1,2})(?:\s*,\s*\d{1,2})*\]", "", reply)
        cleaned = re.sub(r" {2,}", " ", cleaned)
        return cleaned.strip()

    def _has_web_citation_marker(self, reply: str, web_context: WebContext) -> bool:
        # Markers must be in the answer itself; a Sources block the model wrote
        # proves nothing about which claims the evidence supports.
        validation = validate_citation_markers(
            _strip_llm_sources_block(reply),
            web_context.citations,
            supported_indices={chunk.source_index for chunk in web_context.evidence_chunks},
            require_marker=True,
        )
        if not validation.valid:
            self._timings.setdefault("citation_errors", validation.errors)
        return validation.valid

    def _cited_web_reply(
        self,
        prompt: str,
        draft: str,
        web_context: WebContext,
        messages: list[LLMMessage],
        *,
        budget: int,
    ) -> str | None:
        """A web answer whose claims carry verified citations, or what to do instead.

        A small model often answers correctly from the evidence but leaves out
        the [n] markers that tie each claim to its source. That is a citation
        failure, not a knowledge failure, so the recovery is to ask for the
        citations -- once, without reasoning, since nothing new is being worked
        out -- before giving up on the answer.

        ``None`` means the sources found do not answer the question: the model
        said so, or could not tie its answer to them even when asked. Anything
        but a single-fact lookup is then answered as though the search found
        nothing usable, which says so and still helps; showing the user raw
        passages from off-topic pages helps no one.
        """

        if self._has_web_citation_marker(draft, web_context):
            return self._with_web_citations(draft, web_context)
        sources_lack_answer = bool(_SOURCES_LACK_ANSWER.search(draft))
        if draft.strip() and not sources_lack_answer:
            started = time.perf_counter()
            repair = [
                *messages,
                LLMMessage(role="assistant", content=draft),
                LLMMessage(role="user", content=_CITATION_REPAIR_REQUEST),
            ]
            try:
                result = self._generate_complete(repair, num_predict=budget, llm=self.quick_llm)
                repaired = self.quick_llm.clean_response(result.content)
            except Exception:
                repaired = ""
            cited = bool(repaired.strip()) and self._has_web_citation_marker(repaired, web_context)
            self._timings["citation_repair"] = {
                "result": "cited" if cited else "failed",
                "ms": int((time.perf_counter() - started) * 1000),
            }
            if cited:
                return self._with_web_citations(repaired, web_context)
        if draft.strip() and web_context.answer_mode != "fact_lookup":
            self._timings["sources_unused"] = (
                "model_said_unanswered" if sources_lack_answer else "uncitable"
            )
            return None
        return self._web_generation_fallback(
            prompt, web_context, RuntimeError("generated web answer lacked citation markers")
        )

    def _web_generation_fallback(
        self, prompt: str, web_context: WebContext, error: Exception
    ) -> str:
        grounded_prompt = web_context.query or prompt
        if web_context.answer_mode == "fact_lookup":
            release_answer = self._verified_release_answer(grounded_prompt, web_context)
            if release_answer is not None:
                return release_answer
            fact = run_extractors(grounded_prompt, web_context.evidence_chunks)
            if fact is not None:
                answer = self._format_fact_answer(grounded_prompt, fact)
                citations = self.citation_formatter.format_citations(
                    [
                        citation
                        for citation in web_context.citations
                        if citation.index == fact.source_index
                    ]
                )
                return f"{answer}\n\n{citations}" if citations else answer
            return (
                "I searched the web but could not find sufficiently reliable "
                "evidence to answer that."
            )
        if web_context.answer_mode in {"news_summary", "overview"}:
            return self._evidence_digest(grounded_prompt, web_context)
        return (
            "I searched the web but could not find sufficiently reliable evidence to answer that."
        )

    def _direct_web_reply(self, prompt: str, web_context: WebContext) -> str | None:
        """Answer a single-fact lookup straight from the evidence, when that is safe.

        A verified release date answers through its own extractor. Any other fact
        type answers without the model only when independent sources agree on the
        extracted value; one source, or sources that disagree, go to generation
        from the cited evidence instead, which can say which variant or source
        each figure belongs to.
        """

        if not web_context.needed or not web_context.evidence_chunks or not web_context.citations:
            return None
        if web_context.answer_mode != "fact_lookup":
            # news_summary and overview deliberately fall through to the model.
            # Rendering the evidence chunks straight out reads as extracted
            # snippets rather than an answer; summarising them is what the model
            # is for. The deterministic rendering is kept in _evidence_digest and
            # used by _web_generation_fallback when generation fails.
            return None
        release_answer = self._verified_release_answer(prompt, web_context)
        if release_answer is not None:
            return release_answer
        fact = run_extractors(prompt, web_context.evidence_chunks)
        if fact is None or not _corroborated(fact, web_context):
            return None
        answer = self._format_fact_answer(prompt, fact)
        citations = self.citation_formatter.format_citations(
            [citation for citation in web_context.citations if citation.index == fact.source_index]
        )
        return f"{answer}\n\n{citations}" if citations else answer

    def _recover_answer(
        self,
        failure: str,
        prompt: str,
        *,
        messages: list[LLMMessage],
        context: ContextPackage,
        history: list[ChatTurn],
        project_context: str | None,
        task_context: str | None,
        llm: LLMClient,
        budget: int,
        web_context: WebContext,
    ) -> dict[str, Any]:
        """The one bounded recovery for a failed answer, on the non-streaming path.

        ``knowledge_gap`` searches the original question once and answers from
        what is found; ``empty`` regenerates once with the same inputs. Nothing
        here runs again for the same turn. Returns the new ``reply`` (or ``None``
        when nothing better was found), the ``web_context`` it used, the model
        ``result`` and a ``recovery`` record for diagnostics.
        """

        if failure == "knowledge_gap":
            web_context = self.web_search.build_context_forced(
                prompt, options=self._search_options()
            )
            direct = self._direct_web_reply(prompt, web_context)
            if direct is not None:
                return {
                    "reply": direct,
                    "web_context": web_context,
                    "recovery": {"action": "search", "result": "direct"},
                }
            if self._web_failure_reply(web_context) is not None:
                return {"reply": None, "recovery": {"action": "search", "result": "search_failed"}}
            recovery_messages = self.build_messages(
                prompt,
                history,
                context,
                web_context,
                project_context,
                task_context,
                calendar_execution=self.calendar_execution,
            )
        else:
            # Same inputs, same evidence: an empty answer on a turn that searched
            # is regenerated from that search, and keeps its citations.
            recovery_messages = messages
        try:
            result = self._generate_complete(recovery_messages, num_predict=budget, llm=llm)
        except Exception:
            return {"reply": None, "recovery": {"action": "regenerate", "result": "provider_error"}}
        content = result.content
        if not content.strip() or result.finish_reason == "length":
            return {
                "reply": None,
                "recovery": {
                    "action": "search" if failure == "knowledge_gap" else "regenerate",
                    "result": "unchanged",
                },
            }
        if web_context is not None and web_context.citations:
            reply = (
                self._web_generation_fallback(
                    prompt, web_context, RuntimeError("recovered answer lacked citation markers")
                )
                if not self._has_web_citation_marker(content, web_context)
                else self._with_web_citations(content, web_context)
            )
        else:
            reply = content
        return {
            "reply": reply,
            "web_context": web_context,
            "result": result,
            "recovery": {
                "action": "search" if failure == "knowledge_gap" else "regenerate",
                "result": "generated",
            },
        }

    def _evidence_digest(self, prompt: str, web_context: WebContext) -> str:
        """Render evidence chunks directly, for when generation cannot be used."""
        lines = [
            "Here are the source-backed updates I found:"
            if web_context.answer_mode == "news_summary"
            else "Here is what the sources say:"
        ]
        chunks = sorted(web_context.evidence_chunks, key=lambda chunk: -chunk.relevance_score)
        for chunk in chunks[:4]:
            lines.append(f"- {_clean_snippet_text(chunk.text[:420])} [{chunk.source_index}]")
        citations = self._citations_for_body("\n".join(lines), web_context)
        if citations:
            lines.extend(["", citations])
        return "\n".join(lines)

    def _format_fact_answer(self, prompt: str, fact: FactResult) -> str:
        """A short, cited answer from one extracted fact, in domain-neutral words.

        The supporting sentence is quoted so the reader sees what the figure
        refers to -- which variant, which region, which date -- in the source's
        own words, instead of Neo guessing a label from the question.
        """

        answer = f"{fact.answer} [{fact.source_index}]."
        support = _clean_snippet_text(fact.support_text or "").strip()
        if support and support.lower() != str(fact.answer).lower():
            if len(support) > 240:
                support = support[:239].rstrip() + "\u2026"
            return f'{answer}\n\nFrom the source: "{support}"'
        return answer

    def _verified_release_answer(
        self,
        prompt: str,
        web_context: WebContext,
    ) -> str | None:
        """Return a release answer only through the shared verified extractor."""

        if not self._is_release_date_query(prompt):
            return None
        fact = extract_release_date(prompt, web_context.evidence_chunks)
        if fact is None:
            answer = (
                "The fetched sources did not provide a release date that passed "
                "verification, so I cannot report a verified date yet."
            )
            citations = self.citation_formatter.format_citations(web_context.citations)
            return f"{answer}\n\n{citations}" if citations else answer

        answer = f"The verified release date is {fact.answer} [{fact.source_index}]."
        citations = self.citation_formatter.format_citations(
            [citation for citation in web_context.citations if citation.index == fact.source_index]
        )
        return f"{answer}\n\n{citations}" if citations else answer

    def _web_debug(
        self,
        web_context: WebContext | None,
        context: ContextPackage | None = None,
        web_context_in_prompt: bool = False,
        final_answer: str | None = None,
    ) -> dict[str, Any]:
        search = web_context.search if web_context is not None else None
        memory_context_loaded = False
        if context is not None:
            memory_context_loaded = bool(
                context.profile
                or context.preferences
                or context.goals
                or context.projects
                or context.relevant_memories
                or context.events
                or context.archive_results
            )
        return {
            "web_search_needed": bool(web_context and web_context.needed),
            "web_search_provider": search.provider
            if search is not None
            else self.web_search.provider.name,
            "web_provider_query": search.provider_query if search is not None else None,
            "web_search_called": search is not None,
            "web_decision_warning": web_context.warning if web_context is not None else None,
            "web_results_count": len(search.results) if search is not None else 0,
            "web_provider_attempts": search.provider_attempts if search is not None else [],
            "web_rejected_results": (
                [
                    {
                        "title": result.title,
                        "url": result.url,
                        "reason": "not_selected_for_verified_evidence",
                    }
                    for result in search.results
                    if web_context is not None
                    and result.url
                    not in {selected.url for selected in web_context.selected_results}
                ]
                if search is not None
                else []
            ),
            "web_results": (
                [
                    {
                        "rank": result.rank,
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "relevance_score": result.relevance_score,
                        "relevance_reasons": result.relevance_reasons,
                    }
                    for result in search.results[:10]
                ]
                if search is not None
                else []
            ),
            "web_selected_results": (
                [
                    {
                        "rank": result.rank,
                        "title": result.title,
                        "url": result.url,
                        "relevance_score": result.relevance_score,
                        "relevance_reasons": result.relevance_reasons,
                    }
                    for result in web_context.selected_results[:10]
                ]
                if web_context is not None
                else []
            ),
            "web_selected_results_count": len(web_context.selected_results)
            if web_context is not None
            else 0,
            "web_fetched_count": (
                sum(1 for page in web_context.pages if page.fetched)
                if web_context is not None
                else 0
            ),
            "web_fetched_pages": (
                [
                    {
                        "url": page.url,
                        "title": page.title,
                        "text_length": len(page.text),
                        "fetched": page.fetched,
                        "error": page.error,
                    }
                    for page in web_context.pages
                ]
                if web_context is not None
                else []
            ),
            "web_sources_count": len(web_context.citations) if web_context is not None else 0,
            "web_context_length": len(web_context.context_text) if web_context is not None else 0,
            "web_evidence_chunks_count": len(web_context.evidence_chunks)
            if web_context is not None
            else 0,
            "web_evidence": (
                [
                    {
                        "source_index": chunk.source_index,
                        "source_url": chunk.source_url,
                        "relevance_score": chunk.relevance_score,
                        "text": chunk.text[:500],
                    }
                    for chunk in web_context.evidence_chunks
                ]
                if web_context is not None
                else []
            ),
            "web_citation_decisions": (
                [
                    {
                        "index": citation.index,
                        "url": citation.url,
                        "fetched": citation.fetched,
                        "accepted": True,
                    }
                    for citation in web_context.citations
                ]
                if web_context is not None
                else []
            ),
            "web_freshness": {
                "required": bool(
                    web_context
                    and re.search(
                        r"\b(?:latest|current|today|recent|newest|right now)\b",
                        web_context.query,
                        re.IGNORECASE,
                    )
                ),
                "published_dates": (
                    [
                        result.published_date
                        for result in web_context.selected_results
                        if result.published_date
                    ]
                    if web_context is not None
                    else []
                ),
            },
            "web_answer_mode": web_context.answer_mode if web_context is not None else None,
            "memory_context_loaded": memory_context_loaded,
            "web_context_entered_final_prompt": web_context_in_prompt,
            "final_answer_included_sources": bool(final_answer and "Sources:" in final_answer),
        }

    #: Tokens an English word costs. Measured against local models: a 1500-word answer
    #: ran ~2400 tokens on qwen3-coder and overran 2550 on the wordier gemma4, so this
    #: carries deliberate headroom. Overshooting costs nothing -- generation stops at the
    #: end of the answer -- while undershooting truncates mid-sentence.
    _TOKENS_PER_WORD = 2.3
    #: Ceiling for an explicitly requested length, so one prompt cannot exhaust the context.
    _MAX_REQUESTED_PREDICT = 6144

    @staticmethod
    def _requested_output_tokens(prompt: str) -> int | None:
        """Tokens needed for an explicit length request such as "in about 1500 words".

        Without this every long-form request is capped at ``chat_num_predict`` and comes
        back truncated no matter how many continuations run.
        """
        match = re.search(r"\b(\d{2,5})\s*[- ]?\s*words?\b", prompt.lower())
        if not match:
            return None
        words = int(match.group(1))
        if words < 50:
            return None
        return int(
            min(words * NeoChatService._TOKENS_PER_WORD, NeoChatService._MAX_REQUESTED_PREDICT)
        )

    def _stream_answer(
        self,
        llm: LLMClient,
        messages: list[LLMMessage],
        budget: int,
        *,
        buffer_content: bool,
    ) -> Iterator[dict[str, Any]]:
        """Stream one answer, continuing it at most twice if it hits the output limit.

        Yields what the client should see -- thinking always, the answer text
        unless ``buffer_content`` -- and returns ``(raw_reply, final_metadata,
        streamed_reply, thinking)`` through ``yield from``. Continuation text is
        collected, not streamed, so ``streamed_reply`` is exactly what the
        client has been shown.
        """

        raw_reply = ""
        streamed_reply = ""
        thinking = ""
        final_metadata: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "duration_ms": None,
            "finish_reason": None,
        }
        for event in llm.chat_stream(messages, temperature=0.2, num_predict=budget):
            kind = event.get("type")
            if kind == "chunk":
                raw_reply += event["content"]
                if not buffer_content:
                    streamed_reply += event["content"]
                    yield event
            elif kind == "thinking":
                thinking += event["content"]
                yield event
            elif kind == "done":
                final_metadata = event
        continuation_count = 0
        while final_metadata.get("finish_reason") == "length" and continuation_count < 2:
            continuation_count += 1
            yield {
                "type": "status",
                "content": "Continuing a response that reached the model limit",
            }
            # An assistant turn with no text carries nothing to continue from, and
            # some providers reject empty turns outright.
            continuation_messages = [
                *messages,
                *([LLMMessage(role="assistant", content=raw_reply)] if raw_reply.strip() else []),
                LLMMessage(
                    role="user",
                    content=(
                        "Continue the same answer exactly where it stopped. "
                        "Do not repeat earlier text. Finish the requested answer."
                    ),
                ),
            ]
            continuation = ""
            continuation_metadata: dict[str, Any] = {}
            # Grow the budget each round. Repeating the same cap makes a model that
            # overspends on reasoning fail identically every time.
            retry_budget = min(budget * (continuation_count + 1), 8192)
            for event in llm.chat_stream(
                continuation_messages, temperature=0.2, num_predict=retry_budget
            ):
                kind = event.get("type")
                if kind == "chunk":
                    continuation += str(event.get("content") or "")
                elif kind == "thinking":
                    thinking += str(event.get("content") or "")
                    yield event
                elif kind == "done":
                    continuation_metadata = event
            raw_reply = _append_without_overlap(raw_reply, continuation)
            final_metadata = _merge_generation_metadata(final_metadata, continuation_metadata)
        return raw_reply, final_metadata, streamed_reply, thinking

    @staticmethod
    def _answers_without_evidence(web_context: WebContext) -> bool:
        """Whether a failed search can still be followed by a general answer.

        A single current fact -- a figure, a date, a version -- cannot be answered
        honestly without sources, so it keeps the plain "could not find" reply.
        Anything else is answered from general knowledge, after saying that
        current information could not be verified.
        """

        return web_context.answer_mode != "fact_lookup"

    def _generate_complete(
        self,
        messages: list[LLMMessage],
        *,
        num_predict: int,
        llm: LLMClient | None = None,
    ) -> LLMChatResult:
        """Generate, continuing when the model stops because it hit the output limit.

        The streaming path already does this. Without it here a long answer comes back
        silently truncated mid-word, because the caller has no way to tell a finished
        answer from one that ran out of room.
        """
        llm = llm or self.ollama
        result = llm.chat_with_metadata(messages, temperature=0.2, num_predict=num_predict)
        content = result.content
        prompt_tokens = result.prompt_tokens or 0
        completion_tokens = result.completion_tokens or 0
        duration_ms = result.duration_ms or 0

        attempts = 0
        while result.finish_reason == "length" and attempts < 2:
            attempts += 1
            follow_up = [
                *messages,
                *([LLMMessage(role="assistant", content=content)] if content.strip() else []),
                LLMMessage(
                    role="user",
                    content=(
                        "Continue the same answer exactly where it stopped. "
                        "Do not repeat earlier text. Finish the requested answer."
                    ),
                ),
            ]
            result = llm.chat_with_metadata(
                follow_up, temperature=0.2, num_predict=min(num_predict * (attempts + 1), 8192)
            )
            content = _append_without_overlap(content, result.content)
            prompt_tokens += result.prompt_tokens or 0
            completion_tokens += result.completion_tokens or 0
            duration_ms += result.duration_ms or 0

        return result.model_copy(
            update={
                "content": content,
                "prompt_tokens": prompt_tokens or None,
                "completion_tokens": completion_tokens or None,
                "total_tokens": (prompt_tokens + completion_tokens) or None,
                "duration_ms": duration_ms or None,
            }
        )

    def _num_predict(self, prompt: str, context: ContextPackage, *, reasoning: bool = False) -> int:
        """The output limit for an answer: a ceiling, not a target.

        An explicit length request ("in about 1500 words") gets room for it;
        anything else gets the configured limit. When the answer reasons first,
        its reasoning counts against the same limit, so it gets extra headroom
        rather than leaving the answer itself to be cut off. The provider runtime
        lowers the limit further if the model's context window cannot hold it.
        """

        requested = self._requested_output_tokens(prompt)
        budget = (
            max(requested, self.settings.chat_num_predict)
            if requested
            else self.settings.chat_num_predict
        )
        if reasoning:
            budget += self.settings.chat_thinking_headroom_tokens
        return budget


#: The reply to an explicit connector command: chat has no connector runner.
_CONNECTOR_UNSUPPORTED_REPLY = (
    "I can't run connectors or external tools from a chat reply, so nothing was called."
)

#: Asked when a weather request names no place.
_WEATHER_PLACE_QUESTION = "Which place should I check the weather for?"

#: Added when a reply said it lacked information and the search for it found
#: nothing usable, so the user knows a search was tried.
_SEARCH_FOUND_NOTHING_NOTE = "I also searched the web for this, but found no usable sources."

#: The answer when a model returns nothing visible twice in a row.
_EMPTY_ANSWER_REPLY = (
    "I couldn't produce an answer to that just now. Please try again, or rephrase the question."
)

#: The one follow-up for a web answer that left out its citation markers.
_CITATION_REPAIR_REQUEST = (
    "Rewrite your answer above so that every fact taken from the web context is followed "
    "by the bracket number of the source it came from, like [1]. Use only the numbered "
    "sources in the web context, and leave out anything they do not support. Keep the "
    "answer otherwise the same, and do not add a list of sources."
)


def _searched_for(prompt: str, web_context: WebContext | None) -> str | None:
    """What the turn searched, when that is not the user's message itself.

    A search command or an agreement searches the earlier question, and a
    router decision searches a standalone rewrite. The answer step only sees
    the user's literal message ("search", "yes"), so without this it may not
    know what the evidence is for and answer the wrong question.
    """

    if web_context is None or not web_context.needed:
        return None
    query = " ".join((web_context.query or "").split())[:300]
    if not query or query.casefold() == " ".join((prompt or "").split()).casefold():
        return None
    return query.replace('"', "'")


def _with_timings(metadata: dict[str, Any], timings: dict[str, Any]) -> dict[str, Any]:
    """``metadata`` for a persisted reply, with this turn's timings added."""

    inner = metadata.get("metadata")
    inner = dict(inner) if isinstance(inner, dict) else {}
    inner["timings"] = timings
    return {**metadata, "metadata": inner}


def _writing_from_sources(web_context: WebContext) -> str:
    """A status naming the sources the answer is being written from."""

    hosts: list[str] = []
    for citation in web_context.citations:
        host = urlparse(citation.url).netloc.lower().removeprefix("www.")
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return "Writing the answer from sources"
    shown = ", ".join(hosts[:3]) + (" and others" if len(hosts) > 3 else "")
    count = len(web_context.citations)
    return f"Writing the answer from {count} source{'s' if count != 1 else ''}: {shown}"


def _corroborated(fact: FactResult, web_context: WebContext) -> bool:
    """Whether independent sources state the same extracted value.

    Counted by registrable domain, so two pages of one site are one source. A
    value only one source states, or one that another source contradicts, is not
    corroborated, and the answer is then generated from the cited evidence.
    """

    from app.services.research.textutil import compact, host_group, host_of

    value = compact(str(fact.answer or ""))
    if len(value) < 2:
        return False
    hosts = {
        host_group(host_of(chunk.source_url))
        for chunk in web_context.evidence_chunks
        if value in compact(chunk.text)
    }
    hosts.discard("")
    return len(hosts) >= 2


def _append_without_overlap(existing: str, continuation: str) -> str:
    """Join provider continuations without repeating an overlapping prefix."""

    if not continuation:
        return existing
    limit = min(len(existing), len(continuation), 1000)
    for overlap in range(limit, 0, -1):
        if existing[-overlap:] == continuation[:overlap]:
            return existing + continuation[overlap:]
    separator = (
        "" if existing.endswith((" ", "\n")) or continuation.startswith((" ", "\n")) else " "
    )
    return existing + separator + continuation


#: The sentence ``_PROMPT_WEB`` tells the model to write when the evidence does
#: not contain the answer. Being an instruction followed, not a failure to cite,
#: it is never sent back for citations.
_SOURCES_LACK_ANSWER = re.compile(
    r"\bI searched the web but could not find sufficiently reliable current sources\b",
    re.IGNORECASE,
)


def _without_usable_evidence(web_context: WebContext) -> WebContext:
    """The same search, recorded as having found nothing the answer can use."""

    return web_context.model_copy(
        update={
            "citations": [],
            "evidence_chunks": [],
            "context_text": "",
            "warning": "The pages found did not answer the question.",
        }
    )


def _merge_generation_metadata(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    merged = {**first, **second}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "duration_ms"):
        values = [item.get(key) for item in (first, second)]
        numeric = [value for value in values if isinstance(value, int)]
        merged[key] = sum(numeric) if numeric else None
    return merged


def _referenced_citation_indices(text: str) -> set[int]:
    """Citation numbers the text actually cites, including grouped [1, 2] markers."""
    indices: set[int] = set()
    for group in re.findall(r"\[((?:\d{1,2})(?:\s*,\s*\d{1,2})*)\]", text):
        indices.update(int(number) for number in re.findall(r"\d{1,2}", group))
    return indices


#: The start of a Sources/References block the model wrote itself. At the start
#: of a line it may be bolded or a heading ("**Sources:**", "## References:").
#: Mid-line it counts only when citations or a line break follow, so prose such
#: as "the main energy sources: coal and gas" is never cut off.
_LLM_SOURCES_HEADER = re.compile(
    r"(?:^|\n)[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?(?:Sources|References|Citations)(?:\*\*|__)?"
    r"[ \t]*:"
    r"|\s(?:\*\*|__)?(?:Sources|References|Citations)(?:\*\*|__)?[ \t]*:"
    r"(?=\s*(?:\[\d|https?://|\n|$))"
    # A heading line on its own, in any Markdown dress and with or without a
    # colon: "**Sources**", "*Sources:*", "### References".
    r"|(?:^|\n)[ \t]*(?:#{1,6}[ \t]*)?[*_]{0,3}[ \t]*(?:Sources|References|Citations)"
    r"[ \t]*:?[ \t]*[*_]{0,3}[ \t]*:?[ \t]*(?=\n|$)",
    re.IGNORECASE,
)
#: A Markdown rule left dangling once a model-written Sources block is removed.
_TRAILING_RULE = re.compile(r"\n[ \t]*(?:\*{3,}|-{3,}|_{3,})[ \t]*$")


def _strip_llm_sources_block(reply: str) -> str:
    """Remove any Sources/References block the LLM generated; the backend appends its own."""
    cleaned = _LLM_SOURCES_HEADER.split(reply, maxsplit=1)[0]
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*\n(?:\s*\[?\d{1,2}\]?\s*.*\n?)*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\n{1,3}(?:Sources|References|Citations)\s*:\s*$", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\s*[\[(]\s*$", "", cleaned)
    cleaned = _TRAILING_RULE.sub("", cleaned.rstrip())
    return cleaned.strip()


_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(\s*(https?://[^\s)]+)\s*\)")
_BARE_URL = re.compile(r"https?://\S+")


def _strip_fabricated_urls(reply: str, valid_urls: set[str]) -> str:
    """Remove inline URLs from an answer body that are not in the valid citation set.

    Only reached once a web search has actually run, so an uncited URL here is a
    source the model invented rather than ordinary content.

    A Markdown link collapses to its own label instead of losing only the target:
    deleting the URL alone left ``[label](`` behind, which reads as broken output and
    renders as literal text.
    """

    def _cited(url: str) -> bool:
        trimmed = url.rstrip(".,;:)>]")
        if trimmed in valid_urls:
            return True
        return any(trimmed.startswith(valid) or valid.startswith(trimmed) for valid in valid_urls)

    def _replace_link(match: re.Match) -> str:
        return match.group(0) if _cited(match.group(2)) else match.group(1)

    def _replace_url(match: re.Match) -> str:
        return match.group(0) if _cited(match.group(0)) else ""

    cleaned = _MARKDOWN_LINK.sub(_replace_link, reply)
    cleaned = _BARE_URL.sub(_replace_url, cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def _clean_snippet_text(text: str) -> str:
    """Strip raw search-result labels that should never appear in output."""
    cleaned = re.sub(r"^Search result title:\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.\s*Search result snippet:\s*", ". ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Search result snippet:\s*", "", cleaned, flags=re.IGNORECASE)
    # Evidence-extraction labels tell the model what kind of passage it is
    # reading; to a person they are noise.
    cleaned = re.sub(
        r"^(?:Article pick|Page description|Infobox):\s*", "", cleaned, flags=re.IGNORECASE
    )
    return cleaned.strip()


#: Replies that agree to something, with nothing else in them. Used only to skip
#: past them when looking for the question a search command refers to.
_SHORT_AGREEMENT = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|please|please do|go ahead|do it|go for it|"
    r"yes please|sure thing|alright|all right)[.!\s]*$",
    re.IGNORECASE,
)

#: A reply that says the model lacks the information the question needs. This
#: reads the model's own wording about itself -- never the user's topic -- and it
#: is only acted on for short replies on turns that did not search.
_KNOWLEDGE_GAP = re.compile(
    r"(?:\bI (?:do not|don't) know\b|"
    r"\bI (?:do not|don't) have (?:access to )?(?:any )?(?:current|real-time|up-to-date|live|"
    r"the latest|recent|reliable|that|this|enough) (?:information|data|details)?|"
    r"\bI(?:'m| am) not sure\b|"
    r"\bI (?:couldn't|could not|can't|cannot) (?:find|verify|confirm|access|browse|check)\b|"
    r"\bI can look (?:it|this|that) up\b|"
    r"\bas of my (?:last|latest|most recent) (?:update|training|knowledge)|"
    r"\bmy (?:knowledge|training data|training) (?:cutoff|cut-off|only goes|ends)\b|"
    r"\bno (?:access|way) to (?:the internet|real-time|current|live)\b)",
    re.IGNORECASE,
)
_KNOWLEDGE_GAP_MAX_CHARS = 500
_KNOWLEDGE_GAP_SCAN_CHARS = 300


def _excerpt(text: str, limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "\u2026"


def _capability_used(message: ChatMessage | None) -> str | None:
    """A short label for what an earlier reply drew on, for the router's context."""

    if message is None:
        return None
    kind = message.response_kind or ""
    if kind == "web_search":
        return "web search"
    if kind.startswith("calendar"):
        return "the user's calendar"
    if kind == "structured_weather":
        return "a weather lookup"
    if kind == "structured_currency":
        return "a currency conversion"
    if kind == "local_datetime":
        return "the local clock"
    if kind == "direct_memory":
        return "saved memory"
    return None
