from __future__ import annotations

from typing import Any


class AgenticVerifier:
    def verify(self, plan_step: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        evidence = list(result.get("evidence") or [])
        requires_approval = bool(result.get("requires_approval"))
        status = result.get("status", "failed")
        passed = status == "completed" and bool(evidence)
        if requires_approval:
            passed = status in {"completed", "blocked"} and bool(evidence)
        return {
            "expected_outcome": plan_step.get(
                "verification_method", "Recorded outcome matches plan."
            ),
            "actual_outcome": result.get("summary")
            or result.get("error")
            or "No outcome recorded.",
            "passed": passed,
            "evidence": evidence,
            "next_action": result.get("next_action")
            or ("Reflect and continue." if passed else "Recover safely or ask the user."),
        }
