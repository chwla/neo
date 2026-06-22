"""Test strict report format with live SearXNG sources."""
import re
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api"
RESULTS = []


def log(msg):
    print(msg, flush=True)


def start_and_wait(query, depth="quick", timeout=500):
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
        log(f"  [{elapsed:3d}s] {status['status']:14s} {status['progress_percent']:3d}%")
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(5)

    job = requests.get(f"{BASE}/research/{job_id}").json()
    return job


def check_format(job, test_name):
    report = job.get("report", "")
    safe = report.encode("ascii", errors="replace").decode()

    log(f"\n  Report length: {len(report)} chars")
    log(f"\n  --- Full Report ---")
    log(safe[:3000])
    log(f"  --- End (truncated) ---")

    errors = []

    # Check for malformed citations
    for pattern, label in [
        ("Source: )", "Source: )"),
        ("Sources: ,", "Sources: ,"),
        ('target="_blank"', "raw target attr"),
        ('rel="noopener"', "raw rel attr"),
    ]:
        if pattern in report:
            errors.append(f"Malformed: {label}")

    # Check for empty citation markers
    empty = len(re.findall(r"\[\s*\]", report))
    if empty > 1:
        errors.append(f"{empty} empty citation markers")

    # Check for filler phrases
    for filler in ["This report delves", "In conclusion", "As we can see"]:
        if filler.lower() in report.lower():
            errors.append(f"Filler phrase: '{filler}'")

    # Check report has header metadata
    if "**Query:**" not in report:
        errors.append("Missing **Query:** header")
    if "**Mode:**" not in report:
        errors.append("Missing **Mode:** header")
    if "**Report type:**" not in report:
        errors.append("Missing **Report type:** header")
    if "**Generated:**" not in report:
        errors.append("Missing **Generated:** header")
    if "**Confidence:**" not in report:
        errors.append("Missing **Confidence:** header")

    # For full/partial reports, check section structure
    report_lower = report.lower()
    is_insufficient = "insufficient" in report_lower[:200]

    if not is_insufficient:
        # Should have Sources section
        if "## 10. Sources" not in report and "## Sources" not in report:
            errors.append("Missing Sources section")

        # Check sources section is at the end
        sources_match = re.search(r"## (?:10\. )?Sources", report)
        if sources_match:
            sources_section = report[sources_match.start():]
            source_lines = [l for l in sources_section.split("\n") if l.startswith("[")]
            if not source_lines:
                errors.append("Sources section has no [N] entries")
            else:
                log(f"  Sources listed: {len(source_lines)}")

        # Check Evidence Quality section exists
        if "Evidence Quality" not in report and "Evidence grade" not in report:
            errors.append("Missing Evidence Quality section")

    if errors:
        log(f"\n  FORMAT FAILURES:")
        for e in errors:
            log(f"    X {e}")
        RESULTS.append((test_name, False, errors))
    else:
        log(f"\n  FORMAT PASS: {test_name}")
        RESULTS.append((test_name, True, []))
    return not errors


def main():
    # Clear old jobs
    requests.delete(f"{BASE}/research/clear")

    # Test 1: Full report - comparison query
    job = start_and_wait("Research Tavily vs SearXNG for Neo", "quick")
    check_format(job, "Tavily vs SearXNG (quick)")

    # Test 2: Full report - entity query
    job = start_and_wait("amazing spiderman comics", "quick")
    check_format(job, "Spider-Man (quick)")

    # Test 3: Memory-scoped
    job = start_and_wait("Research best local LLM model for my laptop", "quick")
    check_format(job, "Local LLM (quick)")

    log(f"\n\n{'='*70}")
    log("FINAL SUMMARY")
    log(f"{'='*70}")
    passed = 0
    for name, ok, errors in RESULTS:
        status = "PASS" if ok else "FAIL"
        log(f"  [{status}] {name}")
        if errors:
            for e in errors:
                log(f"         X {e}")
        if ok:
            passed += 1

    log(f"\n  {passed}/{len(RESULTS)} tests passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
