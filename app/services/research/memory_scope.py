"""Scoped memory retrieval for research context — only fetches relevant memories."""

from __future__ import annotations

import logging
import re

from app.db.session import SessionLocal
from app.models.enums import GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore

logger = logging.getLogger(__name__)

MAX_MEMORY_CONTEXT_CHARS = 600

_PERSONAL_PATTERNS = re.compile(
    r"\b(my |for me|my laptop|my pc|my computer|my machine|my hardware|"
    r"my setup|my project|my stack|for my|my team|my use case|"
    r"i have|i use|i need|i want|should i|do i)\b",
    re.IGNORECASE,
)

_HARDWARE_PATTERNS = re.compile(
    r"\b(laptop|pc|computer|hardware|ram|gpu|cpu|processor|graphics|"
    r"vram|desktop|machine|device|specs?|local|on-device)\b",
    re.IGNORECASE,
)

_PROJECT_PATTERNS = re.compile(
    r"\b(neo|shelfd|project|building|my app|my tool|my service|stack)\b",
    re.IGNORECASE,
)


def retrieve_scoped_memory(query: str) -> tuple[str, list[str]]:
    """Return (memory_context_text, list_of_memory_keys_used).

    Only retrieves memory categories relevant to the research query.
    Returns empty string if no personal context is needed.
    """
    if not _PERSONAL_PATTERNS.search(query):
        return "", []

    try:
        db = SessionLocal()
        try:
            store = MemoryStore(db)
            return _build_scoped_context(store, query)
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to retrieve scoped memory")
        return "", []


def _build_scoped_context(store: MemoryStore, query: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    keys_used: list[str] = []

    if _HARDWARE_PATTERNS.search(query):
        hw = _get_hardware(store)
        if hw:
            parts.append(f"Hardware: {hw}")
            keys_used.append("current_hardware")

    if _PROJECT_PATTERNS.search(query):
        projects = _get_projects(store)
        if projects:
            parts.append(f"Active projects: {projects}")
            keys_used.append("projects")

    goals = _get_goals(store)
    if goals:
        parts.append(f"Goals: {goals}")
        keys_used.append("goals")

    profile = _get_profile_basics(store)
    if profile:
        parts.append(f"Profile: {profile}")
        keys_used.append("profile")

    context = "\n".join(parts)
    if len(context) > MAX_MEMORY_CONTEXT_CHARS:
        context = context[:MAX_MEMORY_CONTEXT_CHARS] + "..."

    return context, keys_used


def _get_hardware(store: MemoryStore) -> str:
    memories = [
        m
        for m in store.active_memories_by_type(MemoryType.KNOWLEDGE)
        if m.canonical_slot == "current_hardware"
        or m.memory_text.lower().startswith("current hardware:")
    ]
    if memories:
        mem = sorted(memories, key=lambda m: m.updated_at, reverse=True)[0]
        return mem.memory_text.removeprefix("Current hardware:").strip()
    return ""


def _get_projects(store: MemoryStore) -> str:
    projects = store.list_projects(ProjectStatus.ACTIVE)
    if not projects:
        return ""
    items = []
    for p in projects[:3]:
        desc = f" ({p.description[:60]})" if p.description else ""
        items.append(f"{p.name}{desc}")
    return "; ".join(items)


def _get_goals(store: MemoryStore) -> str:
    goals = store.list_goals(GoalStatus.ACTIVE)
    if not goals:
        return ""
    items = [g.goal[:80] for g in goals[:3]]
    return "; ".join(items)


def _get_profile_basics(store: MemoryStore) -> str:
    facts = store.list_profile(active_only=True)
    if not facts:
        return ""
    relevant_keys = {"name", "location", "occupation", "experience_level"}
    items = [f"{f.key}={f.value}" for f in facts if f.key in relevant_keys]
    return ", ".join(items[:4])
