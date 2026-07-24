from __future__ import annotations

# ruff: noqa: E501
import json

from .redaction import redact


def report(run, results, comparison=None):
    payload = {
        "summary": run.get("summary", {}),
        "run": run,
        "case_results": results,
        "metric_breakdown": {},
        "hard_failures": [],
        "warnings": [],
        "baseline_comparison": comparison,
        "linked_artifacts": [],
        "safety_audit": {"passed": not any(r["hard_failures"] for r in results)},
        "recommended_fixes": [],
    }
    for result in results:
        for key, value in result["metrics"].items():
            payload["metric_breakdown"].setdefault(key, []).append(value)
        payload["hard_failures"] += result["hard_failures"]
        payload["warnings"] += result["warnings"]
    payload["metric_breakdown"] = {
        k: sum(v) / len(v) for k, v in payload["metric_breakdown"].items()
    }
    payload["recommended_fixes"] = [f"Resolve {x}" for x in payload["hard_failures"]]
    payload = redact(payload)
    payload["json"] = json.dumps(payload, indent=2, sort_keys=True)
    hard_failures = len(payload["hard_failures"])
    payload["markdown"] = (
        f"# Evaluation Report\n\nScore: {run.get('overall_score')}\n\nHard failures: {hard_failures}"
    )
    return payload
