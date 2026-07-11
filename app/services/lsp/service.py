from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from app.services.code_index import store as code_index_store
from app.services.context_memory.redaction import redact
from app.services.lsp import store
from app.services.lsp.diagnostics import publish
from app.services.lsp.manager import LSPManager
from app.services.repos import store as repo_store

SERVERS = {
    "python": ("pyright-langserver", ["--stdio"]),
    "python-pylsp": ("pylsp", []),
    "typescript": ("typescript-language-server", ["--stdio"]),
    "javascript": ("typescript-language-server", ["--stdio"]),
    "c": ("clangd", []),
}

METHODS = {
    "hover": "textDocument/hover",
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "document-symbols": "textDocument/documentSymbol",
    "workspace-symbols": "workspace/symbol",
    "rename-preview": "textDocument/prepareRename",
}

DEFAULT_MANAGER = LSPManager()


class LSPService:
    """Safe LSP protocol facade limited to registered managed workspaces."""

    def __init__(self, manager: LSPManager | None = None, request_timeout: float = 10):
        self.manager = manager or DEFAULT_MANAGER
        self.request_timeout = request_timeout

    def servers(self):
        return [
            {"language": key, "command": value[0], "available": shutil.which(value[0]) is not None}
            for key, value in SERVERS.items()
        ]

    def status(self):
        return {"enabled": True, "servers": self.servers(), "sessions": store.sessions()}

    def start(self, workspace_id: str, language: str = "python"):
        if language not in SERVERS:
            raise ValueError("Language server is not allowlisted.")
        command, arguments = SERVERS[language]
        executable = shutil.which(command)
        if not executable:
            store.save_session(workspace_id, language, command, "unavailable", "command not found")
            return self._session_result(workspace_id, language, "unavailable", "command not found")

        root = self._workspace_root(workspace_id)
        if root is None:
            store.save_session(
                workspace_id,
                language,
                command,
                "unavailable",
                "managed workspace not found",
            )
            return self._session_result(
                workspace_id, language, "unavailable", "managed workspace not found"
            )

        try:
            client = self.manager.client_factory(
                [executable, *arguments], str(root), timeout=self.request_timeout
            )
            self._set_notification_handler(client, workspace_id, language, root)
            capabilities = client.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": root.as_uri(),
                    "capabilities": {},
                    "clientInfo": {"name": "neo", "version": "protocol-core"},
                },
            )
            client.notify("initialized", {})
            self.manager.attach(workspace_id, language, client, root)
        except (OSError, RuntimeError, TimeoutError) as exc:
            if "client" in locals():
                client.close()
            reason = self._safe_error(exc)
            store.save_session(workspace_id, language, command, "unavailable", reason)
            return self._session_result(workspace_id, language, "unavailable", reason)

        store.save_session(workspace_id, language, command, "running")
        return self._session_result(
            workspace_id, language, "running", capabilities=redact(capabilities)
        )

    def stop(self, workspace_id: str):
        self.manager.stop(workspace_id)
        for session in store.sessions(workspace_id):
            store.save_session(
                workspace_id, session["language"], session["server_command"], "stopped"
            )
        return {"workspace_id": workspace_id, "status": "stopped"}

    def path(self, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("Path must be a managed-workspace relative path.")
        return path.as_posix()

    def open_document(self, workspace_id: str, file_path: str, language: str, text: str = ""):
        relative_path = self.path(file_path)
        client = self.manager.get(workspace_id, language)
        root = self.manager.root(workspace_id, language)
        if client is None or root is None:
            return self._degraded(workspace_id, relative_path)
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": self._uri(root, relative_path),
                    "languageId": language,
                    "version": 1,
                    "text": text,
                }
            },
        )
        return {"workspace_id": workspace_id, "file_path": relative_path, "status": "available"}

    def query(
        self,
        workspace_id: str,
        file_path: str,
        *,
        action: str = "hover",
        language: str = "python",
        line: int = 0,
        character: int = 0,
        query: str = "",
        text: str = "",
        **_: Any,
    ):
        relative_path = "" if action == "workspace-symbols" else self.path(file_path)
        if action == "did-open":
            return self.open_document(workspace_id, relative_path, language, text)
        if action not in METHODS:
            raise ValueError("Unsupported LSP query action.")
        if line < 0 or character < 0:
            raise ValueError("Position values must not be negative.")

        client = self.manager.get(workspace_id, language)
        root = self.manager.root(workspace_id, language)
        if client is None or root is None:
            return self._degraded(workspace_id, relative_path)
        params: dict[str, Any] = {
            "textDocument": {"uri": self._uri(root, relative_path)},
            "position": {"line": line, "character": character},
        }
        if action == "workspace-symbols":
            params = {"query": query}
        elif action == "references":
            params["context"] = {"includeDeclaration": True}
        result = client.request(METHODS[action], params)
        return {
            "workspace_id": workspace_id,
            "file_path": relative_path,
            "status": "available",
            "result": self._safe_response(result, root),
            "rename_preview": action == "rename-preview",
        }

    def publish_diagnostics(self, workspace_id, file_path, language, diagnostics):
        publish(workspace_id, self.path(file_path), language, diagnostics)

    def get_lsp_symbol_context(
        self, workspace_id: str, file_path: str, line: int = 0, character: int = 0
    ) -> dict:
        """Return compact, read-only LSP and static-symbol context for a coding run."""
        relative_path = self.path(file_path)
        try:
            static_symbols, _ = code_index_store.list_symbols(
                workspace_id, relative_path=relative_path, limit=20
            )
        except sqlite3.Error:  # Static indexing is optional for LSP fallback context.
            static_symbols = []
        diagnostics = store.diagnostics(workspace_id)
        diagnostics = [item for item in diagnostics if item["file_path"] == relative_path][:20]
        session = next(
            (item for item in store.sessions(workspace_id) if item["status"] == "running"), None
        )
        root = self.manager.root(workspace_id, "python")
        result = {
            "static_symbols": self._safe_response(static_symbols, root)
            if root
            else redact(static_symbols),
            "diagnostics": redact(diagnostics),
            "hover": None,
            "definition": None,
            "references": None,
            "document_symbols": None,
        }
        if not session:
            return {
                "lsp_context_used": False,
                "lsp_session_id": None,
                "lsp_degraded_reason": "LSP server unavailable; static symbols remain available.",
                "context": result,
            }
        language = session["language"]
        try:
            for action, key in (
                ("hover", "hover"),
                ("definition", "definition"),
                ("references", "references"),
                ("document-symbols", "document_symbols"),
            ):
                response = self.query(
                    workspace_id,
                    relative_path,
                    action=action,
                    language=language,
                    line=line,
                    character=character,
                )
                result[key] = response["result"]
            return {
                "lsp_context_used": True,
                "lsp_session_id": session["id"],
                "lsp_degraded_reason": None,
                "context": result,
            }
        except (RuntimeError, TimeoutError, ValueError) as exc:
            return {
                "lsp_context_used": False,
                "lsp_session_id": session["id"],
                "lsp_degraded_reason": self._safe_error(exc),
                "context": result,
            }

    def _set_notification_handler(
        self, client, workspace_id: str, language: str, root: Path
    ) -> None:
        def handler(method: str, params: dict) -> None:
            if method != "textDocument/publishDiagnostics":
                return
            uri = str(params.get("uri", ""))
            relative_path = self._relative_uri(root, uri)
            if relative_path is not None:
                self.publish_diagnostics(
                    workspace_id,
                    relative_path,
                    language,
                    params.get("diagnostics", []),
                )

        client.notification_handler = handler

    @staticmethod
    def _workspace_root(workspace_id: str) -> Path | None:
        repo = repo_store.get_repo(workspace_id)
        if not repo:
            return None
        try:
            root = Path(repo["workspace_path"]).resolve(strict=True)
        except (KeyError, OSError):
            return None
        return root if root.is_dir() else None

    @staticmethod
    def _uri(root: Path, relative_path: str) -> str:
        candidate = (root / relative_path).resolve()
        if root not in (candidate, *candidate.parents):
            raise ValueError("Path must be a managed-workspace relative path.")
        return candidate.as_uri()

    def _relative_uri(self, root: Path, uri: str) -> str | None:
        try:
            candidate = Path(uri.removeprefix("file://")).resolve()
            return candidate.relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def _safe_response(self, value: Any, root: Path | None) -> Any:
        if isinstance(value, str):
            relative = (
                self._relative_uri(root, value) if root and value.startswith("file://") else None
            )
            if relative is not None:
                return f"file:///managed/{relative}"
            return redact(value)
        if isinstance(value, list):
            return [self._safe_response(item, root) for item in value]
        if isinstance(value, dict):
            return {str(key): self._safe_response(item, root) for key, item in value.items()}
        return redact(value)

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        return str(redact(str(error))) or "LSP process failed"

    @staticmethod
    def _session_result(workspace_id, language, status, reason=None, capabilities=None):
        return {
            "workspace_id": workspace_id,
            "language": language,
            "status": status,
            "reason": reason,
            "capabilities": capabilities,
        }

    @staticmethod
    def _degraded(workspace_id: str, file_path: str):
        return {
            "workspace_id": workspace_id,
            "file_path": file_path,
            "status": "unavailable",
            "result": None,
            "reason": "LSP server unavailable; static symbols remain available.",
        }
