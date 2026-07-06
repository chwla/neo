from __future__ import annotations

import re
import uuid

from app.services.code_index import store
from app.services.code_index.indexer import index_repo_file
from app.services.code_index.summarizer import summarize_file
from app.services.code_index.types import CodeIndexBuildRequest
from app.services.files import store as file_store
from app.services.files.service import WorkspaceFilesService
from app.services.repos import store as repo_store


class CodeIndexService:
    def build(self, repo_id: str, request: CodeIndexBuildRequest) -> dict:
        repo = repo_store.get_repo(repo_id)
        if not repo:
            raise LookupError("Repository not found or has been deleted.")
        repo_files, total = repo_store.list_repo_files(repo_id, limit=500)
        if not repo_files:
            raise ValueError("Repository has no indexed workspace files.")
        existing = store.get_index(repo_id)
        if existing and not request.force:
            return existing
        now = file_store.now_iso()
        index_id = existing["id"] if existing else str(uuid.uuid4())
        created_at = existing["created_at"] if existing else now
        store.upsert_index(
            {
                "id": index_id,
                "repo_id": repo_id,
                "status": "building",
                "file_count": total,
                "indexed_file_count": 0,
                "symbol_count": 0,
                "dependency_count": 0,
                "route_count": 0,
                "metadata": {},
                "created_at": created_at,
                "updated_at": now,
                "indexed_at": None,
            }
        )
        store.clear_repo_index(repo_id)
        indexed = symbol_count = dependency_count = route_count = 0
        errors: list[dict] = []
        for mapping in repo_files:
            try:
                result, dependencies = index_repo_file(mapping, repo_files)
                parent_ids: dict[str, str] = {}
                for symbol in result.symbols:
                    symbol_id = str(uuid.uuid4())
                    store.insert_symbol(
                        {
                            "id": symbol_id,
                            "repo_id": repo_id,
                            "repo_file_id": mapping["id"],
                            "file_id": mapping["file_id"],
                            "relative_path": mapping["relative_path"],
                            "name": symbol.name,
                            "qualified_name": symbol.qualified_name,
                            "symbol_type": symbol.symbol_type,
                            "language": result.language,
                            "line_start": symbol.line_start,
                            "line_end": symbol.line_end,
                            "signature": symbol.signature,
                            "parent_symbol_id": parent_ids.get(symbol.parent_name or ""),
                            "doc_text": symbol.doc_text,
                            "metadata": symbol.metadata,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    parent_ids.setdefault(symbol.name, symbol_id)
                    symbol_count += 1
                    route_count += symbol.symbol_type == "api_route"
                for dependency in dependencies:
                    store.insert_dependency(
                        {
                            "id": str(uuid.uuid4()),
                            "repo_id": repo_id,
                            "source_repo_file_id": mapping["id"],
                            "source_relative_path": mapping["relative_path"],
                            **dependency,
                            "created_at": now,
                        }
                    )
                    dependency_count += 1
                summary, purpose, keys = summarize_file(mapping["relative_path"], result)
                store.insert_summary(
                    {
                        "id": str(uuid.uuid4()),
                        "repo_id": repo_id,
                        "repo_file_id": mapping["id"],
                        "file_id": mapping["file_id"],
                        "relative_path": mapping["relative_path"],
                        "language": result.language,
                        "summary": summary,
                        "purpose": purpose,
                        "key_symbols": keys,
                        "metadata": {"source_sha256": mapping.get("sha256")},
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                indexed += 1
            except Exception as exc:
                errors.append({"relative_path": mapping["relative_path"], "error": str(exc)})
        status = "partial" if errors and indexed else "failed" if errors else "ready"
        return store.upsert_index(
            {
                "id": index_id,
                "repo_id": repo_id,
                "status": status,
                "file_count": total,
                "indexed_file_count": indexed,
                "symbol_count": symbol_count,
                "dependency_count": dependency_count,
                "route_count": route_count,
                "metadata": {"errors": errors},
                "created_at": created_at,
                "updated_at": now,
                "indexed_at": now,
            }
        )

    def get(self, repo_id: str) -> dict:
        if not repo_store.get_repo(repo_id):
            raise LookupError("Repository not found or has been deleted.")
        item = store.get_index(repo_id)
        if not item:
            raise LookupError("Codebase index has not been built.")
        return item

    def search(self, repo_id: str, query: str, limit: int = 50) -> list[dict]:
        self.get(repo_id)
        terms = [term for term in re.findall(r"[A-Za-z0-9_./-]{2,}", query.lower())][:12]
        symbols, _ = store.list_symbols(repo_id, limit=500)
        results = []
        for symbol in symbols:
            result = self._symbol_result(symbol, terms)
            if result["score"]:
                results.append(result)
        for summary in store.list_summaries(repo_id):
            haystack = " ".join(
                [
                    summary["relative_path"],
                    summary["summary"],
                    summary.get("purpose") or "",
                    *summary.get("key_symbols", []),
                ]
            ).lower()
            score = _score(haystack, terms)
            if score:
                results.append(
                    {
                        "result_type": "file",
                        "score": score,
                        "relative_path": summary["relative_path"],
                        "repo_file_id": summary["repo_file_id"],
                        "file_id": summary["file_id"],
                        "name": summary["relative_path"],
                        "symbol_type": None,
                        "summary": summary["summary"],
                        "line_start": None,
                        "line_end": None,
                    }
                )
        for dependency in store.list_dependencies(repo_id):
            haystack = " ".join(
                [
                    dependency["source_relative_path"],
                    dependency.get("target_relative_path") or "",
                    dependency["import_text"],
                ]
            ).lower()
            score = _score(haystack, terms)
            if score:
                results.append(
                    {
                        "result_type": "dependency",
                        "score": score,
                        "relative_path": dependency["source_relative_path"],
                        "name": dependency["import_text"],
                        "symbol_type": "dependency",
                        "summary": dependency.get("target_relative_path") or "External dependency",
                        "line_start": dependency.get("metadata", {}).get("line_start"),
                        "line_end": None,
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["relative_path"], item["name"]))
        return results[:limit]

    def routes(self, repo_id: str) -> list[dict]:
        self.get(repo_id)
        symbols, _ = store.list_symbols(repo_id, symbol_type="api_route", limit=500)
        return [
            {
                "id": item["id"],
                "method": item.get("metadata", {}).get("method"),
                "path": item.get("metadata", {}).get("path"),
                "handler": item.get("metadata", {}).get("handler"),
                "relative_path": item["relative_path"],
                "line_start": item["line_start"],
            }
            for item in symbols
        ]

    def file_summary(self, repo_id: str, repo_file_id: str) -> dict:
        self.get(repo_id)
        mapping = repo_store.get_repo_file(repo_file_id)
        if not mapping or mapping["repo_id"] != repo_id:
            raise LookupError("Repository file not found.")
        summary = store.get_summary(repo_file_id)
        if not summary:
            raise LookupError("File has not been indexed.")
        symbols, _ = store.list_symbols(repo_id, relative_path=mapping["relative_path"], limit=500)
        return {
            "summary": summary,
            "symbols": symbols,
            "dependencies": store.list_dependencies(repo_id, mapping["relative_path"]),
        }

    def symbol_detail(self, symbol_id: str) -> dict:
        symbol = store.get_symbol(symbol_id)
        if not symbol:
            raise LookupError("Code symbol not found.")
        return {
            "symbol": symbol,
            "file": WorkspaceFilesService().get(symbol["file_id"]),
            "dependencies": store.list_dependencies(symbol["repo_id"], symbol["relative_path"]),
        }

    def context_for_prompt(self, prompt: str, max_chars: int = 6000) -> str:
        if not _looks_like_codebase_question(prompt):
            return "No codebase index context requested."
        repos, _ = repo_store.list_repos(limit=20)
        blocks: list[str] = []
        for repo in repos:
            if not store.get_index(repo["id"]):
                continue
            results = self.search(repo["id"], prompt, limit=8)
            if not results:
                continue
            lines = [f"Repo: {repo['name']}"]
            for item in results:
                location = f":{item['line_start']}" if item.get("line_start") else ""
                lines.append(
                    f"- {item['relative_path']}{location} — {item['name']} — "
                    f"{item.get('summary') or item.get('symbol_type')}"
                )
            block = "\n".join(lines)
            if sum(map(len, blocks)) + len(block) > max_chars:
                break
            blocks.append(block)
        if not blocks:
            return "I need a registered and indexed repo to answer codebase questions accurately."
        return "Codebase index context (cite relative paths and lines):\n" + "\n\n".join(blocks)

    def suggest_file_ids(self, project_id: str, objective: str, limit: int = 4) -> list[str]:
        repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
        suggestions: list[tuple[float, str]] = []
        for repo in repos:
            if not store.get_index(repo["id"]):
                continue
            for result in self.search(repo["id"], objective, limit=12):
                file_id = result.get("file_id")
                if file_id:
                    suggestions.append((result["score"], file_id))
        ordered: list[str] = []
        for _, file_id in sorted(suggestions, reverse=True):
            if file_id not in ordered:
                ordered.append(file_id)
        return ordered[:limit]

    def context_for_project(
        self, project_id: str, query: str, max_chars: int = 7000
    ) -> tuple[str, list[str]]:
        repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
        lines: list[str] = []
        considered: list[str] = []
        used = 0
        for repo in repos:
            index = store.get_index(repo["id"])
            if not index:
                continue
            lines.append(f"Repo: {repo['name']} [{index['status']}]")
            for result in self.search(repo["id"], query, limit=8):
                path = result["relative_path"]
                location = f":{result['line_start']}" if result.get("line_start") else ""
                line = f"- {path}{location} — {result['name']}"
                if used + len(line) > max_chars:
                    break
                lines.append(line)
                used += len(line)
                if path not in considered:
                    considered.append(path)
            for file_id in self.suggest_file_ids(project_id, query, limit=3):
                item = file_store.get_file(file_id)
                if not item:
                    continue
                path = item.get("metadata", {}).get("relative_path") or item["display_name"]
                excerpt = (item.get("extracted_text") or "")[:1200]
                block = f"Excerpt from {path}:\n{excerpt}"
                if used + len(block) > max_chars:
                    break
                lines.append(block)
                used += len(block)
        if not lines:
            return "No codebase index is built for this project; using file context fallback.", []
        return "\n".join(lines), considered

    @staticmethod
    def _symbol_result(symbol: dict, terms: list[str]) -> dict:
        haystack = " ".join(
            [
                symbol["relative_path"],
                symbol["name"],
                symbol.get("qualified_name") or "",
                symbol.get("signature") or "",
                symbol.get("doc_text") or "",
            ]
        ).lower()
        return {
            "result_type": "route" if symbol["symbol_type"] == "api_route" else "symbol",
            "score": _score(haystack, terms),
            "relative_path": symbol["relative_path"],
            "repo_file_id": symbol["repo_file_id"],
            "file_id": symbol["file_id"],
            "name": symbol["name"],
            "symbol_type": symbol["symbol_type"],
            "summary": symbol.get("signature") or symbol.get("doc_text"),
            "line_start": symbol["line_start"],
            "line_end": symbol["line_end"],
        }


def _score(haystack: str, terms: list[str]) -> float:
    if not terms:
        return 0.0
    return round(sum(1 for term in terms if term in haystack) / len(terms), 3)


def _looks_like_codebase_question(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(codebase|repo|repository|implemented|function|class|component|route|api|"
            r"imports?|depends?|module|source file|where is|which files?)\b",
            prompt,
            re.I,
        )
    )
