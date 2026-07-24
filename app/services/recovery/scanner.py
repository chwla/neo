from __future__ import annotations

import app.services.agents.store as agent_store
import app.services.coding_agent.store as coding_store
import app.services.recovery.store as recovery_store
import app.services.test_runner.store as test_store
from app.services.recovery.safety import CODING_RUNNING_STATUSES


class RecoveryScanner:
    """Detect and safely repair runs that were active when the process stopped.

    The scanner never resumes work. It only moves ambiguous running states into
    explicit interrupted/needs-review states and reopens executing approvals as
    pending so the user can decide what to do next.
    """

    def scan(self) -> dict:
        recovery_store.initialize_recovery_tables()
        return {
            "agent_runs": self._scan_agent_runs(),
            "coding_runs": self._scan_coding_runs(),
            "actions": self._scan_actions(),
            "test_runs": self._scan_tests(),
        }

    def _scan_agent_runs(self) -> int:
        count = 0
        for status in ("queued", "planning", "running"):
            runs, _ = agent_store.list_runs(status=status, limit=500)
            for run in runs:
                now = agent_store.now_iso()
                agent_store.update_run(
                    run["id"],
                    {
                        "status": "interrupted",
                        "error": "Run interrupted while no active worker was available.",
                    },
                )
                for step in agent_store.list_steps(run["id"]):
                    if step["status"] == "running":
                        agent_store.update_step(
                            step["id"],
                            {
                                "status": "interrupted",
                                "error": "Step interrupted while no active worker was available.",
                                "completed_at": now,
                            },
                        )
                recovery_store.insert_event(
                    run_type="agent",
                    run_id=run["id"],
                    event_type="detected_stuck",
                    status_before=status,
                    status_after="interrupted",
                    metadata={"safe_action": "retry_or_fork_required"},
                )
                count += 1
        return count

    def _scan_coding_runs(self) -> int:
        count = 0
        for status in CODING_RUNNING_STATUSES:
            runs, _ = coding_store.list_runs(status=status, limit=500)
            for run in runs:
                after = (
                    "needs_review"
                    if status
                    in {
                        "applying_patch",
                        "running_tests",
                        "creating_checkpoint",
                        "analyzing_test_result",
                    }
                    else "interrupted"
                )
                now = coding_store.now_iso()
                coding_store.update_run(
                    run["id"],
                    {
                        "status": after,
                        "recovery_state": "needs_review",
                        "last_recoverable_at": now,
                        "error": (
                            "Coding run was interrupted. No patch/test/checkpoint was "
                            "executed by recovery."
                        ),
                        "updated_at": now,
                    },
                )
                recovery_store.insert_event(
                    run_type="coding_agent",
                    run_id=run["id"],
                    event_type="detected_stuck",
                    status_before=status,
                    status_after=after,
                    metadata={"safe_action": "resume_retry_or_fork_required"},
                )
                count += 1
        return count

    def _scan_actions(self) -> int:
        count = 0
        # There is no status filter in the store, so inspect recent runs.
        runs, _ = coding_store.list_runs(limit=500)
        for run in runs:
            for action in coding_store.list_actions(run["id"]):
                if action["status"] != "executing":
                    continue
                coding_store.update_action(
                    action["id"],
                    {
                        "status": "pending",
                        "error": "Action was executing during interruption; approval is required again.",
                        "updated_at": coding_store.now_iso(),
                    },
                )
                recovery_store.insert_event(
                    run_type="coding_agent",
                    run_id=run["id"],
                    event_type="state_repaired",
                    status_before="executing",
                    status_after="pending",
                    action_request_id=action["id"],
                    metadata={"reason": "approval action reopened; no action executed"},
                )
                count += 1
        return count

    def _scan_tests(self) -> int:
        count = 0
        runs, _ = test_store.list_runs(status="running", limit=500)
        for run in runs:
            test_store.update_run(
                run["id"],
                {
                    "status": "interrupted",
                    "error": "Test runner was interrupted; rerun requires explicit approval.",
                    "completed_at": test_store.now_iso(),
                },
            )
            if run.get("agent_run_id"):
                coding_runs, _ = coding_store.list_runs(limit=500)
                coding_run = next(
                    (
                        item
                        for item in coding_runs
                        if item.get("agent_run_id") == run["agent_run_id"]
                    ),
                    None,
                )
                if coding_run:
                    recovery_store.insert_event(
                        run_type="coding_agent",
                        run_id=coding_run["id"],
                        event_type="detected_stuck",
                        status_before="running_tests",
                        status_after="needs_review",
                        metadata={"test_run_id": run["id"]},
                    )
            count += 1
        return count
