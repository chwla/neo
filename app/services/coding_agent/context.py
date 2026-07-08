from __future__ import annotations

from app.services.code_index.service import CodeIndexService
from app.services.files import store as file_store
from app.services.repos import store as repo_store
from app.services.symbol_awareness.service import SymbolAwarenessService


class CodingContextSelector:
    def select(
        self,
        repo: dict,
        objective: str,
        task_id: str | None,
        project_id: str | None,
        limit: int = 6,
    ) -> list[dict]:
        selected: dict[str, dict] = {}

        def add(file_id: str | None, source: str, reason: str) -> None:
            if not file_id or file_id in selected or len(selected) >= limit:
                return
            mapping = repo_store.get_repo_file_by_file_id(file_id)
            item = file_store.get_file(file_id)
            if not mapping or mapping["repo_id"] != repo["id"] or not item:
                return
            selected[file_id] = {
                "file_id": file_id,
                "repo_file_id": mapping["id"],
                "relative_path": mapping["relative_path"],
                "sha256": item.get("sha256"),
                "source": source,
                "reason": reason,
            }

        if project_id:
            try:
                for file_id in SymbolAwarenessService().suggest_file_ids(
                    project_id, objective, limit
                ):
                    add(
                        file_id,
                        "symbol_awareness",
                        "Matched definitions, references, or related files for the objective.",
                    )
            except (LookupError, ValueError):
                pass
        try:
            for result in CodeIndexService().search(repo["id"], objective, limit=limit * 2):
                add(
                    result.get("file_id"),
                    "code_index",
                    f"Codebase Index matched {result.get('name') or result.get('relative_path')}.",
                )
        except (LookupError, ValueError):
            pass
        if task_id:
            linked, _ = file_store.list_files(link_type="task", target_id=task_id, limit=limit)
            for item in linked:
                add(item["id"], "attached_file", "Attached to the selected task.")
        if project_id:
            linked, _ = file_store.list_files(
                link_type="project", target_id=project_id, limit=limit
            )
            for item in linked:
                add(item["id"], "attached_file", "Attached to the selected project.")
        if not selected:
            files, _ = repo_store.list_repo_files(repo["id"], q=objective, limit=limit)
            if not files:
                files, _ = repo_store.list_repo_files(repo["id"], limit=limit)
            for mapping in files:
                add(mapping["file_id"], "repo_search", "Bounded repository fallback selection.")
        if not selected:
            raise ValueError("No previewable managed repository files could be selected.")
        return list(selected.values())
