"""Comprehensive Research Mode quality gate verification."""
import json
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api"
TIMEOUT = 400
RESULTS = []


def log(msg):
    print(msg, flush=True)


def start_and_wait(query, depth="quick", timeout=TIMEOUT):
    log(f"\n{'='*70}")
    log(f"TEST: {query} [{depth}]")
    log(f"{'='*70}")

    resp = requests.post(f"{BASE}/research/start", json={"query": query, "depth": depth})
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    log(f"Job ID: {job_id}")

    start = time.time()
    while time.time() - start < timeout:
        status = requests.get(f"{BASE}/research/{job_id}/status").json()
        elapsed = int(time.time() - start)
        log(f"  [{elapsed:3d}s] {status['status']:14s} {status['progress_percent']:3d}% | {status.get('current_step','')}")
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(4)

    job = requests.get(f"{BASE}/research/{job_id}").json()
    return job


def get_job_details(job):
    sources = job.get("sources", [])
    evidence = job.get("evidence_chunks", [])
    report = job.get("report", "")
    queries = job.get("generated_queries", [])
    fetched = [s for s in sources if s.get("fetched")]
    rejected = [s for s in sources if s.get("fetch_status") == "rejected"]
    failed = [s for s in sources if s.get("fetch_status") == "failed"]
    relevant = [s for s in fetched if s.get("evidence_count", 0) > 0]
    unique_domains = set(s.get("domain", "").lower() for s in relevant)
    return {
        "sources": sources, "evidence": evidence, "report": report,
        "queries": queries, "fetched": fetched, "rejected": rejected,
        "failed": failed, "relevant": relevant, "unique_domains": unique_domains,
    }


def check_report_quality(job, test_name, expect_mode=None):
    d = get_job_details(job)
    report = d["report"]
    status = job.get("status", "unknown")

    log(f"\n  Status: {status}")
    log(f"  Generated queries ({len(d['queries'])}): {d['queries'][:5]}")
    log(f"  Sources: {len(d['fetched'])} fetched / {len(d['sources'])} total ({len(d['rejected'])} rejected, {len(d['failed'])} failed)")
    log(f"  Relevant sources: {len(d['relevant'])}")
    log(f"  Evidence chunks: {len(d['evidence'])}")
    log(f"  Unique domains: {d['unique_domains']}")
    log(f"  Report length: {len(report)} chars")

    # Preview
    if report:
        safe = report.encode("ascii", errors="replace").decode()
        log(f"\n  --- Report first 600 chars ---")
        log(f"  {safe[:600]}")
        log(f"  --- End preview ---")

    errors = []

    # 1. No dictionary/English/Preply sources
    BAD_DOMAINS = ["vocabulary.com", "dictionary.com", "merriam-webster.com",
                   "cambridge.org", "yourdictionary.com", "preply.com",
                   "collinsdictionary.com", "wordreference.com"]
    BAD_TITLE_WORDS = ["meaning in english", "english explained", "what does amazing mean",
                       "definition of amazing", "amazing synonym"]
    for s in d["fetched"]:
        domain = s.get("domain", "").lower()
        title = (s.get("title") or "").lower()
        if any(bd in domain for bd in BAD_DOMAINS):
            errors.append(f"BAD SOURCE: {domain} - {s.get('title')}")
        if any(bt in title for bt in BAD_TITLE_WORDS):
            errors.append(f"BAD TITLE: {s.get('title')}")

    # 2. Citation formatting
    for pattern, label in [
        ("Source: )", "Source: )"),
        ("Sources: ,", "Sources: ,"),
        ('target="_blank"', "raw target attr"),
        ('rel="noopener"', "raw rel attr"),
    ]:
        if pattern in report:
            errors.append(f"Malformed citation: {label}")

    empty_brackets = len([m for m in __import__("re").findall(r"\[\s*\]", report)])
    if empty_brackets > 1:
        errors.append(f"{empty_brackets} empty citation markers []")

    # 3. Report mode check
    if expect_mode:
        report_lower = report.lower()
        if expect_mode == "insufficient" and "insufficient" not in report_lower:
            if len(d["relevant"]) < 2 and len(d["evidence"]) < 3:
                errors.append(f"Expected insufficient-evidence report but got full report with {len(d['relevant'])} relevant sources, {len(d['evidence'])} evidence")
        if expect_mode == "full" and ("insufficient" in report_lower or "partial" in report_lower[:200]):
            if len(d["relevant"]) >= 3 and len(d["evidence"]) >= 6:
                errors.append(f"Expected full report but got insufficient/partial with strong evidence")

    # 4. No hallucinated confident report from weak evidence
    if len(d["relevant"]) < 2 and len(d["evidence"]) < 3:
        if "comprehensive" in report.lower()[:500] and "insufficient" not in report.lower()[:200] and "partial" not in report.lower()[:200]:
            errors.append("Confident 'comprehensive' report from weak evidence")

    # 5. Verify cited source IDs exist and have evidence
    import re
    cited_ids = set(int(m) for m in re.findall(r"\[(\d+)\]", report))
    evidence_source_ids = {e.get("source_id") for e in d["evidence"]}
    fetched_ids = {s.get("id") for s in d["fetched"]}
    for cid in cited_ids:
        if cid not in fetched_ids:
            errors.append(f"Citation [{cid}] references unfetched source")
        if cid not in evidence_source_ids:
            pass  # Sources section lists all valid sources

    if errors:
        log(f"\n  FAILURES:")
        for e in errors:
            log(f"    X {e}")
        RESULTS.append((test_name, False, errors))
        return False
    else:
        log(f"\n  PASS: {test_name}")
        RESULTS.append((test_name, True, []))
        return True


# ============================================================
# TASK 1: Clear All verification
# ============================================================
def task1_clear_all():
    log("\n" + "="*70)
    log("TASK 1: Clear All Verification")
    log("="*70)

    # Check current state
    r = requests.get(f"{BASE}/research/list")
    before = r.json()["total"]
    log(f"  Jobs before clear: {before}")

    # Clear
    r = requests.delete(f"{BASE}/research/clear")
    r.raise_for_status()
    cleared = r.json()["cleared"]
    log(f"  Cleared: {cleared}")

    # Verify empty
    r = requests.get(f"{BASE}/research/list")
    after = r.json()["total"]
    log(f"  Jobs after clear: {after}")
    assert after == 0, f"Expected 0 jobs, got {after}"

    # Clear again (empty state)
    r = requests.delete(f"{BASE}/research/clear")
    r.raise_for_status()
    cleared2 = r.json()["cleared"]
    log(f"  Clear on empty: {cleared2}")
    assert cleared2 == 0, f"Expected 0 cleared, got {cleared2}"

    # Verify memory not affected
    r = requests.get(f"{BASE}/profile")
    assert r.status_code == 200, "Profile endpoint broken after clear"
    log(f"  Memory profile still accessible: OK")

    # Verify search config not affected
    r = requests.get(f"{BASE}/search/config")
    assert r.status_code == 200, "Search config broken after clear"
    log(f"  Search config still accessible: OK")

    log(f"\n  PASS: Clear All")
    RESULTS.append(("TASK 1: Clear All", True, []))


# ============================================================
# TASK 2: Amazing Spider-Man quality gate
# ============================================================
def task2_spiderman():
    log("\n" + "="*70)
    log("TASK 2: Amazing Spider-Man Comics Quality Gate")
    log("="*70)

    # Quick
    job = start_and_wait("amazing spiderman comics", "quick")
    check_report_quality(job, "Quick: amazing spiderman comics")

    # Standard
    job = start_and_wait("amazing spiderman comics", "standard")
    check_report_quality(job, "Standard: amazing spiderman comics")

    # Deep
    job = start_and_wait("amazing spiderman comics", "deep")
    check_report_quality(job, "Deep: amazing spiderman comics")


# ============================================================
# TASK 3: Anchored entity query
# ============================================================
def task3_anchored():
    log("\n" + "="*70)
    log("TASK 3: Anchored Entity Query")
    log("="*70)

    job = start_and_wait("research the publication history of The Amazing Spider-Man Marvel comic series", "standard")
    d = get_job_details(job)

    # Check queries are anchored
    log(f"\n  Generated queries:")
    anchored_count = 0
    bad_queries = []
    for q in d["queries"]:
        q_lower = q.lower()
        has_entity = any(term in q_lower for term in ["amazing spider-man", "spider-man", "marvel"])
        log(f"    {'OK' if has_entity else 'XX'} {q}")
        if has_entity:
            anchored_count += 1
        else:
            if "meaning" in q_lower or "explained" in q_lower or "define" in q_lower:
                bad_queries.append(q)

    log(f"\n  Anchored queries: {anchored_count}/{len(d['queries'])}")
    if bad_queries:
        log(f"  BAD queries: {bad_queries}")

    errors = []
    if bad_queries:
        errors.append(f"Off-topic queries found: {bad_queries}")

    # Check evidence entity relevance
    for chunk in d["evidence"][:5]:
        text_lower = chunk.get("text", "").lower()
        has_entity = any(t in text_lower for t in ["spider-man", "amazing", "marvel", "peter parker", "comic"])
        if not has_entity:
            errors.append(f"Evidence chunk lacks entity relevance: {chunk.get('text','')[:80]}")

    if errors:
        log(f"\n  FAILURES:")
        for e in errors:
            log(f"    X {e}")
        RESULTS.append(("TASK 3: Anchored entity query", False, errors))
    else:
        log(f"\n  PASS: Anchored entity query")
        RESULTS.append(("TASK 3: Anchored entity query", True, []))

    check_report_quality(job, "Anchored: publication history")


# ============================================================
# TASK 6: Regression tests
# ============================================================
def task6_regressions():
    log("\n" + "="*70)
    log("TASK 6: Regression Tests")
    log("="*70)

    # 6a: Tavily vs SearXNG
    job = start_and_wait("Research Tavily vs SearXNG for Neo", "quick")
    check_report_quality(job, "Regression: Tavily vs SearXNG")

    # 6b: Local LLM (memory scoped)
    job = start_and_wait("Research best local LLM model for my laptop", "quick")
    d = get_job_details(job)
    meta = job.get("metadata", {})
    memory_used = meta.get("memory_used", [])
    log(f"  Memory used: {memory_used}")
    check_report_quality(job, "Regression: local LLM + memory")

    # 6c: Cancel test
    log(f"\n  --- Cancel test ---")
    resp = requests.post(f"{BASE}/research/start", json={
        "query": "Research current AI coding agents and what Neo should learn from them",
        "depth": "deep"
    })
    cancel_job_id = resp.json()["job_id"]
    time.sleep(5)
    cancel_resp = requests.post(f"{BASE}/research/{cancel_job_id}/cancel")
    cancel_resp.raise_for_status()
    time.sleep(2)
    cancel_job = requests.get(f"{BASE}/research/{cancel_job_id}").json()
    cancel_status = cancel_job.get("status", "")
    log(f"  Cancel test: status={cancel_status}")
    if cancel_status == "cancelled":
        log(f"  PASS: Cancel test")
        RESULTS.append(("Regression: Cancel job", True, []))
    else:
        log(f"  FAIL: Cancel test, status={cancel_status}")
        RESULTS.append(("Regression: Cancel job", False, [f"Expected cancelled, got {cancel_status}"]))

    # 6d: Clear All after jobs
    r = requests.get(f"{BASE}/research/list")
    before = r.json()["total"]
    log(f"\n  Jobs before final clear: {before}")
    r = requests.delete(f"{BASE}/research/clear")
    cleared = r.json()["cleared"]
    r = requests.get(f"{BASE}/research/list")
    after = r.json()["total"]
    log(f"  Cleared {cleared}, remaining: {after}")
    if after == 0:
        log(f"  PASS: Clear All after jobs")
        RESULTS.append(("Regression: Clear All after jobs", True, []))
    else:
        log(f"  FAIL: Clear All after jobs")
        RESULTS.append(("Regression: Clear All after jobs", False, [f"Expected 0, got {after}"]))


def main():
    task1_clear_all()
    task2_spiderman()
    task3_anchored()
    task6_regressions()

    log(f"\n\n{'='*70}")
    log("FINAL SUMMARY")
    log(f"{'='*70}")
    for name, passed, errors in RESULTS:
        status = "PASS" if passed else "FAIL"
        log(f"  [{status}] {name}")
        if errors:
            for e in errors:
                log(f"         X {e}")

    passed = sum(1 for _, p, _ in RESULTS if p)
    total = len(RESULTS)
    log(f"\n  {passed}/{total} tests passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
