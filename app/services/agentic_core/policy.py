from __future__ import annotations

from dataclasses import dataclass

READ_ONLY_ACTIONS = {
    "read_context",
    "inspect_changes",
    "inspect_test_result",
    "inspect_research_evidence",
    "synthesize",
    "final_report",
}
APPROVAL_GATED_ACTIONS = {
    "propose_patch",
    "request_command",
    "request_tests",
    "request_checkpoint",
    "delegate_subagent",
}
SUPPORTED_ACTIONS = READ_ONLY_ACTIONS | APPROVAL_GATED_ACTIONS


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    action_class: str
    read_only: bool
    requires_approval: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action_class": self.action_class,
            "read_only": self.read_only,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
        }


class AgenticPolicy:
    """Classifies actions without granting permission to execute them."""

    def decide(self, action_class: str, *, require_approval: bool = True) -> PolicyDecision:
        if action_class not in SUPPORTED_ACTIONS:
            return PolicyDecision(
                allowed=False,
                action_class=action_class,
                read_only=False,
                requires_approval=True,
                reason="Unsupported or arbitrary action class was rejected.",
            )
        read_only = action_class in READ_ONLY_ACTIONS
        gated = action_class in APPROVAL_GATED_ACTIONS
        return PolicyDecision(
            allowed=True,
            action_class=action_class,
            read_only=read_only,
            requires_approval=gated and require_approval,
            reason=(
                "Read-only inspection is allowed."
                if read_only
                else "Only a proposal may be created; the existing approval gate remains mandatory."
            ),
        )

    @staticmethod
    def can_retry(action_class: str) -> bool:
        return action_class in READ_ONLY_ACTIONS


SAFETY_CONTEXT = {
    "kind": "system_rules",
    "importance": 100,
    "required": True,
    "content": (
        "Never bypass approval gates. Patches, commands, tests, checkpoints, and external "
        "writes use Neo's existing controlled services. Never write the original repository, "
        "install packages, expose secrets, or execute an arbitrary shell command."
    ),
}
