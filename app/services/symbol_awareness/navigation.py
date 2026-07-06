from __future__ import annotations

from app.services.code_index import store as index_store


def rank_definitions(
    repo_id: str,
    name: str,
    relative_path: str | None = None,
    line: int | None = None,
) -> list[dict]:
    symbols, _ = index_store.list_symbols(repo_id, q=name, limit=500)
    exact = [
        item
        for item in symbols
        if item["name"].lower() == name.lower() and item["symbol_type"] not in {"import", "export"}
    ]
    results: list[dict] = []
    for symbol in exact:
        confidence = 0.84
        if relative_path and symbol["relative_path"] == relative_path:
            confidence += 0.12
        if line and symbol.get("line_start"):
            confidence += max(0.0, 0.04 - abs(symbol["line_start"] - line) / 10000)
        results.append(
            {
                "symbol_id": symbol["id"],
                "name": symbol["name"],
                "qualified_name": symbol.get("qualified_name"),
                "symbol_type": symbol["symbol_type"],
                "relative_path": symbol["relative_path"],
                "repo_file_id": symbol["repo_file_id"],
                "file_id": symbol["file_id"],
                "line_start": symbol.get("line_start"),
                "line_end": symbol.get("line_end"),
                "signature": symbol.get("signature"),
                "confidence": min(confidence, 1.0),
            }
        )
    return sorted(results, key=lambda item: (-item["confidence"], item["relative_path"]))


def document_symbols(repo_id: str, relative_path: str) -> list[dict]:
    symbols, _ = index_store.list_symbols(repo_id, relative_path=relative_path, limit=500)
    return sorted(symbols, key=lambda item: (item.get("line_start") or 0, item["name"]))
