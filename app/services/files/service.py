from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.services.files import store
from app.services.files.extractors import extract_text
from app.services.files.safety import extension_for, safe_storage_path, sanitize_filename
from app.services.files.types import ArtifactCreate, FileLinkCreate
from app.services.notes.store import get_note
from app.services.projects.store import get_project
from app.services.tasks.store import get_task


class WorkspaceFilesService:
    def __init__(self, storage_root: Path | None = None) -> None:
        settings = get_settings()
        store.initialize_workspace_file_tables()
        self.storage_root = Path(storage_root or settings.workspace_files_dir)
        self.max_bytes = settings.workspace_file_max_bytes
        self.max_chars = settings.workspace_extracted_text_max_chars

    def import_bytes(
        self,
        *,
        original_filename: str,
        content: bytes,
        mime_type: str | None = None,
        links: list[tuple[str, str]] | None = None,
    ) -> dict:
        if not content:
            raise ValueError("Empty files cannot be uploaded.")
        if len(content) > self.max_bytes:
            raise ValueError(f"File exceeds the {self.max_bytes}-byte upload limit.")
        safe_name = sanitize_filename(original_filename)
        digest = hashlib.sha256(content).hexdigest()
        existing = store.get_file_by_sha(digest)
        if existing:
            for link_type, target_id in links or []:
                self.attach(
                    existing["id"], FileLinkCreate(link_type=link_type, target_id=target_id)
                )
            return existing

        file_id = str(uuid.uuid4())
        internal_name = f"{file_id}_{safe_name}"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        path = safe_storage_path(self.storage_root, internal_name)
        extracted, metadata = extract_text(safe_name, content, self.max_chars)
        # This is the only disk write path: a generated name inside the configured workspace.
        path.write_bytes(content)
        now = store.now_iso()
        item = store.insert_file(
            {
                "id": file_id,
                "filename": safe_name,
                "original_filename": original_filename,
                "display_name": safe_name,
                "mime_type": mime_type,
                "extension": extension_for(safe_name),
                "size_bytes": len(content),
                "sha256": digest,
                "storage_path": str(path),
                "extracted_text": extracted,
                "summary": None,
                "source_type": "upload",
                "source_id": None,
                "metadata": metadata,
                "deleted": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        for link_type, target_id in links or []:
            self.attach(file_id, FileLinkCreate(link_type=link_type, target_id=target_id))
        return item

    def get(self, file_id: str) -> dict:
        item = store.get_file(file_id)
        if not item:
            raise LookupError("File not found.")
        return item

    def download_path(self, file_id: str) -> Path:
        item = self.get(file_id)
        path = Path(item["storage_path"]).resolve()
        root = self.storage_root.resolve()
        metadata = item.get("metadata", {})
        if metadata.get("source") == "local_repo":
            from app.services.repos.store import get_repo

            repo = get_repo(metadata.get("repo_id", ""), include_deleted=True)
            relative_path = metadata.get("relative_path")
            if not repo or not relative_path:
                allowed = False
            else:
                repo_root = Path(repo["workspace_path"]).resolve()
                expected = (repo_root / relative_path).resolve()
                allowed = path == expected and repo_root in path.parents
        else:
            allowed = path.parent == root
        if not allowed or not path.is_file():
            raise LookupError("Stored file is unavailable.")
        return path

    def attach(self, file_id: str, request: FileLinkCreate) -> dict:
        self.get(file_id)
        if not self._target_exists(request.link_type, request.target_id):
            raise LookupError(f"{request.link_type.replace('_', ' ').title()} target not found.")
        return store.insert_link(
            {
                "id": str(uuid.uuid4()),
                "file_id": file_id,
                "link_type": request.link_type,
                "target_id": request.target_id,
                "title": request.title,
                "metadata": request.metadata,
                "created_at": store.now_iso(),
            }
        )

    def summarize(self, file_id: str) -> str:
        item = self.get(file_id)
        text = (item.get("extracted_text") or "").strip()
        if not text:
            raise ValueError("This file has no previewable text to summarize.")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        excerpt = " ".join(lines[:12])[:1800]
        summary = f"{item['display_name']}: {excerpt}"
        if item.get("metadata", {}).get("truncated"):
            summary += " (Summary is based on a truncated preview.)"
        store.update_file(file_id, {"summary": summary})
        return summary

    def create_artifact(self, request: ArtifactCreate) -> dict:
        self._validate_optional_targets(request)
        now = store.now_iso()
        return store.insert_artifact(
            {
                "id": str(uuid.uuid4()),
                **request.model_dump(),
                "created_at": now,
                "updated_at": now,
            }
        )

    def context_for_task(
        self, task_id: str, project_id: str | None = None, max_chars: int = 9000
    ) -> tuple[str, list[str]]:
        candidates, seen = [], set()
        for link_type, target_id in (("task", task_id), ("project", project_id)):
            if not target_id:
                continue
            files, _ = store.list_files(link_type=link_type, target_id=target_id, limit=50)
            for item in files:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    candidates.append(item)
        blocks, considered, used = [], [], 0
        for item in candidates:
            label = item.get("metadata", {}).get("relative_path") or item["display_name"]
            summary = (item.get("summary") or "").strip()
            excerpt = (item.get("extracted_text") or "").strip()
            body = summary or excerpt[:1800] or "Preview not supported."
            block = f"File: {label} ({item.get('extension') or 'unknown'})\n{body}"
            if used + len(block) > max_chars:
                detail = summary[:400] or "listed; context limit reached"
                block = f"File: {label} — {detail}"
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            considered.append(f"- {label} — linked workspace file")
            used += len(block)
        return "\n\n".join(blocks), considered

    def context_for_prompt(self, prompt: str, max_chars: int = 6000) -> str:
        lowered = prompt.lower()
        if not re.search(
            r"\b(file|code|readme|source|config|\.py|\.js|\.ts|\.md|\.json|\.ya?ml)\b", lowered
        ):
            return "No file context loaded."
        items, _ = store.list_files(limit=100)
        words = {
            word
            for word in re.findall(r"[a-zA-Z0-9_.-]{3,}", lowered)
            if word not in {"this", "that", "file", "what", "does", "where", "find", "summarize"}
        }
        ranked = []
        for item in items:
            label = item.get("metadata", {}).get("relative_path") or item["display_name"]
            name = label.lower()
            summary_text = (item.get("summary") or "").lower()
            extracted_text = (item.get("extracted_text") or "")[:20000].lower()
            haystack = f"{name} {summary_text} {extracted_text}"
            score = sum(4 if word in name else 1 for word in words if word in haystack)
            if score or (len(items) == 1 and re.search(r"\b(this|the) file\b", lowered)):
                ranked.append((score, item))
        if not ranked:
            return "No matching uploaded file context found."
        blocks, used = [], 0
        for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:4]:
            label = item.get("metadata", {}).get("relative_path") or item["display_name"]
            body = (
                item.get("summary")
                or (item.get("extracted_text") or "")[:1800]
                or "Preview not supported."
            )
            block = f"[{label}]\n{body}"
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return "Uploaded file context (cite filenames in the answer):\n" + "\n\n".join(blocks)

    @staticmethod
    def _target_exists(link_type: str, target_id: str) -> bool:
        if link_type == "project":
            return get_project(target_id) is not None
        if link_type == "task":
            return get_task(target_id) is not None
        if link_type == "note":
            return get_note(target_id) is not None
        if link_type == "agent_run":
            from app.services.agents import store as agent_store

            return agent_store.get_run(target_id) is not None
        return False

    def _validate_optional_targets(self, request: ArtifactCreate) -> None:
        for link_type, target_id in (
            ("project", request.project_id),
            ("task", request.task_id),
            ("note", request.note_id),
            ("agent_run", request.agent_run_id),
        ):
            if target_id and not self._target_exists(link_type, target_id):
                raise LookupError(f"{link_type.replace('_', ' ').title()} target not found.")
