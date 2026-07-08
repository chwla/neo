from __future__ import annotations

from collections.abc import Callable

from app.services.files import store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate
from app.services.llm import LLMMessage, get_llm_client
from app.services.patch_apply.parser import ParsedPatch, parse_unified_diff
from app.services.patches.diff_builder import (
    fallback_content,
    normalize_single_file_diff,
    proposal_prompt,
)
from app.services.patches.safety import (
    MAX_FILES,
    MAX_SINGLE_FILE_CHARS,
    MAX_TOTAL_CONTEXT_CHARS,
    clean_objective,
    has_reliable_unified_diff,
    remove_execution_claims,
)
from app.services.patches.types import PatchProposalRequest

Generator = Callable[[str], str]


class PatchProposalService:
    def __init__(self, generator: Generator | None = None) -> None:
        self.files = WorkspaceFilesService()
        self.generator = generator or self._generate_with_llm

    def propose(self, request: PatchProposalRequest) -> dict:
        objective = clean_objective(request.objective)
        files = self._resolve_files(request)
        prompt = proposal_prompt(objective, files)
        generation_error: str | None = None
        try:
            generated = remove_execution_claims(self.generator(prompt).strip())
            generated = self._normalize_single_target_diff(generated, files)
        except Exception as exc:
            generation_error = str(exc)
            generated = ""

        filenames = [item["patch_path"] for item in files]
        reliable = has_reliable_unified_diff(generated, filenames)
        parsed: ParsedPatch | None = None
        if reliable:
            try:
                parsed = parse_unified_diff(generated)
                context_paths = set(filenames)
                if any(
                    item.change_type == "modify" and item.filename not in context_paths
                    for item in parsed.files
                ):
                    raise ValueError("A modified file was not present in supplied context.")
            except ValueError as exc:
                generation_error = str(exc)
                reliable = False
        if reliable and generation_error is None:
            content = generated
            if not content.startswith("# Patch Proposal"):
                content = f"# Patch Proposal\n\n## Objective\n{objective}\n\n{content}"
            if "This patch has not been applied." not in content:
                content += "\n\n## Notes\nThis patch has not been applied."
            artifact_type = "patch_proposal"
        else:
            reason = (
                f"Generation failed: {generation_error}"
                if generation_error
                else (
                    "The model did not return a reliable unified diff based on the "
                    "available context."
                )
            )
            content = fallback_content(
                objective,
                files,
                reason,
            )
            artifact_type = "analysis"

        metadata = {
            "target_file_ids": [item["id"] for item in files],
            "target_filenames": filenames,
            "target_files": [
                {
                    "file_id": item["id"],
                    "filename": item["display_name"],
                    "repo_id": item.get("metadata", {}).get("repo_id"),
                    "relative_path": item.get("metadata", {}).get("relative_path"),
                    "sha256_at_proposal": item["sha256"],
                    "original_size_bytes": item["size_bytes"],
                    "repo_file_id": item.get("repo_file_id"),
                }
                for item in files
            ],
            "context_chars": sum(len(item["context_text"]) for item in files),
            "context_limits": {
                "max_files": MAX_FILES,
                "max_total_chars": MAX_TOTAL_CONTEXT_CHARS,
                "max_single_file_chars": MAX_SINGLE_FILE_CHARS,
            },
            "unified_diff": reliable,
            "proposal_only": True,
        }
        if reliable and parsed and (
            len(parsed.files) > 1 or parsed.files[0].change_type == "create"
        ):
            metadata = self._multi_file_metadata(metadata, files, parsed)
        short = objective[:80].rstrip()
        return self.files.create_artifact(
            ArtifactCreate(
                title=f"Proposed patch: {short}",
                artifact_type=artifact_type,
                content=content,
                source_type="patch_proposal",
                source_id=request.agent_run_id or request.task_id,
                project_id=request.project_id,
                task_id=request.task_id,
                agent_run_id=request.agent_run_id,
                metadata=metadata,
            )
        )

    @staticmethod
    def _multi_file_metadata(base: dict, files: list[dict], parsed: ParsedPatch) -> dict:
        by_path = {item["patch_path"]: item for item in files}
        repo_ids = {
            item.get("metadata", {}).get("repo_id")
            for item in files
            if item.get("metadata", {}).get("repo_id")
        }
        if len(repo_ids) != 1:
            raise ValueError("Multi-file patch proposals require one managed repository.")
        repo_id = next(iter(repo_ids))
        patch_files = []
        target_files = []
        for parsed_file in parsed.files:
            source = by_path.get(parsed_file.filename)
            if parsed_file.change_type == "modify":
                if not source:
                    raise ValueError(
                        f"Modified file lacks supplied context: {parsed_file.filename}."
                    )
                item = {
                    "change_type": "modify",
                    "relative_path": parsed_file.filename,
                    "workspace_file_id": source["id"],
                    "repo_file_id": source.get("repo_file_id"),
                    "original_sha256": source["sha256"],
                    "original_size_bytes": source["size_bytes"],
                }
            else:
                item = {
                    "change_type": "create",
                    "relative_path": parsed_file.filename,
                    "expected_absent": True,
                }
            patch_files.append(item)
            target_files.append(
                {
                    **item,
                    "file_id": item.get("workspace_file_id"),
                    "filename": parsed_file.filename.rsplit("/", 1)[-1],
                    "repo_id": repo_id,
                    "sha256_at_proposal": item.get("original_sha256"),
                }
            )
        return {
            **base,
            "schema_version": 2,
            "patch_kind": "multi_file",
            "repo_id": repo_id,
            "files": patch_files,
            "target_files": target_files,
        }

    def _resolve_files(self, request: PatchProposalRequest) -> list[dict]:
        file_ids = list(dict.fromkeys(request.file_ids))
        if not file_ids and request.project_id:
            from app.services.symbol_awareness.service import SymbolAwarenessService

            file_ids.extend(
                SymbolAwarenessService().suggest_file_ids(request.project_id, request.objective)
            )
        if not file_ids:
            for link_type, target_id in (
                ("task", request.task_id),
                ("project", request.project_id),
            ):
                if not target_id:
                    continue
                linked, _ = store.list_files(
                    link_type=link_type, target_id=target_id, limit=MAX_FILES + 1
                )
                file_ids.extend(item["id"] for item in linked if item["id"] not in file_ids)
        if not file_ids:
            raise ValueError("Select at least one uploaded workspace file.")
        if len(file_ids) > MAX_FILES:
            raise ValueError(
                f"Patch proposals support at most {MAX_FILES} files; narrow the scope."
            )

        resolved, total = [], 0
        for file_id in file_ids:
            item = store.get_file(file_id)
            if not item:
                raise LookupError(f"Workspace file not found: {file_id}")
            text = item.get("extracted_text")
            if not text:
                raise ValueError(f"{item['display_name']} has no previewable text.")
            excerpt = text[:MAX_SINGLE_FILE_CHARS]
            if total + len(excerpt) > MAX_TOTAL_CONTEXT_CHARS:
                excerpt = excerpt[: max(0, MAX_TOTAL_CONTEXT_CHARS - total)]
            if not excerpt:
                raise ValueError(
                    "File context exceeds the 100,000-character limit; narrow the scope."
                )
            patch_path = item.get("metadata", {}).get("relative_path") or item["display_name"]
            repo_file_id = None
            if item.get("metadata", {}).get("repo_id"):
                from app.services.repos.store import get_repo_file_by_file_id

                mapping = get_repo_file_by_file_id(item["id"])
                repo_file_id = mapping.get("id") if mapping else None
            resolved.append(
                {
                    **item,
                    "context_text": excerpt,
                    "patch_path": patch_path,
                    "repo_file_id": repo_file_id,
                }
            )
            total += len(excerpt)
        return resolved

    @staticmethod
    def _generate_with_llm(prompt: str) -> str:
        client = get_llm_client(num_predict=2400, timeout=600)
        result = client.chat_with_metadata(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You create reviewable patch proposals only. Use only supplied workspace "
                        "file text. Never claim changes were applied or validation was run."
                    ),
                ),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.1,
            num_predict=2400,
        )
        return result.content

    @staticmethod
    def _normalize_single_target_diff(content: str, files: list[dict]) -> str:
        if "diff --git " in content or content.count("--- ") != 1 or content.count("+++ ") != 1:
            return content
        for item in files:
            path = item["patch_path"]
            if f"--- a/{path}" in content and f"+++ b/{path}" in content:
                return normalize_single_file_diff(content, path)
        return content
