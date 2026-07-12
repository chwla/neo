from __future__ import annotations

from . import fixtures, store
from .reports import report
from .scorers import score


class EvaluationService:
    def seed_builtins(self):
        for name, (_, description) in fixtures.BUILTIN_SUITES.items():
            store.create_suite(
                name, description, {"builtin": True, "offline": True}, fixtures.builtin_cases(name)
            )
        return self.suites()

    def suites(self):
        self.seed_builtins() if not store.suites() else None
        return store.suites()

    def create_suite(self, name, description="", cases=None, config=None):
        return store.create_suite(name, description, config or {}, cases or [])

    def suite(self, sid):
        return store.suite(sid)

    def run(
        self, sid, fixture_mode=True, max_cases=None, fail_fast=False, compare_baseline_id=None
    ):
        suite = store.suite(sid)
        if not suite:
            raise LookupError("Evaluation suite not found.")
        run = store.create_run(suite["id"], fixture_mode)
        selected = store.cases(suite["id"])[:max_cases]
        for case in selected:
            output = case["fixture"] if fixture_mode else case["expected"]
            result = score(case, output)
            store.add_result(run["id"], case["id"], result, output)
            if fail_fast and result["hard_failures"]:
                break
        rows = store.results(run["id"])
        overall = sum(r["score"] for r in rows) / len(rows) if rows else 0
        hard = sum(len(r["hard_failures"]) for r in rows)
        run = store.finish_run(
            run["id"], overall, hard, {"case_count": len(rows), "fixture_mode": fixture_mode}
        )
        return {
            "run": run,
            "report": report(
                run,
                rows,
                self.compare(run["id"], compare_baseline_id) if compare_baseline_id else None,
            ),
        }

    def runs(self):
        return store.runs()

    def detail(self, rid):
        value = store.run(rid)
        if not value:
            raise LookupError("Evaluation run not found.")
        return value

    def cases(self, rid):
        return store.results(rid)

    def set_baseline(self, rid, name="stable", threshold=0.05):
        return store.baseline(rid, name, threshold)

    def baselines(self):
        return store.baselines()

    def compare(self, rid, baseline_id=None):
        current = self.detail(rid)
        base = (
            next((b for b in store.baselines() if b["id"] == baseline_id), None)
            if baseline_id
            else next(
                (b for b in reversed(store.baselines()) if b["suite_id"] == current["suite_id"]),
                None,
            )
        )
        if not base:
            return {"available": False, "regression": False}
        previous = self.detail(base["run_id"])
        delta = current["overall_score"] - previous["overall_score"]
        regression = delta < -base["threshold"] or (
            current["hard_failure_count"] > 0 and previous["hard_failure_count"] == 0
        )
        return {
            "available": True,
            "baseline": base,
            "score_delta": delta,
            "metric_delta": {},
            "new_failures": max(0, current["hard_failure_count"] - previous["hard_failure_count"]),
            "resolved_failures": max(
                0, previous["hard_failure_count"] - current["hard_failure_count"]
            ),
            "regression": regression,
        }

    def report(self, rid):
        value = self.detail(rid)
        return report(value, store.results(rid), self.compare(rid))

    def delete(self, rid):
        store.delete_run(rid)
