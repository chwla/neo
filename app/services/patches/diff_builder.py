from __future__ import annotations

import re


def proposal_prompt(objective: str, files: list[dict]) -> str:
    sections = []
    for item in files:
        sections.append(
            f"<workspace-file name={item['patch_path']!r}>\n"
            f"{item['context_text']}\n</workspace-file>"
        )
    names = "\n".join(f"- {item['patch_path']}" for item in files)
    return f"""Create a conservative, proposal-only patch based exclusively on the supplied
workspace file text. Do not invent unseen files or omitted content. Return the exact Markdown
structure below and include a valid unified diff with diff --git, ---, +++, and @@ lines.

# Patch Proposal

## Objective
{objective}

## Target files
{names}

## Summary
...

## Proposed changes
...

## Unified diff
```diff
...
```

## Risks
...

## Validation needed
...

## Notes
This patch has not been applied.

Never claim the patch was applied, files were edited, commands were run, or tests passed.
If the supplied context is insufficient for a truthful line-level diff, explain why instead of
inventing code.

Workspace files:
{chr(10).join(sections)}"""


def fallback_content(objective: str, files: list[dict], reason: str) -> str:
    names = "\n".join(f"- {item['patch_path']}" for item in files)
    return f"""# Patch Proposal Could Not Be Generated Reliably

## Objective
{objective}

## Reason
{reason}

## Files reviewed
{names}

## Recommended manual changes
Review the objective against the listed workspace files and provide narrower or more complete
file context before generating a line-level diff.

## Missing context
A reliable unified diff was not present in the generated proposal.

## Notes
This patch has not been applied."""


def normalize_single_file_diff(content: str, filename: str) -> str:
    """Add Git-style headers to an otherwise valid one-file unified diff."""
    if "diff --git " in content or not all(marker in content for marker in ("--- ", "+++ ", "@@")):
        return content
    escaped = re.escape(filename)
    normalized = re.sub(rf"(?m)^--- (?:a/)?{escaped}$", f"--- a/{filename}", content)
    normalized = re.sub(rf"(?m)^\+\+\+ (?:b/)?{escaped}$", f"+++ b/{filename}", normalized)
    header = f"diff --git a/{filename} b/{filename}\n"
    if "```diff\n" in normalized:
        return normalized.replace("```diff\n", f"```diff\n{header}", 1)
    return f"{header}{normalized}"
