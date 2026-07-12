"""Research-level conflict normalization from bounded source evidence."""
# ruff: noqa: E501

from __future__ import annotations

import re


def detect(web_conflicts: list[dict], evidence: list[dict]) -> list[dict]:
    output = []
    for item in web_conflicts:
        output.append(
            {
                "topic": item.get("topic") or "source disagreement",
                "conflict_type": "version_mismatch"
                if item.get("topic") == "version"
                else f"{item.get('topic') or 'source'}_mismatch",
                "claims": [item.get("claim_a", ""), item.get("claim_b", "")],
                "sources": list(item.get("source_ids") or []),
                "severity": item.get("severity") or "medium",
                "recommended_resolution": "Prefer current official primary documentation and preserve the uncertainty if it cannot be resolved.",
            }
        )
    dates = {
        match.group(0)
        for entry in evidence
        for match in re.finditer(r"\b20\d{2}\b", entry.get("evidence_text", ""))
    }
    if len(dates) > 1 and not output:
        output.append(
            {
                "topic": "publication date",
                "conflict_type": "old_vs_current",
                "claims": sorted(dates),
                "sources": [],
                "severity": "low",
                "recommended_resolution": "Confirm the current date/version from an official source before acting.",
            }
        )
    return output
