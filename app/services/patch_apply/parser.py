from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.patch_apply.safety import normalize_target_path, validate_patch_text_safety

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass(frozen=True)
class ParsedPatch:
    filename: str
    patch_text: str
    hunks: list[Hunk]


def extract_diff(markdown: str) -> str:
    matches = re.findall(r"```diff\s*\n(.*?)```", markdown, flags=re.DOTALL | re.IGNORECASE)
    if len(matches) != 1:
        raise ValueError("Patch artifact must contain exactly one fenced unified diff.")
    return matches[0].strip("\n")


def parse_unified_diff(markdown: str) -> ParsedPatch:
    patch_text = extract_diff(markdown)
    validate_patch_text_safety(patch_text)
    lines = patch_text.splitlines()
    diff_headers = [line for line in lines if line.startswith("diff --git ")]
    if len(diff_headers) != 1:
        raise ValueError("Controlled Patch Apply v1 supports exactly one target file.")
    header_parts = diff_headers[0].split()
    if len(header_parts) != 4:
        raise ValueError("Invalid unified diff file header.")
    old_header_name = normalize_target_path(header_parts[2])
    new_header_name = normalize_target_path(header_parts[3])
    if old_header_name != new_header_name:
        raise ValueError("File rename patches are not supported.")

    old_headers = [line[4:] for line in lines if line.startswith("--- ")]
    new_headers = [line[4:] for line in lines if line.startswith("+++ ")]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise ValueError("Patch must contain exactly one --- and +++ file header.")
    if normalize_target_path(old_headers[0]) != old_header_name:
        raise ValueError("Patch old-file header does not match its target.")
    if normalize_target_path(new_headers[0]) != old_header_name:
        raise ValueError("Patch new-file header does not match its target.")

    hunks: list[Hunk] = []
    index = 0
    while index < len(lines):
        match = HUNK_RE.match(lines[index])
        if not match:
            index += 1
            continue
        old_start, old_count, new_start, new_count = (
            int(match.group(1)),
            int(match.group(2) or 1),
            int(match.group(3)),
            int(match.group(4) or 1),
        )
        index += 1
        hunk_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith("diff --git "):
                raise ValueError("Controlled Patch Apply v1 supports exactly one target file.")
            if line == "\\ No newline at end of file":
                index += 1
                continue
            if not line.startswith((" ", "+", "-")):
                break
            hunk_lines.append(line)
            index += 1
        if not hunk_lines:
            raise ValueError("Unified diff hunk is empty.")
        hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
    if not hunks:
        raise ValueError("Patch does not contain any unified diff hunks.")
    return ParsedPatch(filename=old_header_name, patch_text=patch_text, hunks=hunks)
