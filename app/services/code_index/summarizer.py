from __future__ import annotations

from collections import Counter

from app.services.code_index.extractors import ExtractionResult


def summarize_file(relative_path: str, result: ExtractionResult) -> tuple[str, str, list[str]]:
    counts = Counter(symbol.symbol_type for symbol in result.symbols)
    phrases = [
        f"{count} {kind.replace('_', ' ')}{'s' if count != 1 else ''}"
        for kind, count in sorted(counts.items())
    ]
    key_symbols = [
        symbol.name
        for symbol in result.symbols
        if symbol.symbol_type not in {"import", "export", "heading"}
    ][:12]
    details = ", ".join(phrases) if phrases else "no named symbols"
    summary = f"This {result.language} file defines {details}."
    if key_symbols:
        summary += f" Key symbols: {', '.join(key_symbols)}."
    purpose = _purpose(relative_path, result)
    return summary, purpose, key_symbols


def _purpose(relative_path: str, result: ExtractionResult) -> str:
    route_count = sum(symbol.symbol_type == "api_route" for symbol in result.symbols)
    component_count = sum(symbol.symbol_type == "component" for symbol in result.symbols)
    if route_count:
        return f"Defines {route_count} API route{'s' if route_count != 1 else ''}."
    if component_count:
        return f"Defines {component_count} frontend component{'s' if component_count != 1 else ''}."
    if relative_path.lower().endswith(("readme.md", "readme.markdown")):
        return "Repository documentation and overview."
    return f"Static {result.language} source or metadata for {relative_path}."
