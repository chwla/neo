from __future__ import annotations

from app.services.code_index.extractors import ExtractionResult, extract
from app.services.code_index.resolvers import resolve_dependencies
from app.services.code_index.safety import workspace_text
from app.services.files import store as file_store


def index_repo_file(mapping: dict, repo_files: list[dict]) -> tuple[ExtractionResult, list[dict]]:
    file_item = file_store.get_file(mapping["file_id"])
    if not file_item:
        raise LookupError("Mapped workspace file is missing or deleted.")
    text = workspace_text(file_item)
    result = extract(mapping["relative_path"], text)
    dependencies = resolve_dependencies(mapping["relative_path"], result.dependencies, repo_files)
    return result, dependencies
