# ruff: noqa
def plan(goal, scope):
    return {
        "goal": goal,
        "scope": scope,
        "milestones": ["Plan", "Build", "Validate", "Manual review"],
        "tasks": ["Define scope", "Implement deliverables", "Run validation"],
        "risks": ["Missing evidence blocks readiness"],
        "blockers": [],
        "required_research": [],
        "required_evals": ["evaluation harness"],
        "validation_gates": ["tests", "integrity", "safety"],
        "manual_review": ["Review rendered UI"],
        "completion_criteria": ["All required readiness checks pass"],
    }
