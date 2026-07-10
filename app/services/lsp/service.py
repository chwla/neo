from __future__ import annotations

import shutil
from pathlib import PurePosixPath

from app.services.lsp import store

SERVERS = {
    "python": ("pyright-langserver", ["--stdio"]),
    "typescript": ("typescript-language-server", ["--stdio"]),
    "javascript": ("typescript-language-server", ["--stdio"]),
    "c": ("clangd", []),
}


class LSPService:
    def servers(self):
        return [
            {"language": k, "command": v[0], "available": shutil.which(v[0]) is not None}
            for k, v in SERVERS.items()
        ]

    def status(self):
        return {"enabled": True, "servers": self.servers(), "sessions": store.sessions()}

    def start(self, w, language="python"):
        if language not in SERVERS:
            raise ValueError("Language server is not allowlisted.")
        cmd = SERVERS[language][0]
        available = shutil.which(cmd) is not None
        store.save_session(
            w,
            language,
            cmd,
            "available" if available else "unavailable",
            None if available else "command not found",
        )
        return {
            "workspace_id": w,
            "language": language,
            "status": "available" if available else "unavailable",
            "reason": None if available else "command not found",
        }

    def stop(self, w):
        for s in store.sessions(w):
            store.save_session(w, s["language"], s["server_command"], "stopped")
        return {"workspace_id": w, "status": "stopped"}

    def path(self, p):
        x = PurePosixPath(p)
        if x.is_absolute() or ".." in x.parts:
            raise ValueError("Path must be a managed-workspace relative path.")
        return str(x)

    def query(self, w, p, **_):
        return {
            "workspace_id": w,
            "file_path": self.path(p),
            "status": "unavailable",
            "result": None,
            "reason": "LSP server unavailable; static symbols remain available.",
        }
