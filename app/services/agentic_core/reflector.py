from __future__ import annotations

from typing import Any


class AgenticReflector:
    def reflect(
        self,
        plan_step: dict[str, Any],
        result: dict[str, Any],
        verification: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        blocker = result.get("blocker")
        passed = bool(verification.get("passed"))
        return {
            "what_changed": result.get("summary") or "No verified change was recorded.",
            "what_was_learned": list(result.get("evidence") or []),
            "what_failed": result.get("error"),
            "plan_should_change": bool(result.get("revise_plan")) or not passed,
            "more_context_needed": bool(result.get("more_context_needed")),
            "user_input_required": bool(blocker or result.get("requires_approval")),
            "completion_criteria_satisfied": (
                passed
                and not blocker
                and state.get("current_step_index", 0) + 1 >= len(state.get("plan", []))
            ),
            "blocker": blocker,
            "recommended_next_step": result.get("next_action")
            or (
                "Wait for explicit user approval." if blocker else "Continue to the next plan step."
            ),
            "grounded_in_step": plan_step.get("step_index"),
        }
