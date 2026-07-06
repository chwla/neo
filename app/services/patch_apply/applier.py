from __future__ import annotations

from app.services.patch_apply.parser import ParsedPatch


def apply_exact_patch(original: str, patch: ParsedPatch) -> str:
    source = original.splitlines()
    result: list[str] = []
    source_index = 0
    for hunk in patch.hunks:
        target_index = hunk.old_start - 1
        if target_index < source_index or target_index > len(source):
            raise ValueError("Patch hunk position is invalid for the current file.")
        result.extend(source[source_index:target_index])
        source_index = target_index
        consumed = produced = 0
        for line in hunk.lines:
            marker, text = line[0], line[1:]
            if marker in {" ", "-"}:
                if source_index >= len(source) or source[source_index] != text:
                    raise ValueError(
                        "Patch could not be applied cleanly because hunk context did not "
                        "match current file content."
                    )
                source_index += 1
                consumed += 1
            if marker in {" ", "+"}:
                result.append(text)
                produced += 1
        if consumed != hunk.old_count or produced != hunk.new_count:
            raise ValueError("Patch hunk line counts do not match the unified diff header.")
    result.extend(source[source_index:])
    updated = "\n".join(result)
    if original.endswith(("\n", "\r")):
        updated += "\n"
    return updated
