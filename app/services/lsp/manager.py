from __future__ import annotations

from pathlib import Path

from app.services.lsp.client import JsonRpcClient


class LSPManager:
    """Injectable registry for one LSP client per workspace and language."""

    def __init__(self, client_factory=JsonRpcClient):
        self.client_factory = client_factory
        self.clients: dict[tuple[str, str], object] = {}
        self.roots: dict[tuple[str, str], Path] = {}

    def attach(
        self, workspace_id: str, language: str, client: object, root: Path | None = None
    ) -> None:
        key = (workspace_id, language)
        old = self.clients.get(key)
        if old is not None and old is not client:
            old.close()
        self.clients[key] = client
        if root is not None:
            self.roots[key] = root.resolve()

    def get(self, workspace_id: str, language: str):
        return self.clients.get((workspace_id, language))

    def root(self, workspace_id: str, language: str) -> Path | None:
        return self.roots.get((workspace_id, language))

    def stop(self, workspace_id: str) -> None:
        for key in [key for key in self.clients if key[0] == workspace_id]:
            self.clients.pop(key).close()
            self.roots.pop(key, None)
