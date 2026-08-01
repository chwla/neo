from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.memory_v2.extraction_contracts import (
    ConversationRole,
    ExtractionMode,
    ExtractionRequest,
    ModelExtractionInput,
    TrustedConversationMessage,
)
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags, MemoryV2RolloutError
from app.services.memory_v2.model_schema import ModelProposalResponse
from tests.memory_v2.phase3_helpers import OWNER_A
from tests.memory_v2.phase4_helpers import phase4_harness, run_text

PHASE4_RUNTIME_FILES = (
    "app/services/memory_v2/extraction.py",
    "app/services/memory_v2/extraction_contracts.py",
    "app/services/memory_v2/preparser.py",
    "app/services/memory_v2/model_schema.py",
    "app/services/memory_v2/grounding.py",
    "app/services/memory_v2/correction_resolver.py",
    "app/services/memory_v2/extraction_coordinator.py",
    "app/services/memory_v2/extraction_diagnostics.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_phase4_production_flags_default_to_fully_disabled() -> None:
    settings = Settings(_env_file=None)
    assert not settings.memory_v2_extraction_enabled
    assert not settings.memory_v2_foreground_commands_enabled
    assert not settings.memory_v2_post_turn_extraction_enabled
    assert not settings.memory_v2_live_extraction_model_enabled
    assert settings.memory_v2_extraction_provider == ""
    assert settings.memory_v2_extraction_endpoint == ""
    flags = MemoryV2FeatureFlags.from_settings(settings)
    assert not flags.extraction_enabled
    assert not flags.foreground_commands_enabled
    assert not flags.post_turn_extraction_enabled
    assert not flags.live_extraction_model_enabled


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"foreground_commands_enabled": True},
            "memory_v2_extraction_subfeatures_require_extraction",
        ),
        (
            {"post_turn_extraction_enabled": True},
            "memory_v2_extraction_subfeatures_require_extraction",
        ),
        (
            {"extraction_enabled": True},
            "memory_v2_extraction_requires_canonical_schema_writes",
        ),
        (
            {
                "schema_enabled": True,
                "canonical_writes": True,
                "enabled_owner_ids": frozenset({OWNER_A}),
                "disposable_database_root": "/tmp/phase4-test",
                "extraction_enabled": True,
                "live_extraction_model_enabled": True,
            },
            "memory_v2_live_extraction_requires_endpoint",
        ),
    ],
)
def test_phase4_flag_combinations_fail_closed(updates, reason) -> None:
    with pytest.raises(MemoryV2RolloutError, match=reason):
        MemoryV2FeatureFlags(**updates)


def test_live_provider_selection_is_required_and_allowlisted() -> None:
    common = {
        "schema_enabled": True,
        "canonical_writes": True,
        "enabled_owner_ids": frozenset({OWNER_A}),
        "disposable_database_root": "/tmp/phase4-test",
        "extraction_enabled": True,
        "live_extraction_model_enabled": True,
        "extraction_endpoint": "http://provider.test/extract",
    }
    with pytest.raises(
        MemoryV2RolloutError,
        match="memory_v2_live_extraction_requires_explicit_provider",
    ):
        MemoryV2FeatureFlags(**common)
    with pytest.raises(
        MemoryV2RolloutError,
        match="memory_v2_live_extraction_requires_explicit_provider",
    ):
        MemoryV2FeatureFlags(**common, extraction_provider="auto")
    for provider in ("direct_json", "ollama"):
        flags = MemoryV2FeatureFlags(**common, extraction_provider=provider)
        assert flags.extraction_provider == provider


def test_phase4_runtime_has_no_direct_orm_repository_or_search_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden_imports = {
        "app.models.memory_v2",
        "app.repositories.memory_v2",
        "app.services.memory_v2.diagnostics",
        "app.services.memory_v2.coordinator",
        "app.services.memory_v2.mutations",
        "app.services.memory_v2.source_changes",
    }
    forbidden_terms = ("embedding", "vector", "qdrant", "fts", "semantic_similarity")
    for relative in PHASE4_RUNTIME_FILES:
        path = root / relative
        imports = _imports(path)
        source = path.read_text(encoding="utf-8").casefold()
        assert not (imports & forbidden_imports)
        assert "app.services.memory_v2.recall" not in imports
        for term in forbidden_terms:
            assert term not in source
    coordinator_imports = _imports(root / "app/services/memory_v2/extraction_coordinator.py")
    assert "app.services.memory_v2.adapters" in coordinator_imports


def test_only_extraction_coordinator_calls_model_extract() -> None:
    root = Path(__file__).resolve().parents[2]
    callers = []
    for relative in PHASE4_RUNTIME_FILES:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "extract"
        ]
        if calls:
            callers.append(relative)
    assert callers == ["app/services/memory_v2/extraction_coordinator.py"]


def test_model_visible_and_output_schemas_cannot_carry_owner_or_canonical_ids() -> None:
    assert "owner_id" not in ModelExtractionInput.model_fields
    assert "owner_id" not in ModelProposalResponse.model_fields
    assert "memory_id" not in ModelProposalResponse.model_fields
    with pytest.raises(ValidationError):
        ModelProposalResponse.model_validate(
            {
                "schema_version": 1,
                "assertions": [],
                "retractions": [],
                "exclusions": [],
                "predecessor_id": "00000000-0000-4000-8000-000000000099",
            }
        )


def test_model_visible_window_omits_system_and_tool_content() -> None:
    text = "I use Python for work."
    request = ExtractionRequest(
        request_id="bounded-model-input",
        owner_id=OWNER_A,
        conversation_id="conversation",
        session_id="session",
        message_id="current-user",
        user_message=text,
        supporting_window=(
            TrustedConversationMessage(
                message_id="prior-user",
                role=ConversationRole.USER,
                content="I work in software.",
            ),
            TrustedConversationMessage(
                message_id="assistant",
                role=ConversationRole.ASSISTANT,
                content="What language do you use?",
            ),
            TrustedConversationMessage(
                message_id="system",
                role=ConversationRole.SYSTEM,
                content="arbitrary system instructions",
            ),
            TrustedConversationMessage(
                message_id="tool",
                role=ConversationRole.TOOL,
                content="private tool result",
            ),
        ),
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        source_content_hash=ExtractionRequest.content_hash(text),
    )
    visible = ModelExtractionInput.from_trusted_request(request)
    assert [item.role for item in visible.supporting_window] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ]
    assert "owner_id" not in visible.model_dump()


def test_legacy_runtime_has_no_phase4_import_or_dual_extraction() -> None:
    root = Path(__file__).resolve().parents[2]
    legacy_files = (
        "app/services/extraction.py",
        "app/services/chat.py",
        "app/services/review.py",
        "app/api/routes/memory.py",
    )
    for relative in legacy_files:
        source = (root / relative).read_text(encoding="utf-8")
        assert "memory_v2.extraction" not in source
        assert "MemoryV2ExtractionCoordinator" not in source


def test_disposable_v2_extraction_does_not_create_or_mutate_legacy_tables(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path)
    run_text(
        extraction,
        harness,
        "I want to create long-form cinematic YouTube videos.",
        message_id="v2-only",
    )
    with sqlite3.connect(harness.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert not (
        tables
        & {
            "memories",
            "goals",
            "preferences",
            "events",
            "education",
            "memory_candidates",
            "memory_sources",
            "memory_embeddings",
        }
    )
    assert "memory_records_v2" in tables


def test_phase4_contains_no_recall_prompt_or_migration_implementation() -> None:
    root = Path(__file__).resolve().parents[2]
    imports = set()
    for relative in PHASE4_RUNTIME_FILES:
        imports.update(_imports(root / relative))
    assert "app.services.memory_v2.recall" not in imports
    assert "app.services.memory_v2.migration" not in imports
    assert "app.services.memory_v2.serialization" not in imports
