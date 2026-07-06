from __future__ import annotations

import re
import uuid

from app.services.code_index import store as index_store
from app.services.code_index.service import CodeIndexService
from app.services.files import store as file_store
from app.services.repos import store as repo_store
from app.services.symbol_awareness import store
from app.services.symbol_awareness.navigation import document_symbols, rank_definitions
from app.services.symbol_awareness.reference_extractor import extract_references
from app.services.symbol_awareness.related_files import compute_related_files
from app.services.symbol_awareness.resolver import (
    containing_symbol,
    relationship_type,
    resolve_reference,
)
from app.services.symbol_awareness.safety import awareness_text
from app.services.symbol_awareness.types import SymbolAwarenessBuildRequest


class SymbolAwarenessService:
    def build(self, repo_id: str, request: SymbolAwarenessBuildRequest) -> dict:
        repo = repo_store.get_repo(repo_id)
        if not repo:
            raise LookupError("Repository not found or has been deleted.")
        if not index_store.get_index(repo_id):
            raise ValueError("Build Codebase Index before building Symbol Awareness.")
        existing = store.get_status(repo_id)
        if existing and existing.get("status") in {"ready", "partial"} and not request.force:
            return {"status": existing["status"], "stats": store.stats(repo_id)}
        now = file_store.now_iso()
        store.set_status(repo_id, "building", {"updated_at": now})
        store.clear_repo(repo_id)
        repo_files, _ = repo_store.list_repo_files(repo_id, limit=500)
        symbols, _ = index_store.list_symbols(repo_id, limit=500)
        dependencies = index_store.list_dependencies(repo_id)
        errors: list[dict] = []
        persisted_references: list[dict] = []
        relationship_keys: set[tuple[str, str, str]] = set()

        for mapping in repo_files:
            file_item = file_store.get_file(mapping["file_id"])
            if not file_item:
                errors.append(
                    {"relative_path": mapping["relative_path"], "error": "Workspace file missing."}
                )
                continue
            try:
                references = extract_references(mapping["relative_path"], awareness_text(file_item))
            except Exception as exc:
                errors.append({"relative_path": mapping["relative_path"], "error": str(exc)})
                continue
            for reference in references:
                target, confidence = resolve_reference(reference, mapping, symbols, dependencies)
                item = {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "symbol_id": target["id"] if target else None,
                    "referenced_name": reference.name,
                    "reference_type": reference.reference_type,
                    "source_repo_file_id": mapping["id"],
                    "source_file_id": mapping["file_id"],
                    "source_relative_path": mapping["relative_path"],
                    "line_start": reference.line_start,
                    "line_end": reference.line_end,
                    "column_start": reference.column_start,
                    "column_end": reference.column_end,
                    "context_text": reference.context_text,
                    "resolved": target is not None,
                    "confidence": confidence,
                    "metadata": reference.metadata,
                    "created_at": now,
                    "updated_at": now,
                }
                store.insert_reference(item)
                item["target_repo_file_id"] = target["repo_file_id"] if target else None
                persisted_references.append(item)
                source_symbol = containing_symbol(symbols, mapping["id"], reference.line_start)
                if source_symbol and target and source_symbol["id"] != target["id"]:
                    self._insert_relationship(
                        repo_id,
                        source_symbol["id"],
                        target["id"],
                        relationship_type(reference.reference_type),
                        confidence,
                        now,
                        relationship_keys,
                    )

        self._route_relationships(repo_id, symbols, now, relationship_keys)
        for related in compute_related_files(repo_files, dependencies, persisted_references):
            store.insert_related_file(
                {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "source_repo_file_id": related["source_repo_file_id"],
                    "target_repo_file_id": related["target_repo_file_id"],
                    "relationship_type": related["relationship_type"],
                    "score": related["score"],
                    "metadata": {"reasons": related["reasons"]},
                    "created_at": now,
                    "updated_at": now,
                }
            )
        stats = store.stats(repo_id)
        status = "partial" if errors else "ready"
        store.set_status(
            repo_id,
            status,
            {"updated_at": now, "indexed_at": now, "errors": errors, **stats},
        )
        return {"status": status, "stats": stats, "errors": errors}

    def status(self, repo_id: str) -> dict:
        if not repo_store.get_repo(repo_id):
            raise LookupError("Repository not found or has been deleted.")
        if not index_store.get_index(repo_id):
            return {
                "status": "not_ready",
                "error": "Build Codebase Index before building Symbol Awareness.",
                "stats": store.stats(repo_id),
            }
        status = store.get_status(repo_id)
        if not status:
            return {"status": "not_built", "stats": store.stats(repo_id)}
        return {"status": status["status"], "stats": store.stats(repo_id), "metadata": status}

    def definitions(
        self,
        repo_id: str,
        name: str,
        relative_path: str | None = None,
        line: int | None = None,
    ) -> list[dict]:
        self._require_ready(repo_id)
        return rank_definitions(repo_id, name, relative_path, line)

    def references_for_symbol(
        self, symbol_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[list[dict], int]:
        symbol = index_store.get_symbol(symbol_id)
        if not symbol:
            raise LookupError("Code symbol not found.")
        self._require_ready(symbol["repo_id"])
        return store.list_references(symbol_id=symbol_id, limit=limit, offset=offset)

    def references_by_name(
        self,
        repo_id: str,
        name: str,
        reference_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        self._require_ready(repo_id)
        return store.list_references(
            repo_id=repo_id,
            name=name,
            reference_type=reference_type,
            limit=limit,
            offset=offset,
        )

    def document_symbols(self, repo_id: str, repo_file_id: str) -> list[dict]:
        self._require_ready(repo_id)
        mapping = self._mapping(repo_id, repo_file_id)
        return document_symbols(repo_id, mapping["relative_path"])

    def related_files(self, repo_id: str, repo_file_id: str) -> list[dict]:
        self._require_ready(repo_id)
        self._mapping(repo_id, repo_file_id)
        return store.list_related_files(repo_id, repo_file_id)

    def symbol_context(self, symbol_id: str) -> dict:
        symbol = index_store.get_symbol(symbol_id)
        if not symbol:
            raise LookupError("Code symbol not found.")
        self._require_ready(symbol["repo_id"])
        file_item = file_store.get_file(symbol["file_id"])
        text = awareness_text(file_item) if file_item else ""
        lines = text.splitlines()
        start = max(0, (symbol.get("line_start") or 1) - 4)
        end = min(len(lines), (symbol.get("line_end") or symbol.get("line_start") or 1) + 3)
        references, _ = store.list_references(symbol_id=symbol_id, limit=100)
        related = store.list_related_files(symbol["repo_id"], symbol["repo_file_id"])
        dependencies = index_store.list_dependencies(symbol["repo_id"], symbol["relative_path"])
        summary = index_store.get_summary(symbol["repo_file_id"])
        return {
            "symbol": symbol,
            "definition_excerpt": "\n".join(lines[start:end])[:4000],
            "references": references,
            "related_files": related,
            "dependencies": dependencies,
            "summary": summary.get("summary") if summary else None,
        }

    def context_for_prompt(self, prompt: str, max_chars: int = 6000) -> str:
        if not _symbol_intent(prompt):
            return "No symbol navigation context requested."
        names = _candidate_names(prompt)
        repos, _ = repo_store.list_repos(limit=20)
        blocks: list[str] = []
        for repo in repos:
            if not store.get_status(repo["id"]):
                continue
            lines = [f"Repo: {repo['name']}"]
            for name in names[:6]:
                definitions = rank_definitions(repo["id"], name)[:3]
                references, _ = store.list_references(repo_id=repo["id"], name=name, limit=8)
                for definition in definitions:
                    lines.append(
                        f"- Definition {definition['name']}: "
                        f"{definition['relative_path']}:{definition.get('line_start') or '?'}"
                    )
                for reference in references:
                    lines.append(
                        f"- Reference {name}: {reference['source_relative_path']}:"
                        f"{reference.get('line_start') or '?'} — "
                        f"{reference.get('context_text') or ''}"
                    )
            if len(lines) > 1:
                block = "\n".join(lines)
                if sum(map(len, blocks)) + len(block) > max_chars:
                    break
                blocks.append(block)
        if not blocks:
            return (
                "I need Symbol Awareness built for this repo first. Build Codebase Index, "
                "then build Symbol Awareness from the Repos page."
            )
        return "Symbol awareness context (cite paths and lines):\n" + "\n\n".join(blocks)

    def suggest_file_ids(self, project_id: str, objective: str, limit: int = 6) -> list[str]:
        base = CodeIndexService().suggest_file_ids(project_id, objective, limit=4)
        repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
        scored: list[tuple[float, str]] = [(1.0, file_id) for file_id in base]
        for repo in repos:
            if not store.get_status(repo["id"]):
                continue
            for file_id in base:
                mapping = repo_store.get_repo_file_by_file_id(file_id)
                if not mapping or mapping["repo_id"] != repo["id"]:
                    continue
                for related in store.list_related_files(repo["id"], mapping["id"]):
                    scored.append((related["score"], related["target_file_id"]))
            for name in _candidate_names(objective):
                for definition in rank_definitions(repo["id"], name)[:2]:
                    scored.append((definition["confidence"], definition["file_id"]))
        result: list[str] = []
        for _, file_id in sorted(scored, reverse=True):
            if file_id not in result:
                result.append(file_id)
        return result[:limit]

    def context_for_project(
        self, project_id: str, prompt: str, max_chars: int = 6000
    ) -> tuple[str, list[str]]:
        repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
        lines: list[str] = []
        considered: list[str] = []
        for repo in repos:
            if not store.get_status(repo["id"]):
                continue
            lines.append(f"Repo: {repo['name']}")
            for name in _candidate_names(prompt)[:8]:
                for definition in rank_definitions(repo["id"], name)[:2]:
                    path = definition["relative_path"]
                    lines.append(
                        f"- Definition {name}: {path}:{definition.get('line_start') or '?'}"
                    )
                    if path not in considered:
                        considered.append(path)
                    for related in store.list_related_files(repo["id"], definition["repo_file_id"])[
                        :4
                    ]:
                        related_path = related["target_relative_path"]
                        lines.append(f"  Related: {related_path} ({related['score']:.2f})")
                        if related_path not in considered:
                            considered.append(related_path)
                references, _ = store.list_references(repo_id=repo["id"], name=name, limit=6)
                for reference in references:
                    path = reference["source_relative_path"]
                    lines.append(f"- Reference {name}: {path}:{reference.get('line_start') or '?'}")
                    if path not in considered:
                        considered.append(path)
                if sum(len(line) for line in lines) >= max_chars:
                    break
        if not lines:
            return (
                "Symbol Awareness is not built for this project; using Codebase Index fallback.",
                [],
            )
        return "\n".join(lines)[:max_chars], considered

    @staticmethod
    def _insert_relationship(
        repo_id: str,
        source_id: str,
        target_id: str,
        kind: str,
        confidence: float,
        now: str,
        keys: set[tuple[str, str, str]],
    ) -> None:
        key = (source_id, target_id, kind)
        if key in keys:
            return
        keys.add(key)
        store.insert_relationship(
            {
                "id": str(uuid.uuid4()),
                "repo_id": repo_id,
                "source_symbol_id": source_id,
                "target_symbol_id": target_id,
                "relationship_type": kind,
                "confidence": confidence,
                "metadata": {},
                "created_at": now,
            }
        )

    def _route_relationships(
        self,
        repo_id: str,
        symbols: list[dict],
        now: str,
        keys: set[tuple[str, str, str]],
    ) -> None:
        for route in (item for item in symbols if item["symbol_type"] == "api_route"):
            handler = route.get("metadata", {}).get("handler")
            target = next(
                (
                    item
                    for item in symbols
                    if item["repo_file_id"] == route["repo_file_id"]
                    and item["name"] == handler
                    and item["symbol_type"] in {"function", "async_function", "method"}
                ),
                None,
            )
            if target:
                self._insert_relationship(
                    repo_id,
                    target["id"],
                    route["id"],
                    "defines_route",
                    1.0,
                    now,
                    keys,
                )

    @staticmethod
    def _mapping(repo_id: str, repo_file_id: str) -> dict:
        mapping = repo_store.get_repo_file(repo_file_id)
        if not mapping or mapping["repo_id"] != repo_id:
            raise LookupError("Repository file not found.")
        return mapping

    @staticmethod
    def _require_ready(repo_id: str) -> None:
        if not repo_store.get_repo(repo_id):
            raise LookupError("Repository not found or has been deleted.")
        if not index_store.get_index(repo_id):
            raise ValueError("Build Codebase Index before building Symbol Awareness.")
        status = store.get_status(repo_id)
        if not status or status.get("status") not in {"ready", "partial"}:
            raise ValueError("Build Symbol Awareness for this repository first.")


def _symbol_intent(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(defined|definition|references?|usages?|uses|callers?|callees?|related files?|"
            r"implemented|symbol|component used|what should i edit|where is|what uses)\b",
            prompt,
            re.I,
        )
    )


def _candidate_names(prompt: str) -> list[str]:
    quoted = re.findall(r"[`'\"]([A-Za-z_$][\w$]*)[`'\"]", prompt)
    words = re.findall(r"\b[A-Za-z_$][\w$]{2,}\b", prompt)
    stop = {
        "where",
        "what",
        "which",
        "defined",
        "definition",
        "implemented",
        "references",
        "reference",
        "uses",
        "usage",
        "component",
        "function",
        "class",
        "files",
        "should",
        "edit",
        "this",
        "that",
        "from",
        "with",
        "does",
    }
    result: list[str] = []
    for word in [*quoted, *words]:
        if word.lower() not in stop and word not in result:
            result.append(word)
    return result[:12]
