from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExtractedSymbol:
    name: str
    symbol_type: str
    line_start: int | None = None
    line_end: int | None = None
    qualified_name: str | None = None
    signature: str | None = None
    parent_name: str | None = None
    doc_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDependency:
    import_text: str
    dependency_type: str
    module: str | None = None
    line_start: int | None = None


@dataclass
class ExtractionResult:
    language: str
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    dependencies: list[ExtractedDependency] = field(default_factory=list)


LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".sql": "sql",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


def language_for_path(relative_path: str) -> str:
    return LANGUAGES.get(Path(relative_path).suffix.lower(), "unknown")


def extract(relative_path: str, text: str) -> ExtractionResult:
    language = language_for_path(relative_path)
    if language == "python":
        return _python(text)
    if language in {"javascript", "typescript", "jsx", "tsx"}:
        return _javascript(text, language)
    if language in {"c", "cpp"}:
        return _c_family(text, language)
    if language == "java":
        return _java(text)
    if language == "go":
        return _go(text)
    if language == "rust":
        return _rust(text)
    if language == "sql":
        return _sql(text)
    if language == "markdown":
        return _markdown(text)
    return ExtractionResult(language=language)


def _python(text: str) -> ExtractionResult:
    tree = ast.parse(text)
    result = ExtractionResult(language="python")
    parents: list[str] = []

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        result.symbols.append(
                            ExtractedSymbol(alias.name, "import", node.lineno, node.end_lineno)
                        )
                        result.dependencies.append(
                            ExtractedDependency(
                                ast.unparse(node), "import", alias.name, node.lineno
                            )
                        )
                else:
                    module = "." * node.level + (node.module or "")
                    result.symbols.append(
                        ExtractedSymbol(
                            module or ast.unparse(node), "import", node.lineno, node.end_lineno
                        )
                    )
                    result.dependencies.append(
                        ExtractedDependency(ast.unparse(node), "from_import", module, node.lineno)
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents[-1] if parents else None
                kind = (
                    "method"
                    if parent
                    else (
                        "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                    )
                )
                qualified = ".".join([*parents, node.name]) if parents else node.name
                result.symbols.append(
                    ExtractedSymbol(
                        node.name,
                        kind,
                        node.lineno,
                        node.end_lineno,
                        qualified,
                        _python_signature(node),
                        parent,
                        ast.get_docstring(node),
                    )
                )
                for decorator in node.decorator_list:
                    route = _python_route(decorator)
                    if route:
                        method, path = route
                        result.symbols.append(
                            ExtractedSymbol(
                                f"{method} {path}",
                                "api_route",
                                decorator.lineno,
                                node.end_lineno,
                                parent_name=node.name,
                                metadata={"method": method, "path": path, "handler": node.name},
                            )
                        )
                parents.append(node.name)
                visit(node.body)
                parents.pop()
            elif isinstance(node, ast.ClassDef):
                qualified = ".".join([*parents, node.name]) if parents else node.name
                result.symbols.append(
                    ExtractedSymbol(
                        node.name,
                        "class",
                        node.lineno,
                        node.end_lineno,
                        qualified,
                        doc_text=ast.get_docstring(node),
                    )
                )
                parents.append(node.name)
                visit(node.body)
                parents.pop()
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not parents:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        result.symbols.append(
                            ExtractedSymbol(target.id, "constant", node.lineno, node.end_lineno)
                        )

    visit(tree.body)
    return result


def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({ast.unparse(node.args)})"


def _python_route(decorator: ast.expr) -> tuple[str, str] | None:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    method = decorator.func.attr.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
        return None
    if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
        return None
    return method, str(decorator.args[0].value)


def _javascript(text: str, language: str) -> ExtractionResult:
    result = ExtractionResult(language=language)
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        import_match = re.match(r"import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", stripped)
        side_effect = re.match(r"import\s+['\"]([^'\"]+)['\"]", stripped)
        require_match = re.search(r"require\(['\"]([^'\"]+)['\"]\)", stripped)
        dynamic = re.search(r"import\(['\"]([^'\"]+)['\"]\)", stripped)
        for match, kind in (
            (import_match, "import"),
            (side_effect, "import"),
            (require_match, "require"),
            (dynamic, "dynamic_import"),
        ):
            if match:
                module = match.group(1)
                result.dependencies.append(ExtractedDependency(stripped, kind, module, index))
                result.symbols.append(ExtractedSymbol(module, "import", index, index))
        function = re.search(
            r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
            line,
        )
        arrow = re.search(
            r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>", line
        )
        class_match = re.search(r"(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)", line)
        type_match = re.search(r"(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)", line)
        candidate = function or arrow
        if candidate:
            name = candidate.group(1)
            symbol_type = (
                "component" if name[:1].isupper() and language in {"jsx", "tsx"} else "function"
            )
            if name.startswith("use") and name[3:4].isupper():
                symbol_type = "hook"
            result.symbols.append(ExtractedSymbol(name, symbol_type, index, index, name, stripped))
            if stripped.startswith("export "):
                result.symbols.append(ExtractedSymbol(name, "export", index, index))
        if class_match:
            result.symbols.append(ExtractedSymbol(class_match.group(1), "class", index, index))
            if stripped.startswith("export "):
                result.symbols.append(ExtractedSymbol(class_match.group(1), "export", index, index))
        if type_match:
            kind = "interface" if "interface" in stripped else "type"
            result.symbols.append(ExtractedSymbol(type_match.group(1), kind, index, index))
            if stripped.startswith("export "):
                result.symbols.append(ExtractedSymbol(type_match.group(1), "export", index, index))
        export = re.match(r"export\s+(?:default\s+)?(?:\{\s*)?([A-Za-z_$][\w$]*)", stripped)
        if export and not function and not class_match and not type_match:
            result.symbols.append(ExtractedSymbol(export.group(1), "export", index, index))
    return result


def _regex_result(text: str, language: str, patterns: list[tuple[str, str]]) -> ExtractionResult:
    result = ExtractionResult(language=language)
    for index, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            match = re.search(pattern, line)
            if match:
                result.symbols.append(
                    ExtractedSymbol(match.group(1), kind, index, index, signature=line.strip())
                )
    return result


def _c_family(text: str, language: str) -> ExtractionResult:
    result = _regex_result(
        text,
        language,
        [
            (r"\b(?:class|struct)\s+(\w+)", "class"),
            (r"^[\w:*&<>\s]+\s+(\w+)\s*\([^;]*\)\s*\{", "function"),
        ],
    )
    for index, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"\s*#include\s*[<\"]([^>\"]+)[>\"]", line)
        if match:
            result.dependencies.append(
                ExtractedDependency(line.strip(), "include", match.group(1), index)
            )
    return result


def _java(text: str) -> ExtractionResult:
    result = _regex_result(
        text,
        "java",
        [
            (r"\b(?:class|interface)\s+(\w+)", "class"),
            (r"\b(?:public|private|protected)\s+[\w<>\[\]]+\s+(\w+)\s*\(", "method"),
        ],
    )
    for index, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"\s*import\s+([\w.]+)", line)
        if match:
            result.dependencies.append(
                ExtractedDependency(line.strip(), "import", match.group(1), index)
            )
    return result


def _go(text: str) -> ExtractionResult:
    return _regex_result(
        text,
        "go",
        [
            (r"\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", "function"),
            (r"\btype\s+(\w+)\s+struct\b", "struct"),
        ],
    )


def _rust(text: str) -> ExtractionResult:
    result = _regex_result(
        text,
        "rust",
        [
            (r"\bfn\s+(\w+)\s*\(", "function"),
            (r"\bstruct\s+(\w+)", "struct"),
            (r"\benum\s+(\w+)", "enum"),
        ],
    )
    for index, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"\s*use\s+([^;]+)", line)
        if match:
            result.dependencies.append(
                ExtractedDependency(line.strip(), "use", match.group(1), index)
            )
    return result


def _sql(text: str) -> ExtractionResult:
    return _regex_result(
        text,
        "sql",
        [
            (r"(?i)CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"[]?([\w.]+)", "table"),
            (
                r"(?i)CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"[]?([\w.]+)",
                "index",
            ),
        ],
    )


def _markdown(text: str) -> ExtractionResult:
    result = ExtractionResult(language="markdown")
    for index, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            result.symbols.append(
                ExtractedSymbol(
                    match.group(2), "heading", index, index, metadata={"level": len(match.group(1))}
                )
            )
    return result
