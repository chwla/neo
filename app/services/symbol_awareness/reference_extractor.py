from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.code_index.extractors import language_for_path
from app.services.symbol_awareness.safety import bounded_context


@dataclass
class RawReference:
    name: str
    reference_type: str
    line_start: int
    line_end: int
    column_start: int | None
    column_end: int | None
    context_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def extract_references(relative_path: str, text: str) -> list[RawReference]:
    language = language_for_path(relative_path)
    if language == "python":
        return _python(text)
    if language in {"javascript", "typescript", "jsx", "tsx"}:
        return _javascript(text)
    return _generic(text)


def _python(text: str) -> list[RawReference]:
    tree = ast.parse(text)
    lines = text.splitlines()
    references: list[RawReference] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _python_name(node.func)
            if name:
                references.append(_raw(name, "call", node, lines))
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _python_name(base)
                if name:
                    references.append(_raw(name, "type_usage", base, lines))
            for decorator in node.decorator_list:
                name = _python_name(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                if name:
                    references.append(_raw(name, "usage", decorator, lines))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                name = _python_name(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                if name:
                    references.append(_raw(name, "usage", decorator, lines))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                references.append(
                    _raw(
                        alias.asname or alias.name.split(".")[-1],
                        "import_usage",
                        node,
                        lines,
                        {"module": alias.name},
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                references.append(
                    _raw(
                        alias.asname or alias.name,
                        "import_usage",
                        node,
                        lines,
                        {"module": node.module or ""},
                    )
                )
    return _dedupe(references)


def _python_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _raw(
    name: str,
    kind: str,
    node: ast.AST,
    lines: list[str],
    metadata: dict[str, Any] | None = None,
) -> RawReference:
    line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", line)
    column = getattr(node, "col_offset", None)
    end_column = getattr(node, "end_col_offset", None)
    context = lines[line - 1] if 0 < line <= len(lines) else ""
    return RawReference(
        name,
        kind,
        line,
        end_line,
        column,
        end_column,
        bounded_context(context),
        metadata or {},
    )


def _javascript(text: str) -> list[RawReference]:
    references: list[RawReference] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        named_import = re.match(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", stripped)
        default_import = re.match(
            r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]", stripped
        )
        if named_import:
            for raw_name in named_import.group(1).split(","):
                parts = raw_name.strip().split(" as ")
                name = parts[-1].strip()
                if name:
                    references.append(
                        _js_raw(
                            name,
                            "import_usage",
                            line_number,
                            line,
                            {"module": named_import.group(2), "imported_name": parts[0].strip()},
                        )
                    )
        elif default_import:
            references.append(
                _js_raw(
                    default_import.group(1),
                    "import_usage",
                    line_number,
                    line,
                    {"module": default_import.group(2), "imported_name": "default"},
                )
            )
        for match in re.finditer(r"<([A-Z][A-Za-z0-9_$]*)\b", line):
            references.append(_js_raw(match.group(1), "component_usage", line_number, line))
        if not re.search(r"\b(?:function|class)\s+[A-Za-z_$][\w$]*", line):
            for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", line):
                name = match.group(1)
                if name not in {"if", "for", "while", "switch", "catch", "function"}:
                    references.append(_js_raw(name, "call", line_number, line))
        export = re.match(
            r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface)\s+([A-Za-z_$][\w$]*)",
            stripped,
        )
        if export:
            references.append(_js_raw(export.group(1), "export_usage", line_number, line))
    return _dedupe(references)


def _js_raw(
    name: str,
    kind: str,
    line_number: int,
    line: str,
    metadata: dict[str, Any] | None = None,
) -> RawReference:
    column = line.find(name)
    return RawReference(
        name,
        kind,
        line_number,
        line_number,
        column if column >= 0 else None,
        column + len(name) if column >= 0 else None,
        bounded_context(line),
        metadata or {},
    )


def _generic(text: str) -> list[RawReference]:
    references: list[RawReference] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", line):
            references.append(_js_raw(match.group(1), "call", line_number, line))
    return _dedupe(references)


def _dedupe(references: list[RawReference]) -> list[RawReference]:
    seen: set[tuple] = set()
    result: list[RawReference] = []
    for item in references:
        key = (item.name, item.reference_type, item.line_start, item.column_start)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
