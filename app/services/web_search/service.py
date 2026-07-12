from __future__ import annotations

# ruff: noqa: E501
import re
from urllib.parse import urlparse, urlunparse

from app.services.search import WebSearchService
from app.services.web_search import store
from app.services.web_search.planner import plan
from app.services.web_search.redaction import safe


class ReliableWebSearchService:
    MAX_FETCH_CHARS = 120_000
    def __init__(self, search: WebSearchService | None = None) -> None:
        store.initialize_web_search_tables()
        self.search = search or WebSearchService()

    def plan(self, query: str, mode: str = "research", freshness_required: bool = False) -> dict:
        return plan(query, mode, freshness_required)

    def run(self, request) -> dict:
        search_plan = self.plan(request.query, request.mode, request.freshness_required)
        response = self.search.search(request.query, request.max_sources)
        run = store.create_run(request.query, request.mode, search_plan, {"provider": response.provider})
        if response.error or response.provider == "disabled":
            final = store.update_run(run["id"], status="degraded", error=safe(response.error or "Web search is disabled."), summary_text="Search is unavailable; no unsupported facts were produced.", completed_at=store.now())
            return {**(final or {}), "sources": [], "evidence": [], "conflicts": []}
        seen, sources = set(), []
        for result in response.results[: request.max_sources]:
            url = self.canonical(result.url)
            if not url or url in seen:
                continue
            seen.add(url)
            domain = urlparse(url).netloc.lower()
            official = self._official(domain)
            text = f"{result.title} {result.snippet or ''}".lower()
            breakdown = self._score(request.query, text, domain, official, request.freshness_required)
            source = store.add_source(run["id"], safe({"url": url, "canonical_url": url, "title": result.title, "domain": domain, "snippet": result.snippet or "", "fetched_text": None, "fetched_at": None, "source_type": "official" if official else "web", "credibility_score": breakdown["credibility"], "freshness_score": breakdown["freshness"], "relevance_score": breakdown["relevance"], "final_score": breakdown["final"], "metadata": {"provider": response.provider, "score_breakdown": breakdown}, "redaction_summary": {}}))
            source = store.update_source(
                source["id"],
                credibility_score=breakdown["credibility"], freshness_score=breakdown["freshness"],
                relevance_score=breakdown["relevance"], final_score=breakdown["final"],
                metadata_json=store.json_text({"provider": response.provider, "score_breakdown": breakdown}),
            ) or source
            source = self._hydrate_source(source, request.fetch_sources)
            store.upsert_cache({"canonical_url": source["canonical_url"], "title": source["title"], "domain": source["domain"], "fetched_text": source.get("fetched_text") or source["snippet"], "metadata": source.get("metadata", {}), "redaction_summary": source.get("redaction_summary", {})})
            sources.append(source)
        evidence = []
        for index, source in enumerate(sources, 1):
            text = source.get("fetched_text") or source.get("snippet") or source.get("title") or ""
            if text:
                evidence.append(store.add_evidence(run["id"], source["id"], {"claim": text[:240], "evidence_text": text[:600], "citation_label": f"[{index}]", "confidence": source["final_score"], "metadata": {"url": source["canonical_url"]}}))
        conflicts = self._conflicts(run["id"], sources) if request.include_conflict_detection else []
        summary = f"Collected {len(sources)} deduplicated source(s) and {len(evidence)} citation-ready evidence item(s)."
        final = store.update_run(run["id"], status="completed", summary_text=summary, completed_at=store.now())
        self._memory(run["id"], sources, evidence, conflicts)
        return {**(final or {}), "sources": sources, "evidence": evidence, "conflicts": conflicts}

    @staticmethod
    def canonical(url: str) -> str:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return ""
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        except ValueError:
            return ""

    @staticmethod
    def _official(domain: str) -> bool:
        return domain.endswith((".gov", ".edu", ".org")) or domain.startswith("docs.") or domain in {"openai.com", "platform.openai.com", "developer.mozilla.org"}

    @staticmethod
    def _score(query: str, text: str, domain: str, official: bool, fresh: bool) -> dict:
        terms = [term for term in re.findall(r"[a-z0-9]+", query.lower()) if len(term) > 2]
        relevance = min(0.55, round(0.11 * sum(term in text for term in terms), 3))
        credibility = 0.82 if official else 0.48
        freshness = 0.75 if fresh and re.search(r"\b20\d{2}\b|latest|current|today", text) else (0.55 if fresh else 0.5)
        technical = bool(re.search(r"\b(api|sdk|library|docs?|reference|python|javascript|typescript)\b", query, re.I))
        technical_boost = 0.12 if technical and (domain.startswith("docs.") or "developer" in domain or "reference" in text) else 0.0
        official_boost = 0.12 if official else 0.0
        diversity = 0.05
        penalties = 0.10 if any(marker in domain for marker in ("spam", "clickbait")) else 0.0
        final = max(0.0, min(1.0, round(relevance + credibility * .28 + freshness * .16 + technical_boost + official_boost + diversity - penalties, 3)))
        return {"relevance": relevance, "credibility": credibility, "freshness": freshness, "official_source": official_boost, "domain_diversity": diversity, "technical_doc_boost": technical_boost, "penalties": penalties, "final": final}

    def _hydrate_source(self, source: dict, fetch_sources: bool) -> dict:
        cached = store.get_cache(source["canonical_url"])
        if cached and cached.get("fetched_text"):
            return store.update_source(source["id"], fetched_text=cached["fetched_text"][: self.MAX_FETCH_CHARS], fetched_at=cached.get("fetched_at"), metadata_json=store.json_text({**source.get("metadata", {}), "cache_reused": True})) or source
        if not fetch_sources:
            return source
        try:
            page = self.search.fetch(source["canonical_url"])
            text = self._readable_text(getattr(page, "text", ""))
            if getattr(page, "fetched", False) and text:
                return store.update_source(source["id"], fetched_text=safe(text[: self.MAX_FETCH_CHARS]), fetched_at=store.now()) or source
            return store.update_source(source["id"], metadata_json=store.json_text({**source.get("metadata", {}), "fetch_failure": "No readable content returned."})) or source
        except Exception as exc:
            return store.update_source(source["id"], metadata_json=store.json_text({**source.get("metadata", {}), "fetch_failure": safe(str(exc))[:240]})) or source

    @staticmethod
    def _readable_text(text: str) -> str:
        text = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", text or "", flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def detail(self, run_id: str) -> dict:
        run = store.get_run(run_id)
        if not run:
            raise LookupError("Web search run not found.")
        return {**run, "sources": store.related("workspace_web_sources", run_id), "evidence": store.related("workspace_web_evidence", run_id), "conflicts": store.related("workspace_web_conflicts", run_id)}

    @staticmethod
    def _conflicts(run_id: str, sources: list[dict]) -> list[dict]:
        found: list[dict] = []
        versions = {}
        for source in sources:
            match = re.search(r"\b\d+(?:\.\d+){1,3}\b", f"{source.get('title','')} {source.get('snippet','')}")
            if match:
                versions.setdefault(match.group(0), []).append(source)
        if len(versions) > 1:
            first, second = list(versions.items())[:2]
            found.append(store.add_conflict(run_id, {"topic": "version", "claim_a": first[0], "claim_b": second[0], "source_ids": [item["id"] for item in first[1] + second[1]], "severity": "medium", "metadata": {"reason": "sources mention different version numbers"}}))
        corpus = [(source, f"{source.get('title','')} {source.get('snippet','')} {source.get('fetched_text','')}") for source in sources]
        for topic, pattern in (("date", r"\b20\d{2}-\d{2}-\d{2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}\b"), ("price", r"(?:\$|€|£)\s?\d+(?:\.\d{2})?")):
            values = {}
            for source, text in corpus:
                match = re.search(pattern, text, re.I)
                if match:
                    values.setdefault(match.group(0), []).append(source)
            if len(values) > 1:
                first, second = list(values.items())[:2]
                found.append(store.add_conflict(run_id, {"topic": topic, "claim_a": first[0], "claim_b": second[0], "source_ids": [item["id"] for item in first[1] + second[1]], "severity": "medium", "metadata": {"reason": f"sources mention different {topic} values"}}))
        return found

    @staticmethod
    def _memory(run_id: str, sources: list[dict], evidence: list[dict], conflicts: list[dict]) -> None:
        try:
            from app.services.memory_retrieval import MemoryRetrievalService
            indexer = MemoryRetrievalService().indexer
            for item in evidence[:8]:
                indexer.index_record(scope_type="research_run", scope_id=run_id, source_type="web_search", source_id=item["id"], title=f"Web finding {item['citation_label']}", content=item["evidence_text"], memory_type="research_finding", tags=["web_search", "research_finding"])
                indexer.index_record(scope_type="research_run", scope_id=run_id, source_type="web_search", source_id=f"technical:{item['id']}", title=f"Technical fact {item['citation_label']}", content=item["evidence_text"], memory_type="technical_fact", tags=["web_search", "technical_fact"])
            for source in sources[:8]:
                indexer.index_record(scope_type="research_run", scope_id=run_id, source_type="web_search", source_id=f"quality:{source['id']}", title=f"Source quality: {source['title']}", content={"score": source.get("final_score"), "url": source.get("canonical_url")}, memory_type="source_quality_note", tags=["web_search", "source_quality_note"])
            for conflict in conflicts:
                indexer.index_record(scope_type="research_run", scope_id=run_id, source_type="web_search", source_id=f"conflict:{conflict['id']}", title=f"Open conflict: {conflict['topic']}", content=conflict, memory_type="conflict", tags=["web_search", "conflict"])
        except Exception:
            pass
