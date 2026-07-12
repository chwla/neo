"""Deterministic report rendering that surfaces evidence, gaps, and audit state."""
# ruff: noqa: E501

from __future__ import annotations


def render(
    run: dict, evidence: list[dict], claims: list[dict], conflicts: list[dict], validation: dict
) -> tuple[str, dict]:
    plan = run.get("plan") or {}
    supported = [item for item in claims if item.get("status") == "supported"]
    finding_lines = [
        f"- {item['claim']} {' '.join(item.get('citation_ids') or [])}".strip()
        for item in supported
    ] or ["- No supported web-backed claims were collected; the result is intentionally limited."]
    uncertainty = [
        item["claim"] for item in claims if item.get("status") in {"uncertain", "conflicted"}
    ]
    source_lines = []
    for item in evidence:
        label = item.get("citation_label") or "[memory]"
        meta = item.get("metadata") or {}
        source_lines.append(
            f"- {label} {meta.get('title') or item.get('source_type')}: {meta.get('url') or 'workspace memory'}"
        )
    conflict_lines = [
        f"- {item['topic']} ({item['severity']}): {item['recommended_resolution']}"
        for item in conflicts
    ] or ["- No material conflicts were detected in the collected evidence."]
    confidence = run.get("confidence") or {}
    text = "\n".join(
        [
            f"# Research Report: {run['question']}",
            "",
            "## Executive Summary",
            f"Evidence-grounded research completed with overall confidence {confidence.get('overall', 0):.0%}. {len(supported)} supported finding(s) are included; unsupported claims are excluded.",
            "",
            "## Research Question",
            run["question"],
            "",
            "## Method / Search Plan",
            *[f"- {item}" for item in plan.get("subquestions", [])],
            "",
            "## Key Findings",
            *finding_lines,
            "",
            "## Evidence Table",
            *[
                f"- {item.get('citation_label') or '[memory]'} | quality {item.get('quality_score', 0):.0%} | {item.get('evidence_text', '')[:360]}"
                for item in evidence
            ],
            "",
            "## Detailed Analysis",
            "The findings above are limited to the recorded evidence passages; recommendations require local context and should be reviewed.",
            "",
            "## Conflicts / Disagreements",
            *conflict_lines,
            "",
            "## Confidence & Uncertainty",
            f"- Confidence breakdown: {confidence}",
            *(
                [f"- Uncertain or conflicted: {item}" for item in uncertainty]
                or ["- No additional uncertainty beyond normal source and context limitations."]
            ),
            "",
            "## Open Questions",
            "- Which project constraints or decision criteria should determine the final recommendation?",
            "",
            "## Recommended Next Steps",
            "- Verify high-impact decisions against the cited primary source and current project constraints.",
            "",
            "## Sources / Citations",
            *source_lines,
            "",
            "## Audit Summary",
            f"- Citation validation: {'passed' if validation.get('passed') else 'failed'}",
            f"- Checked claims: {validation.get('checked_claims', 0)}",
            *[f"- Validation issue: {item}" for item in validation.get("errors", [])],
        ]
    )
    sections = {
        "key_findings": finding_lines,
        "open_questions": [
            "Which project constraints or decision criteria should determine the final recommendation?"
        ],
        "citation_validation": validation,
    }
    return text, sections
