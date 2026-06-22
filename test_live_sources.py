"""Live-source Research Mode verification with SearXNG online."""
import json
import re
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api"
TIMEOUT = 500
ALL_JOBS = []
RESULTS = []


def log(msg):
    print(msg, flush=True)


def start_and_wait(query, depth="quick", timeout=TIMEOUT):
    log(f"\n{'='*70}")
    log(f"RESEARCH: {query} [{depth}]")
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
        time.sleep(5)

    job = requests.get(f"{BASE}/research/{job_id}").json()
    ALL_JOBS.append((query, depth, job))
    return job


def analyze_job(job, test_name):
    """Full analysis of a research job."""
    sources = job.get("sources", [])
    evidence = job.get("evidence_chunks", [])
    report = job.get("report", "")
    queries = job.get("generated_queries", [])
    status = job.get("status", "unknown")
    meta = job.get("metadata", {})

    fetched = [s for s in sources if s.get("fetched")]
    rejected = [s for s in sources if s.get("fetch_status") == "rejected"]
    failed = [s for s in sources if s.get("fetch_status") == "failed"]
    with_evidence = [s for s in fetched if s.get("evidence_count", 0) > 0]
    unique_domains = set(s.get("domain", "").lower() for s in with_evidence if s.get("domain"))

    log(f"\n  --- Job Internals: {test_name} ---")
    log(f"  Status: {status}")
    log(f"  Queries ({len(queries)}):")
    for q in queries:
        log(f"    - {q}")
    log(f"  Sources total: {len(sources)}")
    log(f"    Fetched:     {len(fetched)}")
    log(f"    Rejected:    {len(rejected)}")
    log(f"    Failed:      {len(failed)}")
    log(f"    With evidence: {len(with_evidence)}")
    log(f"  Evidence chunks: {len(evidence)}")
    log(f"  Unique domains: {unique_domains}")
    log(f"  Memory used: {meta.get('memory_used', [])}")

    if rejected:
        log(f"  Rejected sources:")
        for s in rejected[:5]:
            log(f"    X [{s.get('domain','')}] {s.get('title','')[:60]} reason={s.get('fetch_error','')}")

    if fetched:
        log(f"  Fetched sources:")
        for s in fetched[:8]:
            ev_count = s.get("evidence_count", 0)
            log(f"    {'OK' if ev_count > 0 else '--'} [{s.get('domain','')}] {s.get('title','')[:60]} evidence={ev_count}")

    if evidence:
        log(f"  Evidence samples:")
        for e in evidence[:5]:
            log(f"    [{e.get('source_id','')}] score={e.get('relevance_score',0):.2f} | {e.get('text','')[:100]}")

    # Report mode
    report_lower = report.lower()[:300]
    if "insufficient" in report_lower:
        report_mode = "insufficient"
    elif "partial" in report_lower:
        report_mode = "partial"
    else:
        report_mode = "full"
    log(f"  Report mode: {report_mode}")
    log(f"  Report length: {len(report)} chars")

    # Preview
    safe = report.encode("ascii", errors="replace").decode()
    log(f"\n  --- Report preview (800 chars) ---")
    log(safe[:800])
    log(f"  --- End preview ---")

    return {
        "status": status, "queries": queries, "sources": sources,
        "fetched": fetched, "rejected": rejected, "failed": failed,
        "with_evidence": with_evidence, "unique_domains": unique_domains,
        "evidence": evidence, "report": report, "report_mode": report_mode,
        "meta": meta,
    }


def check_quality(d, test_name, checks):
    """Run quality checks and record results."""
    errors = []
    for check_name, check_fn in checks:
        try:
            result = check_fn(d)
            if result:
                errors.append(f"{check_name}: {result}")
        except Exception as e:
            errors.append(f"{check_name}: EXCEPTION {e}")

    if errors:
        log(f"\n  FAILURES for {test_name}:")
        for e in errors:
            log(f"    X {e}")
        RESULTS.append((test_name, False, errors))
    else:
        log(f"\n  PASS: {test_name}")
        RESULTS.append((test_name, True, []))
    return not errors


# ---- Quality check functions ----

BAD_DOMAINS = ["vocabulary.com", "dictionary.com", "merriam-webster.com",
               "cambridge.org", "yourdictionary.com", "preply.com",
               "collinsdictionary.com", "wordreference.com", "thesaurus.com"]
BAD_TITLE_WORDS = ["meaning in english", "english explained", "what does amazing mean",
                   "definition of amazing", "amazing synonym", "meaning of amazing"]


def no_bad_sources(d):
    for s in d["fetched"]:
        domain = (s.get("domain") or "").lower()
        title = (s.get("title") or "").lower()
        if any(bd in domain for bd in BAD_DOMAINS):
            return f"Bad domain source: {domain} - {title[:50]}"
        if any(bt in title for bt in BAD_TITLE_WORDS):
            return f"Bad title source: {title[:60]}"
    return None


def no_malformed_citations(d):
    report = d["report"]
    for pattern, label in [
        ("Source: )", "Source: )"),
        ("Sources: ,", "Sources: ,"),
        ('target="_blank"', "raw target attr"),
        ('rel="noopener"', "raw rel attr"),
    ]:
        if pattern in report:
            return f"Malformed: {label}"
    empty = len(re.findall(r"\[\s*\]", report))
    if empty > 1:
        return f"{empty} empty citation markers"
    return None


def no_hallucinated_confident_report(d):
    if len(d["with_evidence"]) < 2 and len(d["evidence"]) < 3:
        if d["report_mode"] == "full":
            return "Full report from weak evidence"
    return None


def cited_sources_valid(d):
    if d["report_mode"] == "insufficient":
        return None
    cited_ids = set(int(m) for m in re.findall(r"\[(\d+)\]", d["report"]))
    fetched_ids = {s.get("id") for s in d["fetched"]}
    for cid in cited_ids:
        if cid > len(d["sources"]):
            return f"Citation [{cid}] exceeds source count {len(d['sources'])}"
    return None


def evidence_mentions_entity(d, entity_terms):
    if not d["evidence"]:
        return None
    relevant = 0
    for e in d["evidence"]:
        text = (e.get("text") or "").lower()
        if any(t in text for t in entity_terms):
            relevant += 1
    if relevant == 0 and len(d["evidence"]) > 0:
        return f"No evidence chunks mention entity terms {entity_terms}"
    return None


# ============================================================
# TASK 2: Clear old jobs
# ============================================================
def task2_clear():
    log("\n" + "="*70)
    log("TASK 2: Clear All Verification")
    log("="*70)

    r = requests.get(f"{BASE}/research/list")
    before = r.json()["total"]
    log(f"  Jobs before clear: {before}")

    r = requests.delete(f"{BASE}/research/clear")
    r.raise_for_status()
    log(f"  Cleared: {r.json()['cleared']}")

    r = requests.get(f"{BASE}/research/list")
    after = r.json()["total"]
    log(f"  Jobs after clear: {after}")
    assert after == 0

    r = requests.delete(f"{BASE}/research/clear")
    log(f"  Clear on empty: {r.json()['cleared']} (no crash)")

    r = requests.get(f"{BASE}/profile")
    assert r.status_code == 200
    log(f"  Memory OK")

    r = requests.get(f"{BASE}/search/config")
    assert r.status_code == 200
    log(f"  Search config OK")

    log(f"  PASS: Clear All")
    RESULTS.append(("TASK 2: Clear All", True, []))


# ============================================================
# TASK 3: Real-source research tests
# ============================================================
def task3_tests():
    log("\n" + "="*70)
    log("TASK 3: Real-Source Research Tests")
    log("="*70)

    # 3a: Amazing Spider-Man - Quick
    job = start_and_wait("amazing spiderman comics", "quick")
    d = analyze_job(job, "Quick: amazing spiderman comics")
    check_quality(d, "Quick: amazing spiderman comics", [
        ("No bad sources", no_bad_sources),
        ("No malformed citations", no_malformed_citations),
        ("No hallucinated confident report", no_hallucinated_confident_report),
        ("Cited sources valid", cited_sources_valid),
    ])

    # 3b: Amazing Spider-Man - Standard
    job = start_and_wait("amazing spiderman comics", "standard")
    d = analyze_job(job, "Standard: amazing spiderman comics")
    check_quality(d, "Standard: amazing spiderman comics", [
        ("No bad sources", no_bad_sources),
        ("No malformed citations", no_malformed_citations),
        ("No hallucinated confident report", no_hallucinated_confident_report),
        ("Cited sources valid", cited_sources_valid),
    ])

    # 3c: Publication history - Standard
    job = start_and_wait("research the publication history of The Amazing Spider-Man Marvel comic series", "standard")
    d = analyze_job(job, "Publication history")
    check_quality(d, "Publication history", [
        ("No bad sources", no_bad_sources),
        ("No malformed citations", no_malformed_citations),
        ("No hallucinated confident report", no_hallucinated_confident_report),
        ("Cited sources valid", cited_sources_valid),
        ("Evidence mentions entity", lambda d: evidence_mentions_entity(d, ["spider-man", "amazing", "marvel", "comic"])),
    ])

    # 3d: Tavily vs SearXNG - Standard
    job = start_and_wait("Research Tavily vs SearXNG for Neo", "standard")
    d = analyze_job(job, "Tavily vs SearXNG")
    check_quality(d, "Tavily vs SearXNG", [
        ("No malformed citations", no_malformed_citations),
        ("Cited sources valid", cited_sources_valid),
    ])

    # 3e: Local LLM - Quick
    job = start_and_wait("Research best local LLM model for my laptop", "quick")
    d = analyze_job(job, "Local LLM laptop")
    memory_used = d["meta"].get("memory_used", [])
    log(f"  Memory used: {memory_used}")
    checks = [
        ("No malformed citations", no_malformed_citations),
        ("Cited sources valid", cited_sources_valid),
    ]
    check_quality(d, "Local LLM laptop", checks)


def main():
    task2_clear()
    task3_tests()

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

    log(f"\n\n{'='*70}")
    log("SOURCE/EVIDENCE SUMMARY TABLE")
    log(f"{'='*70}")
    log(f"  {'Query':<50} {'Mode':<10} {'Src':<5} {'Fetch':<6} {'Rej':<5} {'Evid':<5} {'Domains':<10} {'Report'}")
    log(f"  {'-'*50} {'-'*10} {'-'*5} {'-'*6} {'-'*5} {'-'*5} {'-'*10} {'-'*15}")
    for query, depth, job in ALL_JOBS:
        sources = job.get("sources", [])
        evidence = job.get("evidence_chunks", [])
        fetched = [s for s in sources if s.get("fetched")]
        rejected = [s for s in sources if s.get("fetch_status") == "rejected"]
        with_ev = [s for s in fetched if s.get("evidence_count", 0) > 0]
        domains = set(s.get("domain", "").lower() for s in with_ev if s.get("domain"))
        report = job.get("report", "").lower()[:200]
        rmode = "insufficient" if "insufficient" in report else ("partial" if "partial" in report else "full")
        q = query[:48]
        log(f"  {q:<50} {depth:<10} {len(sources):<5} {len(fetched):<6} {len(rejected):<5} {len(evidence):<5} {len(domains):<10} {rmode}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
