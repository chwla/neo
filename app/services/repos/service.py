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
from app.services.repos.safety import ensure_inside, validate_live_root, validate_repo_root
from app.services.repos.scanner import scan_repo
from app.services.repos.types import RepoRegisterRequest


class RepoWorkspaceService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def register(self, request: RepoRegisterRequest) -> dict:
        """Attach a folder on this machine, live or as a managed copy.

        ``live`` is the default because it is what the user almost always means:
        the agent edits the folder they pointed at, and there is no second step
        to get the work back out. ``managed`` keeps the older copy-and-deliver
        behaviour for anyone who wants their own files left alone.
        """

        if not request.confirm:
            raise ValueError("Repository registration requires confirm=true.")
        if request.project_id and not get_project(request.project_id):
            raise LookupError("Project not found.")
        live = request.access == store.LIVE
        source = validate_live_root(request.path) if live else validate_repo_root(request.path)
        if store.get_repo_by_original_path(str(source)):
            raise ValueError("This repository is already registered.")
        return self._import_root(
            source,
            name=request.name.strip() if request.name else source.name,
            project_id=request.project_id,
            original_path=str(source),
            access=store.LIVE if live else store.MANAGED,
            # An empty folder is a legitimate starting point for a live
            # workspace: the user is asking the agent to create the first file,
            # not importing existing code.
            allow_empty=live,
        )

    def _import_root(
        self,
        source: Path,
        *,
        name: str,
        project_id: str | None,
        original_path: str,
        access: str = store.MANAGED,
        allow_empty: bool = False,
        empty_message: str = (
            "No supported text files were found in the selected repository."
        ),
    ) -> dict:
        scan = scan_repo(
            source,
            max_files=self.settings.workspace_repo_max_files,
            max_total_bytes=self.settings.workspace_repo_max_total_bytes,
            max_file_bytes=self.settings.workspace_repo_max_file_bytes,
        )
        if not scan.files and not allow_empty:
            raise ValueError(empty_message)

        repo_id = str(uuid.uuid4())
        live = access == store.LIVE
        if live:
            # There is no copy, so the workspace *is* the folder. Recording it
            # rather than a managed path is what lets every downstream consumer
            # -- file tools, the command sandbox, the code index -- reach the
            # real files without knowing anything about live workspaces.
            workspace_path = source
        else:
            managed_root = Path(self.settings.workspace_repos_dir).resolve()
            managed_root.mkdir(parents=True, exist_ok=True)
            workspace_path = ensure_inside(managed_root, managed_root / repo_id)
        now = now_iso()
        repo = store.insert_repo(
            {
                "id": repo_id,
                "project_id": project_id,
                "name": name,
                "original_path": original_path,
                "workspace_path": str(workspace_path),
                "access": access,
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
            mappings = import_scan(repo, scan, copy=not live)
        except Exception:
            # Only a managed copy is Neo's to delete. Removing the tree on a
            # failed live import would delete the user's project.
            if not live and workspace_path.exists():
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
