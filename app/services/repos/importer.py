from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.services.files import store as file_store
from app.services.files.extractors import extract_text
from app.services.files.safety import extension_for
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import FileLinkCreate
from app.services.repos import store
from app.services.repos.safety import ensure_inside
from app.services.repos.scanner import ScanResult, language_for


def import_scan(repo: dict, scan: ScanResult) -> list[dict]:
    """Copy a completed scan into Neo-managed storage and index every text file."""
    settings = get_settings()
    destination_root = Path(repo["workspace_path"]).resolve()
    managed_root = Path(settings.workspace_repos_dir).resolve()
    ensure_inside(managed_root, destination_root)
    destination_root.mkdir(parents=True, exist_ok=False)
    mappings: list[dict] = []
    files_service = WorkspaceFilesService()

    for scanned in scan.files:
        destination = ensure_inside(destination_root, destination_root / scanned.relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(scanned.content)
        digest = hashlib.sha256(scanned.content).hexdigest()
        extracted, extraction_metadata = extract_text(
            scanned.relative_path,
            scanned.content,
            settings.workspace_extracted_text_max_chars,
        )
        now = file_store.now_iso()
        file_id = str(uuid.uuid4())
        filename = Path(scanned.relative_path).name
        file_item = file_store.insert_file(
            {
                "id": file_id,
                "filename": filename,
                "original_filename": filename,
                "display_name": filename,
                "mime_type": "text/plain",
                "extension": extension_for(filename),
                "size_bytes": len(scanned.content),
                "sha256": digest,
                "storage_path": str(destination),
                "extracted_text": extracted,
                "summary": None,
                "source_type": "local_repo",
                "source_id": repo["id"],
                "metadata": {
                    **extraction_metadata,
                    "source": "local_repo",
                    "repo_id": repo["id"],
                    "repo_name": repo["name"],
                    "original_path": repo["original_path"],
                    "relative_path": scanned.relative_path,
                    "imported_at": now,
                    "ignored_counts": {
                        "ignored_dirs": scan.ignored_dirs,
                        "ignored_files": scan.ignored_files,
                        "unsupported_files": scan.unsupported_files,
                    },
                },
                "deleted": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        mapping = store.insert_repo_file(
            {
                "id": str(uuid.uuid4()),
                "repo_id": repo["id"],
                "file_id": file_id,
                "relative_path": scanned.relative_path,
                "original_relative_path": scanned.relative_path,
                "language": language_for(scanned.relative_path),
                "size_bytes": len(scanned.content),
                "sha256": digest,
                "status": "indexed",
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            }
        )
        mappings.append(mapping)
        if repo.get("project_id"):
            files_service.attach(
                file_item["id"],
                FileLinkCreate(link_type="project", target_id=repo["project_id"]),
            )
    return mappings
