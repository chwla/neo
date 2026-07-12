from __future__ import annotations

from typing import Any


class AgenticPlanner:
    """Produces bounded, auditable plans; execution remains delegated to existing services."""

    def create_plan(
        self, objective: str, run_type: str, context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if run_type == "coding":
            raw = [
                (
                    "INSPECT",
                    "Inspect repository context and constraints",
                    "read_context",
                    ["rules", "memory", "repo", "lsp"],
                    ["rules", "context-memory", "lsp"],
                    (
                        "Context sources are present or a recorded degraded fallback "
                        "explains each absence."
                    ),
                ),
                (
                    "ACT",
                    "Prepare a bounded patch proposal",
                    "propose_patch",
                    ["repo", "selected_files"],
                    ["coding-agent", "patch-proposal"],
                    "A proposal exists and no patch was applied without explicit approval.",
                ),
                (
                    "VERIFY",
                    "Inspect the approved workspace change",
                    "inspect_changes",
                    ["patch_application"],
                    ["patch-apply"],
                    "Changed managed files match the approved patch application record.",
                ),
                (
                    "VERIFY",
                    "Validate with approved tests",
                    "request_tests",
                    ["test_commands", "patch_application"],
                    ["test-runner", "command-sandbox"],
                    (
                        "A persisted approved test result is inspected, including exit "
                        "status and output."
                    ),
                ),
                (
                    "REFLECT",
                    "Produce a grounded coding report",
                    "final_report",
                    ["verified_steps", "failures", "checkpoints"],
                    ["agentic-core"],
                    "The report names only persisted files, tests, checkpoints, and blockers.",
                ),
            ]
            criteria = [
                "Repository context and safety constraints were inspected.",
                "No patch, command, test, or checkpoint bypassed its approval gate.",
                "Every executed action has persisted verification evidence.",
                "The final report is grounded in actual step results.",
            ]
        elif run_type == "research":
            raw = [
                (
                    "INSPECT",
                    "Inspect objective, rules, and existing memory",
                    "read_context",
                    ["rules", "memory", "project"],
                    ["rules", "context-memory"],
                    "Stored context is relevant and unavailable sources are identified.",
                ),
                (
                    "ACT",
                    "Gather available research evidence",
                    "inspect_research_evidence",
                    ["research_records"],
                    ["research", "web-search"],
                    "Evidence records include sources or an explicit availability blocker.",
                ),
                (
                    "VERIFY",
                    "Cross-check evidence coverage",
                    "synthesize",
                    ["research_evidence"],
                    ["agentic-core"],
                    "Claims are limited to recorded evidence and uncertainty is explicit.",
                ),
                (
                    "REFLECT",
                    "Produce a grounded research report",
                    "final_report",
                    ["verified_steps", "citations"],
                    ["agentic-core"],
                    "The report distinguishes findings, evidence, and open questions.",
                ),
            ]
            criteria = [
                "The objective is addressed with traceable evidence.",
                "Unsupported claims and missing sources are reported as blockers.",
                "The final report cites only evidence recorded by Neo.",
            ]
        else:
            raw = [
                (
                    "INSPECT",
                    "Inspect task, project, rules, and memory",
                    "read_context",
                    ["task", "project", "rules", "memory"],
                    ["tasks", "rules", "context-memory"],
                    "Required context is present or its absence is recorded.",
                ),
                (
                    "ACT",
                    "Perform the bounded task step",
                    "synthesize",
                    ["known_context"],
                    ["agent-runner"],
                    "The result is derived from recorded context without unsafe mutation.",
                ),
                (
                    "VERIFY",
                    "Verify the task outcome",
                    "inspect_changes",
                    ["actions", "artifacts"],
                    ["agent-runner"],
                    "Expected and actual outcomes are compared with evidence.",
                ),
                (
                    "REFLECT",
                    "Produce a grounded task report",
                    "final_report",
                    ["verified_steps", "failures"],
                    ["agentic-core"],
                    "The report states completed work, evidence, and remaining blockers.",
                ),
            ]
            criteria = [
                "The requested task is decomposed and processed step by step.",
                "Meaningful actions have verification and reflection records.",
                "The final report does not claim unverified success.",
            ]

        plan = []
        for index, item in enumerate(raw):
            phase, title, action, required, tools, verification = item
            plan.append(
                {
                    "step_index": index,
                    "title": title,
                    "description": f"Advance the objective: {objective[:400]}",
                    "phase": phase,
                    "action_class": action,
                    "required_context": required,
                    "likely_tools": tools,
                    "verification_method": verification,
                    "risk_notes": self._risks(action),
                }
            )
        return plan, criteria

    @staticmethod
    def _risks(action: str) -> list[str]:
        if action in {"propose_patch", "request_command", "request_tests", "request_checkpoint"}:
            return ["Existing approval is mandatory.", "No unsafe action executes during planning."]
        return ["Do not infer success when context or evidence is missing."]
