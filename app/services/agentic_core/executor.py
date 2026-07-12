from __future__ import annotations

from typing import Any

from app.services.agentic_core.policy import AgenticPolicy


class AgenticExecutor:
    """Executes reads and creates proposals; it never applies an unsafe action itself."""

    def __init__(self, policy: AgenticPolicy | None = None) -> None:
        self.policy = policy or AgenticPolicy()

    def execute(
        self,
        plan_step: dict[str, Any],
        run: dict[str, Any],
        state: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        action = payload.get("action") or plan_step.get("action_class", "read_context")
        decision = self.policy.decide(
            action, require_approval=bool(state.get("require_approval_for_actions", True))
        )
        tool_choice = {
            "action_class": action,
            "tools": list(plan_step.get("likely_tools") or []),
            "policy": decision.as_dict(),
        }
        if not decision.allowed:
            return {
                "status": "blocked",
                "summary": decision.reason,
                "error": decision.reason,
                "blocker": decision.reason,
                "evidence": ["Agentic policy rejected the unsupported action before execution."],
                "tool_choices": [tool_choice],
                "next_action": "Edit the plan to use a supported bounded action.",
            }
        handler = getattr(self, f"_{action}", None)
        if handler is None:
            return {
                "status": "blocked",
                "summary": "No bounded executor is registered for this action.",
                "blocker": "No bounded executor is registered for this action.",
                "evidence": [decision.reason],
                "tool_choices": [tool_choice],
                "next_action": "Revise the plan or ask the user.",
            }
        result = handler(run, state, payload.get("input") or {})
        result.setdefault("tool_choices", []).append(tool_choice)
        return result

    @staticmethod
    def _read_context(run: dict, state: dict, inputs: dict) -> dict:
        budget = state.get("context_budget") or {}
        included = budget.get("included_items") or []
        evidence = [
            f"{item.get('kind')}: {str(item.get('content', ''))[:500]}" for item in included
        ]
        if not evidence:
            return {
                "status": "blocked",
                "summary": "No context was available for inspection.",
                "blocker": "Required context is unavailable.",
                "evidence": ["Context budget contained no included items."],
                "more_context_needed": True,
                "next_action": "Provide task/project context or repair the unavailable source.",
            }
        return {
            "status": "completed",
            "summary": f"Inspected {len(included)} budgeted context item(s).",
            "evidence": evidence,
            "next_action": "Proceed using only the inspected context.",
        }

    @staticmethod
    def _propose_patch(run: dict, state: dict, inputs: dict) -> dict:
        existing = AgenticExecutor._coding_detail(run.get("source_run_id"))
        actions = existing.get("actions", []) if existing else []
        pending = next(
            (item for item in reversed(actions) if item.get("action_type") == "apply_patch"), None
        )
        evidence = [
            "Patch execution remains delegated to Coding Agent's existing approval-gated flow."
        ]
        if pending:
            evidence.append(
                f"Persisted patch action {pending['id']} has status {pending['status']}."
            )
        return {
            "status": "blocked",
            "summary": (
                "A patch may be proposed, but it cannot be applied without explicit approval."
            ),
            "blocker": "Patch review and explicit apply approval are required.",
            "requires_approval": True,
            "approval_reference": pending.get("id") if pending else None,
            "evidence": evidence,
            "next_action": (
                "Review the Coding Agent patch proposal and approve or reject it explicitly."
            ),
        }

    @staticmethod
    def _inspect_changes(run: dict, state: dict, inputs: dict) -> dict:
        if run.get("run_type") != "coding":
            actions = [
                item for item in state.get("actions_taken", []) if item.get("status") == "completed"
            ]
            if actions:
                return {
                    "status": "completed",
                    "summary": f"Inspected {len(actions)} persisted task action result(s).",
                    "evidence": [f"{item.get('title')}: {item.get('summary')}" for item in actions],
                    "next_action": "Reflect on completion criteria and finalize.",
                }
            return {
                "status": "blocked",
                "summary": "No completed task action exists to verify.",
                "blocker": "A persisted task result is required before verification.",
                "evidence": ["Verification did not infer an unrecorded task outcome."],
                "next_action": "Complete a bounded task action and retry verification.",
            }
        detail = AgenticExecutor._coding_detail(run.get("source_run_id"))
        application = detail.get("patch_application") if detail else None
        if not application:
            return {
                "status": "blocked",
                "summary": "No persisted patch application is available to inspect.",
                "blocker": "An approved patch application result is required.",
                "evidence": ["No managed-workspace patch application record was found."],
                "next_action": "Complete or reject the pending patch approval.",
            }
        files = [item.get("relative_path") for item in application.get("files", [])]
        return {
            "status": "completed",
            "summary": f"Inspected patch application {application.get('id')}.",
            "evidence": [
                f"Application status: {application.get('status')}.",
                "Managed files: " + (", ".join(path for path in files if path) or "none recorded"),
            ],
            "next_action": "Validate the persisted workspace state with approved tests.",
        }

    @staticmethod
    def _request_tests(run: dict, state: dict, inputs: dict) -> dict:
        detail = AgenticExecutor._coding_detail(run.get("source_run_id"))
        test_run = detail.get("test_run") if detail else None
        if test_run and test_run.get("status") not in {"queued", "running", "approved"}:
            return {
                "status": "completed",
                "summary": f"Inspected persisted test run {test_run.get('id')}.",
                "evidence": [
                    f"Test status: {test_run.get('status')}.",
                    f"Exit code: {test_run.get('exit_code')}.",
                    str(test_run.get("combined_output") or test_run.get("error") or "No output.")[
                        :2000
                    ],
                ],
                "next_action": "Reflect on the test evidence.",
            }
        return {
            "status": "blocked",
            "summary": "Tests can run only through the existing saved-command approval gate.",
            "blocker": "Explicit test-command approval or a persisted test result is required.",
            "requires_approval": True,
            "evidence": ["Agentic Core did not execute a test command."],
            "next_action": "Select and approve a saved test command in Coding Agent.",
        }

    @staticmethod
    def _request_command(run: dict, state: dict, inputs: dict) -> dict:
        workspace_id = inputs.get("workspace_id") or state.get("repo_id")
        command = inputs.get("command")
        if not workspace_id or not isinstance(command, list) or not command:
            return {
                "status": "blocked",
                "summary": (
                    "A managed workspace and argv array are required for a command proposal."
                ),
                "blocker": "Command proposal input is incomplete.",
                "evidence": ["No command was executed."],
                "next_action": "Provide a managed workspace ID and allowlisted argv array.",
            }
        from app.services.command_sandbox import CommandSandboxService
        from app.services.command_sandbox.types import CommandRequest

        proposal = CommandSandboxService().propose(
            CommandRequest(
                workspace_id=workspace_id,
                command=command,
                cwd=str(inputs.get("cwd") or "."),
                category=str(inputs.get("category") or "read_only"),
                created_by=f"agentic_core:{run['id']}",
            )
        )
        return {
            "status": "blocked",
            "summary": f"Created command proposal {proposal['id']}; it was not executed.",
            "blocker": "Command Sandbox approval is required.",
            "requires_approval": True,
            "approval_reference": proposal["id"],
            "evidence": [f"Command Sandbox record {proposal['id']} status: {proposal['status']}."],
            "next_action": "Review and approve or reject the Command Sandbox proposal.",
        }

    @staticmethod
    def _request_checkpoint(run: dict, state: dict, inputs: dict) -> dict:
        return {
            "status": "blocked",
            "summary": "Checkpoint creation remains in the controlled local Git flow.",
            "blocker": "Explicit checkpoint approval is required.",
            "requires_approval": True,
            "evidence": ["Agentic Core did not create or push a Git commit."],
            "next_action": "Use the existing managed-workspace checkpoint approval.",
        }

    @staticmethod
    def _delegate_subagent(run: dict, state: dict, inputs: dict) -> dict:
        result = inputs.get("result")
        if result is None and inputs.get("parent_run_id") and inputs.get("child_agent_id"):
            from app.services.agent_framework.delegation import AgentDelegationService
            from app.services.agent_framework.types import DelegationCreate

            delegation = AgentDelegationService().create(
                DelegationCreate(
                    parent_run_id=str(inputs["parent_run_id"]),
                    child_agent_id=str(inputs["child_agent_id"]),
                    objective=str(inputs.get("objective") or run["objective"]),
                    input=dict(inputs.get("delegated_input") or {}),
                    parent_agent_id=inputs.get("parent_agent_id"),
                )
            )
            return {
                "status": "blocked",
                "summary": f"Created bounded delegation {delegation.id}; result is pending.",
                "blocker": "A structured subagent result is required before continuation.",
                "requires_approval": True,
                "approval_reference": delegation.id,
                "evidence": [
                    f"Delegation {delegation.id} status: {delegation.status}.",
                    f"Bounded objective: {delegation.objective}",
                ],
                "next_action": "Run the bounded child workflow and record its structured result.",
            }
        if not isinstance(result, dict) or result.get("status") not in {"done", "blocked"}:
            return {
                "status": "blocked",
                "summary": "A bounded structured subagent result is required.",
                "blocker": "Subagent result is missing or malformed.",
                "evidence": ["No autonomous subagent mutation was performed."],
                "next_action": "Provide status, findings, evidence, and recommended_next_step.",
            }
        return {
            "status": "completed" if result["status"] == "done" else "blocked",
            "summary": f"Recorded bounded subagent result: {result['status']}.",
            "blocker": result.get("recommended_next_step")
            if result["status"] == "blocked"
            else None,
            "evidence": list(result.get("evidence") or result.get("findings") or []),
            "delegation_result": result,
            "next_action": result.get("recommended_next_step"),
        }

    @staticmethod
    def _inspect_research_evidence(run: dict, state: dict, inputs: dict) -> dict:
        evidence = list(inputs.get("evidence") or [])
        if not evidence:
            evidence = [
                str(item.get("content"))[:1000]
                for item in state.get("context_budget", {}).get("included_items", [])
                if item.get("kind") in {"research", "memory_summary"}
            ]
        if not evidence:
            from app.services.web_search import ReliableWebSearchService
            from app.services.web_search.types import WebSearchRunRequest

            web = ReliableWebSearchService().run(
                WebSearchRunRequest(
                    query=run["objective"], mode="research", freshness_required=True
                )
            )
            state["web_search_run_id"] = web.get("id")
            state["web_search_sources_used"] = [item.get("id") for item in web.get("sources", [])]
            state["web_search_evidence_used"] = [item.get("id") for item in web.get("evidence", [])]
            state["web_search_conflicts_found"] = [
                item.get("id") for item in web.get("conflicts", [])
            ]
            if web.get("evidence"):
                evidence = [str(item.get("evidence_text", ""))[:12_000] for item in web["evidence"]]
            elif web.get("error"):
                warning = web["error"]
                state["web_search_degraded_reason"] = warning
                return {
                    "status": "blocked",
                    "summary": "Research evidence retrieval degraded safely.",
                    "blocker": f"Research provider unavailable: {warning}",
                    "evidence": ["No unsupported research claim was synthesized."],
                    "next_action": (
                        "Enable a research provider or provide citation-bearing evidence."
                    ),
                }
        if not evidence:
            return {
                "status": "blocked",
                "summary": "No research evidence is available.",
                "blocker": "Evidence retrieval is unavailable or returned no sources.",
                "evidence": ["No citation-bearing evidence was recorded."],
                "next_action": "Enable a research source or provide evidence.",
            }
        return {
            "status": "completed",
            "summary": f"Inspected {len(evidence)} research evidence item(s).",
            "evidence": evidence,
            "next_action": "Cross-check and synthesize only supported claims.",
        }

    @staticmethod
    def _synthesize(run: dict, state: dict, inputs: dict) -> dict:
        verified = [item for item in state.get("verification_results", []) if item.get("passed")]
        context = state.get("context_budget", {}).get("included_items", [])
        if not verified and not context:
            return {
                "status": "blocked",
                "summary": "There is no verified context to synthesize.",
                "blocker": "Verified evidence is required.",
                "evidence": ["Synthesis stopped instead of inventing a result."],
                "next_action": "Gather and verify relevant context.",
            }
        return {
            "status": "completed",
            "summary": (
                "Prepared a bounded synthesis from persisted context and verification records."
            ),
            "evidence": [
                f"Verified result count: {len(verified)}.",
                f"Included context count: {len(context)}.",
            ],
            "next_action": "Verify completion criteria and produce the final report.",
        }

    @staticmethod
    def _final_report(run: dict, state: dict, inputs: dict) -> dict:
        evidence_count = sum(
            len(item.get("evidence") or []) for item in state.get("verification_results", [])
        )
        if not evidence_count:
            return {
                "status": "blocked",
                "summary": "A grounded final report cannot be produced without verified evidence.",
                "blocker": "Verified evidence is missing.",
                "evidence": ["Finalization stopped rather than inventing success."],
                "next_action": "Complete at least one verified step.",
            }
        return {
            "status": "completed",
            "summary": "Final report can be generated from persisted verified step results.",
            "evidence": [f"Grounding contains {evidence_count} evidence item(s)."],
            "next_action": "Mark the run done with a grounded report.",
        }

    @staticmethod
    def _coding_detail(source_run_id: str | None) -> dict[str, Any]:
        if not source_run_id:
            return {}
        try:
            from app.services.coding_agent import store
            from app.services.patch_apply import store as patch_store
            from app.services.test_runner import store as test_store

            run = store.get_run(source_run_id)
            if not run:
                return {}
            return {
                "run": run,
                "actions": store.list_actions(source_run_id),
                "patch_application": patch_store.get_application(run.get("patch_application_id"))
                if run.get("patch_application_id")
                else None,
                "test_run": test_store.get_run(run.get("test_run_id"))
                if run.get("test_run_id")
                else None,
            }
        except (LookupError, RuntimeError, ValueError):
            return {}
