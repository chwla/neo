from __future__ import annotations

from app.services.symbol_awareness.reference_extractor import RawReference


def resolve_reference(
    reference: RawReference,
    source_mapping: dict,
    symbols: list[dict],
    dependencies: list[dict],
) -> tuple[dict | None, float]:
    candidates = [item for item in symbols if item["name"] == reference.name]
    if not candidates and reference.metadata.get("imported_name"):
        imported_name = reference.metadata["imported_name"]
        candidates = [item for item in symbols if item["name"] == imported_name]
    if not candidates:
        return None, 0.35
    same_file = [item for item in candidates if item["repo_file_id"] == source_mapping["id"]]
    if same_file:
        return _closest(same_file, reference.line_start), 0.96
    target_ids = {
        dependency["target_repo_file_id"]
        for dependency in dependencies
        if dependency.get("target_repo_file_id")
        and dependency["source_repo_file_id"] == source_mapping["id"]
    }
    imported = [item for item in candidates if item["repo_file_id"] in target_ids]
    if imported:
        return imported[0], 0.92
    if len(candidates) == 1:
        return candidates[0], 0.76
    return candidates[0], 0.55


def containing_symbol(symbols: list[dict], repo_file_id: str, line: int) -> dict | None:
    candidates = [
        item
        for item in symbols
        if item["repo_file_id"] == repo_file_id
        and item.get("line_start") is not None
        and item["line_start"] <= line <= (item.get("line_end") or item["line_start"])
        and item["symbol_type"] not in {"import", "export", "api_route", "heading"}
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.get("line_end") or line) - item["line_start"])


def relationship_type(reference_type: str) -> str:
    return {
        "call": "calls",
        "component_usage": "uses_component",
        "import_usage": "imports",
        "export_usage": "exports",
        "route_handler": "handles_route",
        "type_usage": "extends",
    }.get(reference_type, "related_by_name")


def _closest(symbols: list[dict], line: int) -> dict:
    return min(symbols, key=lambda item: abs((item.get("line_start") or line) - line))
