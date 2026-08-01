from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import canonical_memory
from app.services.archives import ArchiveSearchResult, QdrantArchiveService
from app.services.chat import NeoChatService
from app.services.context import ContextPackage
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags, MemoryV2RolloutError
from app.services.memory_v2.taxonomy import MemoryType
from app.services.research.memory_scope import retrieve_scoped_memory
from app.services.retrieval import RetrievalRequest, RetrievalService
from tests.memory_v2.phase5_helpers import (
    add_memory,
    phase5_harness,
    phase5_services,
    query_context,
)


class _UnusedLLM:
    model = "unused"


class _ForbiddenLegacyStore:
    def __getattr__(self, name):
        raise AssertionError(f"legacy store accessed: {name}")


def test_retrieval_direct_chat_and_research_use_same_canonical_id(tmp_path) -> None:
    harness, adapter = phase5_harness(tmp_path)
    goal_id = add_memory(
        adapter,
        harness,
        key="parity",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short video reels",
    )
    services = phase5_services(harness)
    try:
        scoped = query_context(
            services,
            domains=frozenset({"video_creation"}),
        )
        retrieval = RetrievalService(
            canonical_recall=services.recall,
            memory_v2_enabled=True,
        ).retrieve(
            _ForbiddenLegacyStore(),
            RetrievalRequest(query="video reels", include_archives=True),
            query_context=scoped,
        )
        assert retrieval.archive_results == []
        assert retrieval.canonical_recall is not None
        assert retrieval.canonical_recall.canonical_ids == (goal_id,)

        direct = DirectMemoryAnswerService(
            canonical_recall=services.recall,
            memory_v2_enabled=True,
        )
        answer = direct.answer(
            _ForbiddenLegacyStore(),
            "What are my current goals?",
            query_context=scoped,
        )
        assert "create short video reels" in answer
        assert direct.last_canonical_ids == (goal_id,)

        research_context, research_ids = retrieve_scoped_memory(
            "Research video reels for my goals",
            v2_enabled=True,
            orchestrator=services.prompt,
            query_context=scoped,
            usage_purpose="research_plan:test",
        )
        assert "name: neo_untrusted_memory_context" in research_context
        assert research_ids == [str(goal_id)]

        chat = NeoChatService(
            services.session,
            ollama=_UnusedLLM(),
            memory_v2_orchestrator=services.prompt,
            memory_v2_context_factory=lambda _prompt: scoped,
            memory_v2_enabled=True,
        )
        empty = ContextPackage(
            profile=[],
            preferences=[],
            goals=[],
            projects=[],
            relevant_memories=[],
            events=[],
            archive_results=[],
        )
        first = chat.build_messages("video reels", [], empty)
        second = chat.build_messages("different current question", [], empty)
        assert first[0].role == second[0].role == "system"
        assert first[0].content == second[0].content
        assert "create short video reels" not in first[0].content
        assert first[1].role == "user"
        assert "name: neo_untrusted_memory_context" in first[1].content
        assert chat.last_memory_v2_selection is not None
    finally:
        services.close()


def test_missing_research_owner_and_ownerless_archive_fail_closed() -> None:
    context, identifiers = retrieve_scoped_memory(
        "research this for my project",
        v2_enabled=True,
    )
    assert context == ""
    assert identifiers == []

    hit = ArchiveSearchResult(
        collection="conversation_archive",
        text="ownerless personal-looking archive text",
        score=1,
    )
    service = object.__new__(QdrantArchiveService)
    assert (
        service.personal_memory_context(
            [hit], authenticated_owner_id="00000000-0000-4000-8000-000000000001"
        )
        == []
    )


def test_direct_answer_gates_return_no_memory_answer(tmp_path) -> None:
    harness, adapter = phase5_harness(tmp_path)
    add_memory(
        adapter,
        harness,
        key="direct-gate",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short video reels",
    )
    services = phase5_services(harness)
    try:
        direct = DirectMemoryAnswerService(
            canonical_recall=services.recall,
            memory_v2_enabled=True,
        )
        for context in (
            query_context(services, incognito=True),
            query_context(services, memory_enabled=False),
        ):
            assert (
                direct.answer(
                    _ForbiddenLegacyStore(),
                    "What are my goals?",
                    query_context=context,
                )
                is None
            )
            assert direct.last_canonical_ids == ()
    finally:
        services.close()


def test_authenticated_chat_runtime_uses_bound_canonical_repository(
    tmp_path,
    monkeypatch,
) -> None:
    harness, adapter = phase5_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="runtime-binding",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short video reels",
    )
    services = phase5_services(harness)

    class _Flags:
        @staticmethod
        def from_settings(_settings):
            return harness.coordinator.flags

    monkeypatch.setattr(canonical_memory, "MemoryV2FeatureFlags", _Flags)
    execution = harness.context.execution
    try:
        runtime = canonical_memory.build_chat_canonical_memory_runtime(
            services.session,
            owner_id=execution.owner_id,
            database_identity=execution.database_identity,
            profile_id=execution.profile_id,
            request_id="authenticated-chat-runtime",
        )
        assert runtime is not None
        context = runtime.context_factory("video reels")
        assert str(context.owner_id) == execution.owner_id
        result = runtime.orchestrator.recall_service.recall(
            canonical_memory.RecallQuery(context=context, text="video reels")
        )
        assert result.canonical_ids == (memory_id,)

        mismatch = canonical_memory.build_chat_canonical_memory_runtime(
            services.session,
            owner_id=execution.owner_id,
            database_identity="account-profile:mismatch",
            profile_id=execution.profile_id,
            request_id="mismatched-chat-runtime",
        )
        assert mismatch is None
    finally:
        services.close()


def test_phase5_recall_has_no_vector_or_legacy_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    recall_source = (root / "app/services/memory_v2/recall.py").read_text()
    assert "Qdrant" not in recall_source
    assert "Embedding" not in recall_source
    assert "MemoryStore" not in recall_source
    research_source = (root / "app/services/research/memory_scope.py").read_text()
    assert (
        "from app.db.session import SessionLocal"
        not in research_source.split("def retrieve_scoped_memory", maxsplit=1)[0]
    )


def test_sync_and_stream_share_only_one_prompt_recall_orchestration() -> None:
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "app/services/chat.py").read_text())
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def called_attributes(method_name: str) -> list[str]:
        return [
            node.func.attr
            for node in ast.walk(methods[method_name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]

    assert called_attributes("send_message").count("build_messages") == 1
    assert called_attributes("stream_message").count("build_messages") == 1
    assert called_attributes("build_messages").count("_build_v2_memory_selection") == 1
    callers = {name for name in methods if "_build_v2_memory_selection" in called_attributes(name)}
    assert callers == {"build_messages"}


def test_phase5_production_defaults_are_disabled_and_subfeatures_fail_closed() -> None:
    settings = Settings(_env_file=None)
    assert not settings.memory_v2_canonical_query_enabled
    assert not settings.memory_v2_lexical_recall_enabled
    assert not settings.memory_v2_secure_prompt_enabled
    assert not settings.memory_v2_direct_answer_reads_enabled
    assert not settings.memory_v2_research_recall_enabled
    assert settings.memory_v2_legacy_read_compatibility
    flags = MemoryV2FeatureFlags.from_settings(settings)
    assert not flags.canonical_query_enabled
    assert not flags.lexical_recall_enabled
    assert not flags.secure_prompt_enabled
    with pytest.raises(
        MemoryV2RolloutError,
        match="memory_v2_recall_subfeatures_require_canonical_queries",
    ):
        MemoryV2FeatureFlags(lexical_recall_enabled=True)
    with pytest.raises(
        MemoryV2RolloutError,
        match="memory_v2_research_recall_requires_secure_prompt",
    ):
        MemoryV2FeatureFlags(
            schema_enabled=True,
            canonical_query_enabled=True,
            research_recall_enabled=True,
            enabled_owner_ids=frozenset({"00000000-0000-4000-8000-000000000001"}),
        )
