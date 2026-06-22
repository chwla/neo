"""Live tests for AI coding tools comparison topic intent fix."""
import re
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api"
TIMEOUT = 600

TESTS = [
    ("cursor vs codex", "quick"),
    ("cursor vs codex", "standard"),
    ("cursor vs codex", "deep"),
    ("codex pro or cursor pro", "quick"),
    ("claude code vs cursor vs codex", "standard"),
]

OFFTOPIC_PATTERNS = [
    r"\bsql\s+cursor\b",
    r"\bui\s+cursor\b",
    r"\bmouse\s+cursor\b",
    r"\bmanuscript\s+codex\b",
    r"\bancient\s+codex\b",
    r"\bhistorical\s+origins?\b",
    r"\bdata\s+storage\b",
    r"\bliterature\b.*\bphilosophy\b",
    r"\bcursor\s+definition\b",
    r"\bcodex\s+manuscript\b",
]

DISCLAIMER_CONTEXTS = re.compile(
    r"(out of scope|not researched|excluded meanings?|disambiguation|"
    r"not applicable|not covered|excluded from|does not cover|"
    r"not about|do not discuss|not generic|not dictionary)",
    re.IGNORECASE,
)

PLACEHOLDER_PATTERNS = [
    r"\|\s*\.\.\.\s*\|",
    r"\|\s*Cursor\s*\|\s*\.\.\.",
]

BAD_QUERY_TERMS = ["sql cursor", "ui cursor", "manuscript", "ancient codex", "historical origins"]
BAD_SOURCE_MARKERS = [
    "geeksforgeeks.org/cursor-in",
    "wikipedia.org/wiki/Cursor_(user_interface)",
    "wikipedia.org/wiki/Codex",
]


def log(msg):
    print(msg, flush=True)


def clear_jobs():
    requests.delete(f"{BASE}/research/clear", timeout=30)


def run_test(query, depth):
    resp = requests.post(
        f"{BASE}/research/start",
        json={"query": query, "depth": depth},
        timeout=30,
    )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    log(f"  Job: {job_id}")

    start = time.time()
    while time.time() - start < TIMEOUT:
        status = requests.get(f"{BASE}/research/{job_id}/status", timeout=30).json()
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(5)

    job = requests.get(f"{BASE}/research/{job_id}", timeout=30).json()
    return job


def _is_in_disclaimer_context(report, match_start):
    """Return True if the match is inside a disclaimer/out-of-scope context."""
    window_start = max(0, match_start - 200)
    preceding = report[window_start:match_start].lower()
    return bool(DISCLAIMER_CONTEXTS.search(preceding))


def _check_offtopic_in_report(report):
    """Check for off-topic patterns in actual research content, not disclaimers."""
    errors = []
    for pat in OFFTOPIC_PATTERNS:
        for m in re.finditer(pat, report, re.IGNORECASE):
            if not _is_in_disclaimer_context(report, m.start()):
                errors.append(f"Off-topic content in report: '{m.group()}' at pos {m.start()}")
    return errors


def check_job(job, query, depth):
    errors = []
    report = job.get("report") or ""
    plan = job.get("plan") or {}
    metadata = job.get("metadata") or {}
    queries = job.get("generated_queries") or []
    sources = job.get("sources") or []
    evidence = job.get("evidence_chunks") or []

    # --- TASK 1: topic_intent ---
    topic = plan.get("topic_intent") or metadata.get("topic_intent")
    if topic != "ai_coding_tools_comparison":
        errors.append(f"topic_intent={topic!r}, expected ai_coding_tools_comparison")

    # --- TASK 2: entity-locked queries ---
    for q in queries:
        ql = q.lower()
        if any(x in ql for x in BAD_QUERY_TERMS):
            errors.append(f"Off-topic query generated: {q}")

    # --- TASK 3: off-topic content (context-aware) ---
    errors.extend(_check_offtopic_in_report(report))

    # --- TASK 4: placeholder table rows ---
    for pat in PLACEHOLDER_PATTERNS:
        if re.search(pat, report):
            errors.append(f"Placeholder in comparison table: {pat}")

    # --- TASK 5: confidence requires official sources ---
    report_lower = report.lower()
    if "confidence:** high" in report_lower.replace(" ", ""):
        has_cursor_official = any(
            "cursor.com" in (s.get("domain") or "").lower()
            for s in sources if s.get("evidence_count", 0) > 0
        )
        has_codex_official = any(
            "openai.com" in (s.get("domain") or "").lower()
            or "github.com/openai/codex" in (s.get("url") or "").lower()
            for s in sources if s.get("evidence_count", 0) > 0
        )
        if not (has_cursor_official and has_codex_official):
            errors.append("High confidence without official sources on both sides")

    # --- TASK 6: top-level section order (## N.) ---
    # Verify present sections are monotonically increasing (correct order).
    # Not all sections need to exist (partial reports may omit 4-9).
    section_nums = [int(n) for n in re.findall(r"^##\s+(\d+)\.", report, re.MULTILINE)]
    if section_nums:
        for i in range(1, len(section_nums)):
            if section_nums[i] < section_nums[i - 1]:
                errors.append(
                    f"Section order wrong: {section_nums[i]} after {section_nums[i-1]} "
                    f"(sections: {section_nums[:10]})"
                )
                break

    # --- TASK 7: required sections exist ---
    required_sections = {1: "Executive Summary", 3: "Evidence Quality", 10: "Sources"}
    for num, name in required_sections.items():
        if num not in section_nums:
            errors.append(f"Missing ## {num}. {name}")

    # --- TASK 8: inline ## headings (not ### subsections) ---
    # Match non-newline char followed by ## but exclude ### (subsection headings are valid)
    for m in re.finditer(r"[^\n](##)\s+\d+\.", report):
        preceding_char = report[m.start()]
        heading_hashes = report[m.start() + 1:m.start() + 3]
        # If there's a # before the ##, it's actually ### — that's a valid subsection
        if m.start() > 0 and report[m.start()] == '#':
            continue
        errors.append(
            f"Heading inline inside paragraph at pos {m.start()}: "
            f"...{report[max(0, m.start()-20):m.start()+30]}..."
        )

    # --- TASK 9: AI coding tool context present ---
    ai_terms = sum(
        1 for t in [
            "cursor ai", "openai codex", "codex cli", "coding agent",
            "cursor.com", "ai editor", "ai ide", "code editor",
        ]
        if t in report_lower
    )
    if ai_terms < 2:
        errors.append(f"Report lacks AI coding tool context (only {ai_terms} key terms)")

    # --- TASK 10: off-topic sources cited ---
    cited_source_ids = set(int(x) for x in re.findall(r"\[(\d+)\]", report))
    source_map = {s.get("id"): s for s in sources}
    for sid in cited_source_ids:
        src = source_map.get(sid)
        if not src:
            continue
        url = (src.get("url") or "").lower()
        if any(marker in url for marker in BAD_SOURCE_MARKERS):
            errors.append(f"Off-topic source cited: [{sid}] {src.get('title', '')} — {url}")

    # --- TASK 11: evidence categories ---
    if evidence:
        categories = set(e.get("evidence_category", "general") for e in evidence)
        irrelevant_count = sum(1 for e in evidence if e.get("evidence_category") == "irrelevant")
        if irrelevant_count > len(evidence) * 0.3:
            errors.append(f"Too many irrelevant evidence chunks: {irrelevant_count}/{len(evidence)}")

    # --- TASK 12: basic format hygiene ---
    for pattern, label in [
        ("Source: )", "Source: )"),
        ("Sources: ,", "Sources: ,"),
        ('target="_blank"', "raw target attr"),
        ('rel="noopener"', "raw rel attr"),
    ]:
        if pattern in report:
            errors.append(f"Malformed citation: {label}")

    return {
        "query": query,
        "depth": depth,
        "status": job.get("status"),
        "topic_intent": topic,
        "queries_count": len(queries),
        "evidence_count": len(evidence),
        "report_len": len(report),
        "confidence": re.search(r"\*\*Confidence:\*\*\s*(\w+)", report),
        "errors": errors,
        "pass": len(errors) == 0 and job.get("status") == "completed",
    }


def main():
    log("Clearing old jobs...")
    clear_jobs()

    results = []
    for query, depth in TESTS:
        log(f"\n{'='*60}")
        log(f"TEST: {query!r} [{depth}]")
        log(f"{'='*60}")
        try:
            job = run_test(query, depth)
            result = check_job(job, query, depth)
        except Exception as exc:
            result = {"query": query, "depth": depth, "pass": False, "errors": [str(exc)]}
        results.append(result)
        conf = result.get("confidence")
        conf_str = conf.group(1) if conf else "?"
        log(f"  Status: {result.get('status')} | topic: {result.get('topic_intent')} | confidence: {conf_str}")
        log(f"  Queries: {result.get('queries_count')} | Evidence: {result.get('evidence_count')} | Report: {result.get('report_len')} chars")
        if result["errors"]:
            for e in result["errors"]:
                log(f"  FAIL: {e}")
        else:
            log("  PASS")

    log(f"\n{'='*60}")
    log("SUMMARY")
    log(f"{'='*60}")
    passed = sum(1 for r in results if r["pass"])
    log(f"{passed}/{len(results)} passed")
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        errs = f" ({len(r.get('errors', []))} errors)" if not r["pass"] else ""
        log(f"  [{mark}] {r['query']} [{r['depth']}]{errs}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
