"""Citation validation prevents final factual claims from being unsupported."""

from __future__ import annotations


def validate(claims: list[dict], evidence: list[dict]) -> dict:
    evidence_ids = {item.get("id") for item in evidence}
    labels = {item.get("citation_label") for item in evidence if item.get("citation_label")}
    errors: list[str] = []
    checked = 0
    for claim in claims:
        if claim.get("status") == "unsupported":
            continue
        checked += 1
        claim_ref = claim.get("id") or claim.get("claim", "")[:50]
        claim_evidence = claim.get("evidence_ids") or []
        claim_citations = claim.get("citation_ids") or []
        if not claim_evidence:
            errors.append(
                f"Claim {claim.get('id') or claim.get('claim', '')[:50]} has no evidence."
            )
        elif any(item not in evidence_ids for item in claim_evidence):
            errors.append(
                f"Claim {claim_ref} references missing evidence."
            )
        if claim.get("status") == "supported" and not claim_citations:
            errors.append(
                f"Supported claim {claim_ref} has no citation label."
            )
        elif any(label not in labels for label in claim_citations):
            errors.append(
                f"Claim {claim_ref} has an unresolved citation label."
            )
    return {"passed": not errors, "checked_claims": checked, "errors": errors}
