from app.models import Base


def test_initial_schema_tables_are_registered() -> None:
    expected_tables = {
        "profile",
        "preferences",
        "goals",
        "projects",
        "events",
        "memories",
        "memory_candidates",
        "reflections",
        "memory_project_links",
        "event_project_links",
    }

    assert expected_tables.issubset(Base.metadata.tables.keys())


def test_memory_schema_keeps_archives_out_of_durable_memories() -> None:
    memory_columns = Base.metadata.tables["memories"].columns.keys()

    assert "memory_text" in memory_columns
    assert "source" in memory_columns
    assert "transcript" not in memory_columns


def test_confidence_and_importance_constraints_exist() -> None:
    table_names = ["profile", "preferences", "memories", "memory_candidates"]
    constraints = {
        table_name: {constraint.name for constraint in Base.metadata.tables[table_name].constraints}
        for table_name in table_names
    }

    assert "ck_profile_confidence" in constraints["profile"]
    assert "ck_preferences_importance" in constraints["preferences"]
    assert "ck_memories_importance" in constraints["memories"]
    assert "ck_candidates_confidence" in constraints["memory_candidates"]

