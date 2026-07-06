from __future__ import annotations

import re


_AGENT_RUN_REQUEST = re.compile(
    r"(?:\b(?:run|start|use|launch)\b.{0,40}\bagent\b|\bagent\s+runner\b|"
    r"\bstart\s+working\s+on\s+(?:the\s+)?task\b)",
    re.IGNORECASE,
)
_PATCH_APPLY_REQUEST = re.compile(
    r"\b(?:apply|install|use)\b.{0,30}\b(?:patch|diff|proposal|it)\b",
    re.IGNORECASE,
)


def agent_run_guidance(prompt: str) -> str | None:
    """Return navigation guidance without starting or mutating an agent run."""
    if _PATCH_APPLY_REQUEST.search(prompt.strip()):
        return (
            "Open Files or the linked Task, open the patch proposal artifact, and click "
            "Validate Patch. Apply Patch becomes available only after validation passes and "
            "you confirm the workspace-copy change. Chat and Agent Runner never apply patches "
            "automatically."
        )
    if not _AGENT_RUN_REQUEST.search(prompt.strip()):
        return None
    return (
        "Open Tasks, select the task, optionally refine the run objective, and click "
        "Run Agent. Neo will create a bounded, audited assisted run linked to that task. "
        "You can inspect each step, cancel an active run, and explicitly save a completed "
        "output to a Note. Chat does not start agent runs automatically."
    )
