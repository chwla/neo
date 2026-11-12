"""Run real research on this machine and report exactly what happened. Manual, not CI.

Uses the profile's own configured research model and search providers, through the same job
pipeline the app uses, so each run also appears in that profile's Research history.

    .venv/Scripts/python.exe scripts/research_live_check.py --profile <profile-id> \
        --depth quick "instagram vs facebook"

Prints the status, duration, findings with their strength and citations (and whether each
citation really resolves to accepted evidence read from a page), evidence accepted and
rejected by reason, sources, per-stage model timings and the run's fallback decisions.
Exits 1 when the outcome is broken or dishonest: a failure that was recoverable, or pages
read with nothing reported and no stated reason.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.disclosure import Labeled, content_hash, public  # noqa: E402
from app.services.profile_accounts import profile_database  # noqa: E402
from app.services.research import jobs, store  # noqa: E402
from app.services.research.textutil import grounding_form, split_sentences  # noqa: E402
from app.services.research.types import DepthMode  # noqa: E402

UNRECOVERABLE = ("unavailable", "not reachable", "not installed", "search", "cancel")


def citation_problems(data: dict) -> list[str]:
    evidence = {e["id"]: e for e in data.get("evidence") or [] if e.get("status") == "accepted"}
    sources = {s["id"]: s for s in data.get("sources") or []}
    chunks = {c["id"]: c for c in (data.get("trace") or {}).get("chunks") or []}
    problems = []
    for claim in data.get("claims") or []:
        if not claim.get("evidence_ids"):
            problems.append(f"{claim['id']}: cites nothing")
        for evidence_id in claim.get("evidence_ids") or []:
            item = evidence.get(evidence_id)
            if item is None:
                problems.append(f"{claim['id']}: {evidence_id} is not accepted evidence")
                continue
            if item["source_id"] not in sources:
                problems.append(f"{claim['id']}: {evidence_id} has no source")
            chunk = chunks.get(item.get("chunk_id"))
            # The pipeline's own normalisation (whitespace, quotes, dashes), sentence by
            # sentence: an excerpt may join sentences that are not adjacent on the page.
            text = (chunk or {}).get("text", "")
            if not text:
                continue
            excerpt = grounding_form(item.get("excerpt") or "")
            numbers = item.get("sentence_ids") or []
            sentences = split_sentences(text)
            if numbers and all(1 <= n <= len(sentences) for n in numbers):
                # Cited by number: the excerpt must be exactly those sentences of the chunk.
                rebuilt = grounding_form(" ".join(sentences[n - 1] for n in sorted(numbers)))
                ok = bool(excerpt) and rebuilt.startswith(excerpt.strip())  # stored capped
            else:
                ok = excerpt[:60] in grounding_form(text)
            if not ok:
                problems.append(f"{claim['id']}: excerpt of {evidence_id} is not on its page")
    return problems


def report(data: dict, seconds: float) -> int:
    trace = data.get("trace") or {}
    metadata = data.get("metadata") or {}
    diagnostics = trace.get("diagnostics") or {}
    print("\n" + "=" * 78)
    print(f"QUERY      {data['user_query']}  ({data['depth']})")
    print(f"STATUS     {data['status']}  completion={metadata.get('completion_status')}")
    print(f"STOPPED    {metadata.get('stop_detail') or data.get('error')}")
    print(f"DURATION   {seconds / 60:.1f} min   confidence={metadata.get('confidence')}")
    route = trace.get("model_route") or {}
    print(f"MODEL      {route.get('provider')}:{route.get('model')} "
          f"ctx={route.get('context_window')} out={route.get('max_output_tokens')}")
    claims = data.get("claims") or []
    print(f"\nFINDINGS ({len(claims)})")
    for claim in claims:
        print(f"  [{claim.get('strength') or '?'}] {claim['text']}  <- {claim.get('evidence_ids')}")
    unverified = data.get("unverified") or metadata.get("unverified") or []
    if unverified:
        print(f"\nUNVERIFIED EVIDENCE ({len(unverified)})")
        for item in unverified:
            print(f"  \"{item.get('excerpt', '')[:140]}\"  <- {item.get('evidence_id')}")
    problems = citation_problems(data)
    print(f"\nCITATIONS  {'all valid' if not problems else 'PROBLEMS'}")
    for problem in problems:
        print(f"  ! {problem}")
    evidence = data.get("evidence") or []
    rejected = Counter(e.get("rejection_reason") for e in evidence if e.get("status") == "rejected")
    accepted = sum(1 for e in evidence if e.get("status") == "accepted")
    print(f"\nEVIDENCE   accepted={accepted} rejected={sum(rejected.values())} {dict(rejected)}")
    sources = data.get("sources") or []
    print(f"SOURCES    read={sum(1 for s in sources if s.get('fetch_outcome') != 'failed')} "
          f"used={sum(1 for s in sources if s.get('used'))}")
    for source in sources:
        assessment = source.get("assessment") or {}
        print(f"  {source['host']:<32} {source.get('fetch_outcome'):<8} "
              f"role={assessment.get('role')} grade={assessment.get('provenance')} "
              f"used={source.get('used')}")
    print("\nSTAGES")
    for stage in trace.get("stages") or []:
        print(f"  {stage['stage']:<22} calls={stage['calls']:<3} retries={stage['retries']} "
              f"in={stage['prompt_tokens']:<6} out={stage['completion_tokens']:<6} "
              f"{stage['seconds']:.0f}s")
    for fallback in diagnostics.get("fallbacks") or []:
        print(f"  fallback: {fallback}")
    for note in trace.get("errors") or []:
        print(f"  note: {note}")

    status = data["status"]
    detail = f"{metadata.get('stop_detail')} {data.get('error')}".lower()
    read = sum(1 for s in sources if s.get("fetch_outcome") != "failed")
    broken = bool(problems)
    if status not in ("completed", "failed", "cancelled"):
        broken = True  # the run never reached an end
    if status == "failed" and not any(word in detail for word in UNRECOVERABLE):
        broken = True
    if status == "completed" and read and not claims and not unverified and "## Open questions" \
            not in (data.get("report") or ""):
        broken = True
    print(f"\nVERDICT    {'BROKEN' if broken else 'OK'}")
    return 1 if broken else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="account profile id")
    parser.add_argument("--depth", default="quick", choices=[d.value for d in DepthMode])
    parser.add_argument("query", nargs="+")
    args = parser.parse_args()
    query = " ".join(args.query).strip()
    with profile_database(args.profile):
        store.initialize_research_tables()
        job = jobs.create_job(
            Labeled(query, public("user_input:" + content_hash(query))),
            DepthMode(args.depth),
            memory_enabled=False,
        )
        cancel = threading.Event()
        with jobs._lock:
            jobs._active_jobs[job.id] = cancel
        # The app's own worker entry point, which opens the profile in the worker thread.
        worker = threading.Thread(
            target=jobs._run_research_pipeline_bound,
            args=(job.id, cancel, args.profile, False),
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        last = ""
        try:
            while worker.is_alive():
                worker.join(timeout=5)
                data = store.load_job(job.id) or {}
                step = (
                    f"{data.get('status')} {data.get('progress_percent')}% "
                    f"{data.get('current_step')}"
                )
                if step != last:
                    print(f"[{(time.monotonic() - started) / 60:5.1f} min] {step}", flush=True)
                    last = step
        except KeyboardInterrupt:
            jobs.cancel_job(job.id)
            cancel.set()
            worker.join(timeout=30)
        return report(store.load_job(job.id), time.monotonic() - started)


if __name__ == "__main__":
    raise SystemExit(main())
