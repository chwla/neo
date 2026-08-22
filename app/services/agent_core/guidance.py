from __future__ import annotations

import re

_AGENT_RUN_REQUEST = re.compile(
    r"(?:\b(?:run|start|use|launch)\b.{0,40}\bagent\b|\bagent\s+runner\b|"
    r"\bstart\s+working\s+on\s+(?:the|this)?\s*task\b)",
    re.IGNORECASE,
)
_PATCH_APPLY_REQUEST = re.compile(
    r"\b(?:apply|install|use)\b.{0,30}\b(?:patch|diff|proposal|it)\b",
    re.IGNORECASE,
)
_INFORMATIONAL_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:explain|describe|document|write\s+(?:documentation|docs)|"
    r"compare|define|summari[sz]e|teach|tell\s+me\s+about|"
    r"what\b|why\b|how\b|when\b|where\b|who\b|which\b)",
    re.IGNORECASE,
)


def agent_run_guidance(prompt: str) -> str | None:
    """Return navigation guidance without starting or mutating an agent run."""
    cleaned = prompt.strip()
    if _INFORMATIONAL_REQUEST.match(cleaned):
        return None
    if _PATCH_APPLY_REQUEST.search(cleaned):
        return (
            "Open Files or the linked Task, open the patch proposal artifact, and click "
            "Validate Patch. Apply Patch becomes available only after validation passes and "
            "you confirm the workspace-copy change. Chat and Agent Runner never apply patches "
            "automatically."
        )
    if not _AGENT_RUN_REQUEST.search(cleaned):
        return None
    return (
        "Switch the composer to Agent mode and give Neo the objective. It inspects the "
        "code, calls tools, and verifies its own work, streaming each step as it goes. "
        "Pick a permission mode first: Plan proposes without changing anything, Normal "
        "asks before each change, and Auto edits on its own. Open a folder on this "
        "machine and Neo works in it directly, the way a coding CLI does -- every run "
        "is journalled, so you can undo it. You can steer or stop the run at any "
        "point. Chat does not start agent runs automatically."
    )
