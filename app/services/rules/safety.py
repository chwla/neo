from __future__ import annotations

from pathlib import PurePosixPath

HARD_FORBIDDEN = [".env", ".git", "node_modules", "dist", "secrets"]
LIST_FIELDS = {
    "instructions",
    "coding_style",
    "preferred_paths",
    "forbidden_paths",
    "research_preferences",
    "source_preferences",
}
KNOWN_FIELDS = LIST_FIELDS | {
    "test_preferences",
    "checkpoint_template",
    "model_routes",
    "approval_defaults",
    "patch_constraints",
    "metadata",
}


def clean_path(value: str) -> str | None:
    value = str(value).strip().replace("\\", "/").lstrip("./")
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        return None
    return value.rstrip("/")


def path_matches(path: str, patterns: list[str]) -> bool:
    cleaned = clean_path(path)
    if not cleaned:
        return True
    return any(cleaned == pattern or cleaned.startswith(pattern + "/") for pattern in patterns)


def sanitize_rules(rules: dict, warnings: list[str], source: str) -> dict:
    if not isinstance(rules, dict):
        warnings.append(f"{source}: rules must be a JSON object; ignored.")
        return {}
    result = {}
    for field in LIST_FIELDS:
        value = rules.get(field, [])
        if isinstance(value, list):
            result[field] = [str(item).strip() for item in value if str(item).strip()]
    for field in ("preferred_paths", "forbidden_paths"):
        valid = []
        for value in result.get(field, []):
            cleaned = clean_path(value)
            if cleaned:
                valid.append(cleaned)
            else:
                warnings.append(f"{source}: unsafe path rule '{value}' was ignored.")
        result[field] = valid
    tests = []
    for item in (
        rules.get("test_preferences", [])
        if isinstance(rules.get("test_preferences", []), list)
        else []
    ):
        if isinstance(item, dict) and isinstance(item.get("command_hint"), list):
            tests.append(
                {
                    "name": str(item.get("name") or "Preferred test"),
                    "command_hint": [str(part) for part in item["command_hint"]],
                    "reason": str(item.get("reason") or ""),
                }
            )
    result["test_preferences"] = tests
    if isinstance(rules.get("checkpoint_template"), str):
        result["checkpoint_template"] = rules["checkpoint_template"][:2000]
    if isinstance(rules.get("model_routes"), dict):
        result["model_routes"] = {str(k): str(v) for k, v in rules["model_routes"].items() if v}
    if isinstance(rules.get("approval_defaults"), dict):
        result["approval_defaults"] = {
            str(k): bool(v) for k, v in rules["approval_defaults"].items()
        }
    if isinstance(rules.get("patch_constraints"), dict):
        result["patch_constraints"] = dict(rules["patch_constraints"])
    unknown = {key: value for key, value in rules.items() if key not in KNOWN_FIELDS}
    metadata = dict(rules.get("metadata", {})) if isinstance(rules.get("metadata"), dict) else {}
    if unknown:
        metadata["unknown_fields"] = unknown
    if metadata:
        result["metadata"] = metadata
    return result
