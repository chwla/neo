from __future__ import annotations

VALID_RUN_TYPES = {"agent", "coding_agent"}

AGENT_SAFE_REPAIR_STATUSES = {"waiting_approval", "failed", "interrupted", "needs_review"}
CODING_SAFE_REPAIR_STATUSES = {
    "waiting_patch_approval",
    "waiting_test_approval",
    "waiting_checkpoint_approval",
    "failed",
    "interrupted",
    "needs_review",
}

CODING_RUNNING_STATUSES = {
    "planning",
    "selecting_context",
    "proposing_patch",
    "applying_patch",
    "running_tests",
    "analyzing_test_result",
    "proposing_followup_patch",
    "creating_checkpoint",
}

CODING_WAITING_STATUSES = {
    "waiting_patch_approval",
    "waiting_test_approval",
    "waiting_checkpoint_approval",
}

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def require_confirm(confirm: bool, action: str) -> None:
    if not confirm:
        raise ValueError(f"Set confirm=true to {action}.")


def validate_run_type(run_type: str) -> None:
    if run_type not in VALID_RUN_TYPES:
        raise ValueError("run_type must be agent or coding_agent.")


def validate_repair_target(run_type: str, target_status: str) -> None:
    validate_run_type(run_type)
    allowed = AGENT_SAFE_REPAIR_STATUSES if run_type == "agent" else CODING_SAFE_REPAIR_STATUSES
    if target_status not in allowed:
        raise ValueError("Target status is not a safe recovery repair state.")
