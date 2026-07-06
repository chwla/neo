from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.services.files.service import WorkspaceFilesService
from app.services.files.store import now_iso
from app.services.projects.store import get_project
from app.services.repos import store
from app.services.repos.importer import import_scan
from app.services.repos.safety import ensure_inside, validate_repo_root
from app.services.repos.scanner import scan_repo
from app.services.repos.types import RepoRegisterRequest


class RepoWorkspaceService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def register(self, request: RepoRegisterRequest) -> dict:
        if not request.confirm:
            raise ValueError("Repository registration requires confirm=true.")
        if request.project_id and not get_project(request.project_id):
            raise LookupError("Project not found.")
        source = validate_repo_root(request.path)
        if store.get_repo_by_original_path(str(source)):
            raise ValueError("This repository is already registered.")
        scan = scan_repo(
            source,
            max_files=self.settings.workspace_repo_max_files,
            max_total_bytes=self.settings.workspace_repo_max_total_bytes,
            max_file_bytes=self.settings.workspace_repo_max_file_bytes,
        )
        if not scan.files:
            raise ValueError("No supported text files were found in the selected repository.")

        repo_id = str(uuid.uuid4())
        managed_root = Path(self.settings.workspace_repos_dir).resolve()
        managed_root.mkdir(parents=True, exist_ok=True)
        workspace_path = ensure_inside(managed_root, managed_root / repo_id)
        now = now_iso()
        repo = store.insert_repo(
            {
                "id": repo_id,
                "project_id": request.project_id,
                "name": request.name.strip() if request.name else source.name,
                "original_path": str(source),
                "workspace_path": str(workspace_path),
                "status": "importing",
                "file_count": len(scan.files),
                "indexed_file_count": 0,
                "total_bytes": scan.total_bytes,
                "metadata": {},
                "deleted": False,
                "created_at": now,
                "updated_at": now,
                "indexed_at": None,
            }
        )
        try:
            mappings = import_scan(repo, scan)
        except Exception:
            if workspace_path.exists():
                shutil.rmtree(workspace_path)
            store.cleanup_failed_import(repo_id)
            raise
        metadata = {
            "ignored_files": scan.ignored_files,
            "ignored_dirs": scan.ignored_dirs,
            "unsupported_files": scan.unsupported_files,
        }
        return (
            store.update_repo(
                repo_id,
                {
                    "status": "ready",
                    "indexed_file_count": len(mappings),
                    "metadata_json": metadata,
                    "updated_at": now,
                    "indexed_at": now,
                },
            )
            or repo
        )

    def get(self, repo_id: str) -> dict:
        repo = store.get_repo(repo_id)
        if not repo:
            raise LookupError("Repository not found.")
        return repo

    def get_file(self, repo_id: str, repo_file_id: str) -> tuple[dict, dict]:
        self.get(repo_id)
        mapping = store.get_repo_file(repo_file_id)
        if not mapping or mapping["repo_id"] != repo_id:
            raise LookupError("Repository file not found.")
        return mapping, WorkspaceFilesService().get(mapping["file_id"])

    def soft_delete(self, repo_id: str) -> None:
        self.get(repo_id)
        store.update_repo(repo_id, {"deleted": True, "updated_at": self._now()})

    @staticmethod
    def _now() -> str:
        return now_iso()
