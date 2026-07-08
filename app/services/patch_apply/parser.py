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
class ParsedFilePatch:
    filename: str
    change_type: str
    patch_text: str
    hunks: list[Hunk]


@dataclass(frozen=True)
class ParsedPatch:
    files: list[ParsedFilePatch]
    patch_text: str

    @property
    def filename(self) -> str:
        return self.files[0].filename

    @property
    def hunks(self) -> list[Hunk]:
        return self.files[0].hunks


def extract_diff(markdown: str) -> str:
    matches = re.findall(r"```diff\s*\n(.*?)```", markdown, flags=re.DOTALL | re.IGNORECASE)
    if len(matches) != 1:
        raise ValueError("Patch artifact must contain exactly one fenced unified diff.")
    return matches[0].strip("\n")


def _header_value(lines: list[str], prefix: str) -> str:
    matches = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"Each file patch must contain exactly one {prefix.strip()} header.")
    return matches[0].strip().split("\t", 1)[0]


def _parse_hunks(lines: list[str]) -> list[Hunk]:
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
        raise ValueError("Each file patch must contain at least one unified diff hunk.")
    return hunks


def _parse_file_section(lines: list[str]) -> ParsedFilePatch:
    header_parts = lines[0].split()
    if len(header_parts) != 4:
        raise ValueError("Invalid unified diff file header.")
    git_old = normalize_target_path(header_parts[2])
    git_new = normalize_target_path(header_parts[3])
    if git_old != git_new:
        raise ValueError("File rename patches are not supported.")

    old_raw = _header_value(lines, "--- ")
    new_raw = _header_value(lines, "+++ ")
    new_file_markers = [line for line in lines if line.startswith("new file mode ")]
    if old_raw == "/dev/null":
        if new_raw == "/dev/null" or len(new_file_markers) != 1:
            raise ValueError("New-file patches require new file mode 100644 and /dev/null.")
        if new_file_markers[0] != "new file mode 100644":
            raise ValueError("Only regular 100644 text files can be created.")
        if normalize_target_path(new_raw) != git_new:
            raise ValueError("Patch new-file header does not match its target.")
        change_type = "create"
    else:
        if new_raw == "/dev/null":
            raise ValueError("File deletion patches are not supported.")
        if new_file_markers:
            raise ValueError("New-file mode is invalid for an existing file patch.")
        old_name = normalize_target_path(old_raw)
        new_name = normalize_target_path(new_raw)
        if old_name != git_old or new_name != git_new or old_name != new_name:
            raise ValueError("Patch file headers do not match their target.")
        change_type = "modify"
    return ParsedFilePatch(
        filename=git_new,
        change_type=change_type,
        patch_text="\n".join(lines),
        hunks=_parse_hunks(lines),
    )


def parse_unified_diff(markdown: str) -> ParsedPatch:
    patch_text = extract_diff(markdown)
    validate_patch_text_safety(patch_text)
    lines = patch_text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        raise ValueError("Patch does not contain a diff --git file header.")
    sections = [
        lines[start : starts[index + 1] if index + 1 < len(starts) else len(lines)]
        for index, start in enumerate(starts)
    ]
    files = [_parse_file_section(section) for section in sections]
    paths = [item.filename for item in files]
    if len(paths) != len(set(paths)):
        raise ValueError("Patch contains duplicate target paths.")
    return ParsedPatch(files=files, patch_text=patch_text)
