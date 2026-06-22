"""Research Mode quality gate tests."""
import json
import time
import requests

BASE = "http://127.0.0.1:8000/api"
TIMEOUT = 360


def start_and_wait(query, depth="quick", timeout=TIMEOUT):
    print(f"\n{'='*70}")
    print(f"TEST: {query} [{depth}]")
    print(f"{'='*70}")

    resp = requests.post(f"{BASE}/research/start", json={"query": query, "depth": depth})
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    print(f"Job ID: {job_id}")

    start = time.time()
    while time.time() - start < timeout:
        status = requests.get(f"{BASE}/research/{job_id}/status").json()
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] {status['status']:14s} {status['progress_percent']:3d}% | {status.get('current_step','')}")

        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(3)

    job = requests.get(f"{BASE}/research/{job_id}").json()
    return job


def check_report(job, test_name):
    report = job.get("report", "")
    status = job.get("status", "unknown")
    sources = job.get("sources", [])
    evidence = job.get("evidence_chunks", [])
    metadata = job.get("metadata", {})

    fetched = [s for s in sources if s.get("fetched")]
    rejected = [s for s in sources if s.get("fetch_status") == "rejected"]
    relevant = [s for s in fetched if s.get("evidence_count", 0) > 0]
    unique_domains = len(set(s.get("domain", "").lower() for s in relevant))

    print(f"\n  Status: {status}")
    print(f"  Sources: {len(fetched)} fetched / {len(sources)} total ({len(rejected)} rejected)")
    print(f"  Relevant sources (with evidence): {len(relevant)}")
    print(f"  Evidence chunks: {len(evidence)}")
    print(f"  Unique domains: {unique_domains}")
    print(f"  Report length: {len(report)} chars")

    if report:
        safe = report.encode("ascii", errors="replace").decode()
        print(f"\n  --- Report preview (first 800 chars) ---")
        print(f"  {safe[:800]}")
        print(f"  --- End preview ---")

    errors = []

    # Check no dictionary/English-learning sources
    bad_sources = []
    for s in fetched:
        domain = s.get("domain", "").lower()
        title = (s.get("title") or "").lower()
        if any(d in domain for d in ["vocabulary.com", "dictionary.com", "merriam-webster.com",
                                      "cambridge.org", "yourdictionary.com"]):
            bad_sources.append(f"{s.get('domain')}: {s.get('title')}")
        if "meaning in english" in title or "english explained" in title:
            bad_sources.append(f"{s.get('domain')}: {s.get('title')}")
    if bad_sources:
        errors.append(f"IRRELEVANT SOURCES still present: {bad_sources}")

    # Check citation formatting
    if "Source: )" in report:
        errors.append("Malformed 'Source: )' found in report")
    if "Sources: ," in report:
        errors.append("Malformed 'Sources: ,' found in report")
    if 'target="_blank"' in report:
        errors.append("Raw HTML attributes in report text")
    if 'rel="noopener"' in report:
        errors.append("Raw HTML rel attribute in report text")
    if "[]" in report and report.count("[]") > 1:
        errors.append("Empty citation markers [] in report")

    # Check report mode for weak evidence
    if len(relevant) < 2 and len(evidence) < 3:
        if "insufficient" not in report.lower() and "partial" not in report.lower():
            if "comprehensive" in report.lower() or "detailed analysis" in report.lower():
                errors.append(f"Report claims to be comprehensive with only {len(relevant)} relevant source(s) and {len(evidence)} evidence chunk(s)")

    if errors:
        print(f"\n  FAILURES:")
        for e in errors:
            print(f"    X {e}")
        return False

    print(f"\n  PASS: {test_name}")
    return True


def main():
    results = []

    # Test 1: Quick - amazing spiderman comics
    job = start_and_wait("amazing spiderman comics", "quick")
    results.append(("Quick: amazing spiderman comics", check_report(job, "Quick comics")))

    # Test 2: Standard - amazing spiderman comics
    job = start_and_wait("amazing spiderman comics", "standard")
    results.append(("Standard: amazing spiderman comics", check_report(job, "Standard comics")))

    # Test 3: Deep - amazing spiderman comics
    job = start_and_wait("amazing spiderman comics", "deep")
    results.append(("Deep: amazing spiderman comics", check_report(job, "Deep comics")))

    # Test 4: Specific entity query
    job = start_and_wait("research the publication history of The Amazing Spider-Man Marvel comic series", "standard")
    results.append(("Specific: publication history", check_report(job, "Publication history")))

    print(f"\n\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")

    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"\n  {passed}/{total} tests passed")


if __name__ == "__main__":
    main()
