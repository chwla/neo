from __future__ import annotations

from pathlib import PurePosixPath

from app.services.code_index.extractors import ExtractedDependency

JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs")


def resolve_dependencies(
    source_path: str,
    dependencies: list[ExtractedDependency],
    repo_files: list[dict],
) -> list[dict]:
    by_path = {item["relative_path"]: item for item in repo_files}
    return [_resolve(source_path, dependency, by_path) for dependency in dependencies]


def _resolve(source_path: str, dependency: ExtractedDependency, by_path: dict) -> dict:
    module = dependency.module or ""
    candidates: list[str] = []
    if dependency.dependency_type in {"import", "require", "dynamic_import"} and module.startswith(
        "."
    ):
        base = PurePosixPath(source_path).parent
        raw = _normalize(base.joinpath(module))
        candidates.extend([raw, *(raw + ext for ext in JS_EXTENSIONS)])
        candidates.extend(f"{raw}/index{ext}" for ext in JS_EXTENSIONS)
    elif dependency.dependency_type in {"import", "from_import"}:
        module_path = module.lstrip(".").replace(".", "/")
        if module_path:
            candidates.extend([f"{module_path}.py", f"{module_path}/__init__.py"])
    elif dependency.dependency_type == "include":
        candidates.append(module)

    target = next((by_path[path] for path in candidates if path in by_path), None)
    internal = target is not None
    return {
        "target_repo_file_id": target["id"] if target else None,
        "target_relative_path": target["relative_path"] if target else None,
        "import_text": dependency.import_text,
        "dependency_type": "internal" if internal else "external",
        "resolved": internal,
        "metadata": {
            "syntax_type": dependency.dependency_type,
            "module": module,
            "line_start": dependency.line_start,
        },
    }


def _normalize(path: PurePosixPath) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)
