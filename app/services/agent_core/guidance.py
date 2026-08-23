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
    """Say how to get a run, without starting one.

    A plain reply cannot escalate itself into an agent turn: asking for work to
    be done is not consent for files to be edited, and the model deciding that
    for itself is exactly the routing this design rejected. So the answer names
    the one gesture -- the toggle -- that does ask.
    """
    cleaned = prompt.strip()
    if _INFORMATIONAL_REQUEST.match(cleaned):
        return None
    if _PATCH_APPLY_REQUEST.search(cleaned):
        return (
            "Open Files or the linked Task, open the patch proposal artifact, and click "
            "Validate Patch. Apply Patch becomes available only after validation passes and "
            "you confirm the workspace-copy change. Neither a reply nor an agent turn "
            "applies patches automatically."
        )
    if not _AGENT_RUN_REQUEST.search(cleaned):
        return None
    return (
        "Turn on Agent in the composer and send that again, and it runs here in this "
        "chat. Neo inspects the code, calls tools, and verifies its own work, showing "
        "each step as a turn in the conversation. The permission chip decides how far "
        "it goes on its own: Plan proposes without changing anything, Normal asks "
        "before each change, and Auto edits on its own. Open a folder and Neo works in "
        "it directly, the way a coding CLI does -- every run is journalled, so you can "
        "undo it. You can steer or stop it at any point. A message never starts a run "
        "on its own; the toggle is what asks for one."
    )
