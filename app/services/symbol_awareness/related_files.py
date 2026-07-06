from __future__ import annotations

from pathlib import PurePosixPath


def compute_related_files(
    repo_files: list[dict], dependencies: list[dict], references: list[dict]
) -> list[dict]:
    by_id = {item["id"]: item for item in repo_files}
    scores: dict[tuple[str, str], dict] = {}

    def add(source: str, target: str, kind: str, amount: float, reason: str) -> None:
        if (
            not source
            or not target
            or source == target
            or source not in by_id
            or target not in by_id
        ):
            return
        key = (source, target)
        item = scores.setdefault(
            key,
            {
                "source_repo_file_id": source,
                "target_repo_file_id": target,
                "relationship_type": kind,
                "score": 0.0,
                "reasons": [],
            },
        )
        item["score"] = min(1.0, item["score"] + amount)
        if reason not in item["reasons"]:
            item["reasons"].append(reason)

    for dependency in dependencies:
        target = dependency.get("target_repo_file_id")
        source = dependency["source_repo_file_id"]
        if target:
            add(source, target, "imports", 0.5, f"Imports {dependency['target_relative_path']}")
            add(
                target,
                source,
                "reverse_import",
                0.4,
                f"Imported by {dependency['source_relative_path']}",
            )
    for reference in references:
        target = reference.get("target_repo_file_id")
        if target:
            add(
                reference["source_repo_file_id"],
                target,
                "symbol_reference",
                0.3,
                f"References {reference['referenced_name']}",
            )
    for source in repo_files:
        source_dir = PurePosixPath(source["relative_path"]).parent
        for target in repo_files:
            if source["id"] == target["id"]:
                continue
            if source_dir == PurePosixPath(target["relative_path"]).parent:
                add(source["id"], target["id"], "same_directory", 0.1, "Same directory")
    return list(scores.values())
