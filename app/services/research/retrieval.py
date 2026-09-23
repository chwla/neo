"""Search, triage and fetching for research actions.

Each action searches the web, and also a narrower vertical (scholarly or news) when its target's
evidence calls for one and a provider offers it. An action following a cited record fetches the
cited page directly: only URLs that appear in pages already read or in search results are ever
fetched, never one a model wrote. A provider's response is used when it is useful, not merely
because it arrived first, so provider order never becomes a quality policy.

Triage decides what to read without a model call: how well a result matches what its action is
looking for, what this run has learnt about the site's provenance, and whether it would add an
independent source. A title naming every compared subject earns nothing: that is the shape of
comparison listicles, not of primary evidence.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Protocol

from app.services.research.textutil import (
    canonicalize_url,
    host_group,
    host_of,
    jaccard,
    mentions_any,
    name_forms,
    normalize_ws,
    token_set,
)
from app.services.research.types import (
    Action,
    FetchRecord,
    Intent,
    LeadKind,
    LeadStatus,
    OriginLead,
    Provenance,
    RecencyKind,
    RejectionReason,
    RejectionRecord,
    ResearchPlan,
    ResearchSource,
    ResearchTrace,
    SearchAttempt,
)
from app.services.search.content import UNSUPPORTED_EXTENSIONS
from app.services.search.types import FetchedPage, WebSearchResponse

QUERY_DUPLICATE_THRESHOLD = 0.8
RESULTS_PER_SEARCH = 8
#: Pages a site whose readability is unknown may have in one fetch wave.
_PROBES_PER_SITE = 1
#: Account and app screens, recognised by the first path segment or the query string only, so
#: documentation paths such as ``/docs/auth`` are still read.
_APP_CHROME = re.compile(
    r"^/(?:log-?in|sign-?in|sign-?up|sign-?out|log-?out|register|signup|account|accounts|"
    r"my-account|cart|basket|checkout|oauth|sso|subscribe)(?:/|$|\.)",
    re.IGNORECASE,
)
_SEARCH_PAGE = re.compile(r"[?&](?:q|query|search|s)=", re.IGNORECASE)
_DOI = re.compile(r"\b10\.\d{4,9}/", re.IGNORECASE)
# PDFs are read (see ``WebPageFetcher(read_documents=True)``); these formats are not.
_NON_HTML_EXTENSIONS = (*UNSUPPORTED_EXTENSIONS, ".epub", ".csv")
_CACHE_TTL_SECONDS = {
    RecencyKind.CURRENT: 6 * 3600,
    RecencyKind.AS_OF_EVENT: 24 * 3600,
    RecencyKind.TIMELESS: 72 * 3600,
}
_PROVENANCE_BONUS = {Provenance.A: 1.5, Provenance.B: 1.0, Provenance.C: 0.0, Provenance.D: -2.0}
_ORIGIN_SEEKING = frozenset({Intent.LOCATE_ORIGIN, Intent.FOLLOW_LEAD, Intent.ACCESSIBLE_COPY})


class SearchProvider(Protocol):
    name: str

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse: ...


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


class PageCache:
    """Bounded, thread-safe cache of fetched pages keyed by canonical URL."""

    def __init__(self, max_entries: int = 256, clock: Callable[[], float] = time.time) -> None:
        self._entries: OrderedDict[str, tuple[float, FetchedPage]] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries
        self._clock = clock

    def get(self, key: str, max_age_seconds: float) -> FetchedPage | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, page = entry
            if self._clock() - stored_at > max_age_seconds:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return page

    def put(self, key: str, page: FetchedPage) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), page)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)


SHARED_PAGE_CACHE = PageCache()


@dataclass
class Candidate:
    canonical_url: str
    url: str
    host: str
    host_group: str
    title: str
    snippet: str
    provider: str
    rank: int
    action_ids: list[str] = field(default_factory=list)
    target_ids: list[str] = field(default_factory=list)
    subject_ids: list[str] = field(default_factory=list)
    #: Set when the candidate is a cited record's own link, fetched without a search.
    lead_id: str | None = None
    score: float = 0.0


@dataclass
class ResultUtility:
    usable: int
    relevant: int
    unique_hosts: int
    novelty: float
    sufficient: bool


@dataclass
class FetchedSource:
    source: ResearchSource
    page: FetchedPage | None
    candidate: Candidate | None = None


@dataclass
class SearchOutcome:
    candidates: list[Candidate]
    #: Results that look like the originating record the action was after.
    leads: list[OriginLead]


class Retriever:
    def __init__(
        self,
        *,
        plan: ResearchPlan,
        max_searches: int,
        max_reads: int,
        providers: list[SearchProvider],
        fetcher: PageFetcher,
        trace: ResearchTrace,
        cache: PageCache | None,
        clock: Callable[[], float],
        has_time: Callable[[], bool],
        max_workers: int = 4,
        fetch_retries: int = 1,
    ) -> None:
        self.plan = plan
        self.max_searches = max_searches
        self.max_reads = max_reads
        self.providers = providers
        self.fetcher = fetcher
        self.trace = trace
        self.cache = cache
        self.clock = clock
        self.has_time = has_time
        self.max_workers = max(1, max_workers)
        self.fetch_retries = max(0, fetch_retries)
        self.candidates: dict[str, Candidate] = {}
        self.fetched_urls: set[str] = set()
        self.blocked_groups: set[str] = set()
        self.readable_groups: set[str] = set()
        self.searches_used = 0
        self.fetches_used = 0
        self.provider_calls = 0
        self.provider_failures = 0
        self._executed: list[tuple[set[str], str]] = []
        self._source_counter = 0
        self._lead_counter = 0

    # --- search ------------------------------------------------------------------------

    @property
    def search_unavailable(self) -> bool:
        return not self.providers or (
            self.provider_calls > 0 and self.provider_failures >= self.provider_calls
        )

    @property
    def searches_remaining(self) -> int:
        return max(0, self.max_searches - self.searches_used)

    @property
    def fetches_remaining(self) -> int:
        return max(0, self.max_reads - self.fetches_used)

    def is_duplicate_query(self, query: str, vertical: str = "web") -> bool:
        wanted = token_set(query)
        return any(
            kind == vertical and jaccard(wanted, previous) >= QUERY_DUPLICATE_THRESHOLD
            for previous, kind in self._executed
        )

    def run_actions(self, actions: list[Action], leads: dict[str, OriginLead]) -> SearchOutcome:
        """Search for, or directly locate, what each action is after."""
        discovered: dict[str, Candidate] = {}
        searches: list[tuple[Action, str, int]] = []
        #: Actions whose query another action in this same step is already running.
        waiting: list[Action] = []
        queued: list[tuple[set[str], str]] = []
        for action in actions:
            lead = leads.get(action.lead_id or "")
            if action.intent == Intent.FOLLOW_LEAD and lead is not None and lead.href:
                candidate = self._lead_candidate(action, lead)
                if candidate is not None:
                    discovered[candidate.canonical_url] = candidate
                continue
            verticals = ["web"] + ([action.vertical] if action.vertical != "web" else [])
            for vertical in verticals:
                if not action.query:
                    continue
                if not any(_offers(provider, vertical) for provider in self.providers):
                    continue
                wanted = token_set(action.query)
                if any(
                    kind == vertical and jaccard(wanted, previous) >= QUERY_DUPLICATE_THRESHOLD
                    for previous, kind in queued
                ):
                    # Another action in this step is already asking it; share its results.
                    self._attempt(action, vertical, "skipped_duplicate")
                    waiting.append(action)
                    continue
                repeat = self.is_duplicate_query(action.query, vertical)
                if repeat:
                    # Asking the same thing twice returns the same pages. What the action is
                    # after is a page it has not read yet: first the results already in hand.
                    recalled = self._recall(action)
                    for candidate in recalled:
                        discovered.setdefault(candidate.canonical_url, candidate)
                    if recalled:
                        self._attempt(action, vertical, "skipped_duplicate")
                        continue
                if self.searches_remaining <= 0 or not self.has_time():
                    self._attempt(action, vertical, "skipped_budget")
                    continue
                self._executed.append((wanted, vertical))
                queued.append((wanted, vertical))
                self.searches_used += 1
                # Nothing unread is left from the same query, so ask it for deeper results.
                searches.append((action, vertical, RESULTS_PER_SEARCH * (2 if repeat else 1)))

        # Searches wait on the network, so they run together; merging stays in action order.
        responses: dict[int, list[tuple[str, WebSearchResponse | Exception]]] = {}
        if searches:
            workers = max(1, min(len(searches), self.max_workers))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._provider_chain, action, vertical, limit): index
                    for index, (action, vertical, limit) in enumerate(searches)
                }
                for future in as_completed(futures):
                    responses[futures[future]] = future.result()

        found_leads: list[OriginLead] = []
        for index, (action, vertical, _limit) in enumerate(searches):
            for provider_name, response in responses.get(index, []):
                if isinstance(response, Exception):
                    self.provider_failures += 1
                    self._attempt(action, vertical, "error", provider_name, error=str(response))
                    continue
                if not response.results:
                    if response.error:
                        self.provider_failures += 1
                    self._attempt(
                        action,
                        vertical,
                        "error" if response.error else "empty",
                        provider_name,
                        error=response.error,
                    )
                    continue
                utility = self.result_utility(response.results, action)
                self._attempt(
                    action,
                    vertical,
                    "useful" if utility.sufficient else "weak",
                    provider_name,
                    result_count=len(response.results),
                    utility=utility,
                )
                for result in response.results:
                    candidate = self._remember(result, action, provider_name)
                    if candidate is None:
                        continue
                    if candidate.canonical_url not in self.fetched_urls:
                        discovered[candidate.canonical_url] = candidate
                    lead = self._result_lead(result, action, provider_name, vertical)
                    if lead is not None:
                        found_leads.append(lead)
        for action in waiting:
            for candidate in self._recall(action):
                discovered.setdefault(candidate.canonical_url, candidate)
        return SearchOutcome(list(discovered.values()), found_leads)

    def _recall(self, action: Action) -> list[Candidate]:
        """Results already seen that suit this action and have not been read yet.

        These pages were returned for a query equivalent to this one, so what makes them
        suitable is that they are about the action's subjects; asking them to repeat the
        query's own words as well would rule out most of what the search just found.
        """
        subject_forms = self._subject_forms(action.subject_ids)
        target_tokens = self._target_tokens(action)
        recalled: list[Candidate] = []
        for candidate in self.candidates.values():
            if candidate.canonical_url in self.fetched_urls:
                continue
            text = f"{candidate.title} {candidate.snippet} {candidate.url}"
            about_subjects = mentions_any(text, subject_forms) if subject_forms else False
            if not about_subjects and not self._looks_relevant(text, subject_forms, target_tokens):
                continue
            _add(candidate.action_ids, action.id)
            _add(candidate.target_ids, action.target_id)
            for subject_id in action.subject_ids:
                _add(candidate.subject_ids, subject_id)
            recalled.append(candidate)
        return recalled

    def _provider_chain(
        self, action: Action, vertical: str, limit: int = RESULTS_PER_SEARCH
    ) -> list[tuple[str, WebSearchResponse | Exception]]:
        """Ask providers in order until one gives a useful answer. Robustness, not extra budget."""
        answers: list[tuple[str, WebSearchResponse | Exception]] = []
        for provider in self.providers:
            if not _offers(provider, vertical):
                continue
            if self.provider_calls >= self.max_searches * 2:
                break
            self.provider_calls += 1
            try:
                if vertical == "web":
                    response = provider.search(action.query, limit, action.time_filter)
                else:
                    response = provider.search(action.query, limit, action.time_filter, vertical)
            except Exception as exc:  # one provider failing must not end the action
                answers.append((provider.name, exc))
                continue
            answers.append((provider.name, response))
            if response.results and self.result_utility(response.results, action).sufficient:
                break
        return answers

    def result_utility(self, results, action: Action) -> ResultUtility:
        subject_forms = self._subject_forms(action.subject_ids)
        target_tokens = self._target_tokens(action)
        usable = [result for result in results if self._usable_url(result.url) is None]
        relevant = [
            result
            for result in usable
            if self._looks_relevant(
                f"{result.title} {result.snippet or ''} {result.url}", subject_forms, target_tokens
            )
        ]
        hosts = {host_group(host_of(result.url)) for result in relevant}
        novel = [
            result
            for result in relevant
            if (canonicalize_url(result.url) or "") not in self.candidates
        ]
        novelty = len(novel) / len(relevant) if relevant else 0.0
        needed = min(3, max(1, len(results) // 2))
        return ResultUtility(
            usable=len(usable),
            relevant=len(relevant),
            unique_hosts=len(hosts),
            novelty=round(novelty, 3),
            sufficient=len(relevant) >= needed and len(hosts) >= 2 and novelty >= 0.25,
        )

    # --- triage ------------------------------------------------------------------------

    def triage(
        self,
        candidates: list[Candidate],
        actions: list[Action],
        leads: dict[str, OriginLead],
        sources: list[ResearchSource],
        limit: int,
    ) -> list[Candidate]:
        """Choose what to read, spreading reads across the step's targets."""
        if limit <= 0:
            return []
        by_action = {action.id: action for action in actions}
        host_grade = _best_grade_by_host(sources)
        read_groups = {source.host_group for source in sources if source.reviewed}
        for candidate in candidates:
            candidate.score = self._score(candidate, by_action, leads, host_grade, read_groups)
        queues: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in sorted(candidates, key=lambda c: (-c.score, c.rank)):
            queues[candidate.target_ids[0] if candidate.target_ids else ""].append(candidate)
        chosen: list[Candidate] = []
        per_site: dict[tuple[str, str], int] = {}
        order = list(queues)
        while len(chosen) < limit and any(queues.values()):
            for key in order:
                queue = queues[key]
                while queue:
                    candidate = queue.pop(0)
                    site = (key, candidate.host_group)
                    if per_site.get(site, 0) >= 2:
                        continue
                    per_site[site] = per_site.get(site, 0) + 1
                    chosen.append(candidate)
                    break
                if len(chosen) >= limit:
                    break
        return chosen

    def _score(
        self,
        candidate: Candidate,
        by_action: dict[str, Action],
        leads: dict[str, OriginLead],
        host_grade: dict[str, Provenance],
        read_groups: set[str],
    ) -> float:
        actions = [by_action[a] for a in candidate.action_ids if a in by_action]
        intents = {action.intent for action in actions}
        text = f"{candidate.title} {candidate.snippet}"
        score = 0.0
        if candidate.lead_id is not None:
            score += 5.0  # the evidence itself cited this page
        if intents & _ORIGIN_SEEKING:
            if _document_like(candidate.url, candidate.snippet):
                score += 2.0
            wanted: set[str] = set()
            for action in actions:
                lead = leads.get(action.lead_id or "")
                if lead is not None:
                    wanted |= token_set(f"{lead.title} {lead.issuer}")
                target = self.plan.target(action.target_id)
                if target is not None and target.profile.record_form.value != "other":
                    wanted |= token_set(target.profile.record_form.value.replace("_", " "))
            if wanted and jaccard(token_set(text), wanted) >= 0.3:
                score += 1.5
        target_tokens: set[str] = set()
        for action in actions:
            target_tokens |= self._target_tokens(action)
        score += 0.6 * min(3, len(target_tokens & token_set(text)))
        forms = self._subject_forms(candidate.subject_ids)
        if forms:
            score += 1.0 if mentions_any(f"{text} {candidate.url}", forms) else -2.0
        score += _PROVENANCE_BONUS.get(host_grade.get(candidate.host_group), 0.0)
        if candidate.host_group not in read_groups:
            score += 0.5
        score += 0.5 / max(1, candidate.rank)
        if not candidate.snippet and candidate.lead_id is None:
            score -= 0.5
        return round(score, 3)

    # --- fetching ----------------------------------------------------------------------

    def fetch(self, ranked: list[Candidate], limit: int) -> list[FetchedSource]:
        """Read up to ``limit`` pages from a ranked list, probing each untested site first.

        One page per site whose readability is unknown goes out in the first wave; sites
        that refuse access are skipped afterwards, so a blocked site cannot absorb the
        step's page budget and the remaining reads go to the next-best candidates.
        """
        outcomes: list[FetchedSource] = []
        queue: list[Candidate] = []
        for candidate in ranked:
            if candidate.canonical_url in self.fetched_urls:
                continue
            cached = (
                self.cache.get(candidate.canonical_url, self._ttl(candidate))
                if self.cache
                else None
            )
            if cached is not None:
                self.fetched_urls.add(candidate.canonical_url)
                outcomes.append(self._source_from(candidate, cached, cached=True))
            else:
                queue.append(candidate)
        remaining = min(limit, self.fetches_remaining)
        while remaining > 0 and queue and self.has_time():
            wave: list[Candidate] = []
            deferred: list[Candidate] = []
            probing: dict[str, int] = {}
            for candidate in queue:
                if candidate.host_group in self.blocked_groups:
                    self.trace.fetches.append(
                        FetchRecord(
                            url=candidate.url,
                            outcome="skipped",
                            error="site refused access earlier in this run",
                        )
                    )
                    continue
                untested = candidate.host_group not in self.readable_groups
                probes = probing.get(candidate.host_group, 0)
                if len(wave) < remaining and not (untested and probes >= _PROBES_PER_SITE):
                    wave.append(candidate)
                    if untested:
                        probing[candidate.host_group] = probes + 1
                else:
                    deferred.append(candidate)
            if not wave:
                break
            outcomes.extend(self._fetch_batch(wave))
            remaining -= len(wave)
            queue = deferred
        return outcomes

    def _ttl(self, candidate: Candidate) -> float:
        kinds = [
            target.profile.recency.kind
            for target_id in candidate.target_ids
            if (target := self.plan.target(target_id)) is not None
        ]
        return min((_CACHE_TTL_SECONDS[kind] for kind in kinds), default=24 * 3600)

    def _fetch_batch(self, batch: list[Candidate]) -> list[FetchedSource]:
        for candidate in batch:
            self.fetched_urls.add(candidate.canonical_url)
        self.fetches_used += len(batch)
        pages: dict[str, FetchedPage] = {}
        with ThreadPoolExecutor(max_workers=min(len(batch), self.max_workers)) as pool:
            futures = {pool.submit(self._fetch_one, item.url): item for item in batch}
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    page = future.result()
                except Exception as exc:  # a broken page must not break the run
                    page = FetchedPage(
                        url=candidate.url, domain=candidate.host, error=f"Fetch failed: {exc}"
                    )
                if self.cache and page.fetched and page.text:
                    self.cache.put(candidate.canonical_url, page)
                pages[candidate.canonical_url] = page
        # Fetches finish in any order; sources are numbered in the order they were ranked.
        return [
            self._source_from(candidate, pages[candidate.canonical_url], cached=False)
            for candidate in batch
        ]

    def _fetch_one(self, url: str) -> FetchedPage:
        page = self.fetcher.fetch(url)
        attempts = 0
        while (
            attempts < self.fetch_retries
            and not page.fetched
            and _retryable(page)
            and self.has_time()
        ):
            attempts += 1
            page = self.fetcher.fetch(url)
        return page

    def _source_from(
        self, candidate: Candidate, page: FetchedPage, *, cached: bool
    ) -> FetchedSource:
        self._source_counter += 1
        source_id = f"s{self._source_counter}"
        final_url = canonicalize_url(page.url) or candidate.canonical_url
        host = host_of(page.url) or candidate.host
        ok = bool(page.fetched and page.text)
        status = page.http_status or _status_from_error(page.error)
        source = ResearchSource(
            id=source_id,
            url=page.url or candidate.url,
            canonical_url=final_url,
            host=host,
            host_group=host_group(host),
            title=(page.title if page.title and page.title != page.url else candidate.title)[:300],
            snippet=candidate.snippet[:500],
            site_name=page.site_name or "",
            organization_names=page.organization_names,
            schema_types=page.schema_types,
            scholarly=page.scholarly,
            published_date=page.published_date,
            modified_date=page.modified_date,
            author=page.author or "",
            provider=candidate.provider,
            search_rank=candidate.rank,
            discovered_by_task_ids=[
                *candidate.action_ids,
                *([candidate.lead_id] if candidate.lead_id else []),
            ],
            fetch_outcome=("cached" if cached else "fetched") if ok else "failed",
            fetch_error=None if ok else (page.error or "No readable content"),
            http_status=status,
            document_kind=page.document_kind,
            links=page.links,
            citation_meta=page.citation_meta,
        )
        if page.canonical_url:
            canonical = canonicalize_url(page.canonical_url)
            source.declared_canonical = canonical
            if canonical and host_group(host_of(canonical)) != source.host_group:
                source.class_signals.append(f"canonical_elsewhere:{host_of(canonical)}")
        if not ok:
            reason = (
                RejectionReason.UNSUPPORTED_FORMAT
                if "content type" in (page.error or "").lower()
                else RejectionReason.FETCH_FAILURE
            )
            source.rejection_reason = reason
            if status in (401, 403, 429, 451):
                self.blocked_groups.add(source.host_group)
            self.trace.rejections.append(
                RejectionRecord(
                    stage="fetch", ref=source.url, reason=reason, detail=source.fetch_error or ""
                )
            )
        elif final_url != candidate.canonical_url and final_url in self.fetched_urls:
            source.rejection_reason = RejectionReason.DUPLICATE
            self.trace.rejections.append(
                RejectionRecord(
                    stage="dedup",
                    ref=source.url,
                    reason=RejectionReason.DUPLICATE,
                    detail="redirect target already fetched",
                )
            )
        self.fetched_urls.add(final_url)
        if ok:
            self.readable_groups.add(source.host_group)
        self.trace.fetches.append(
            FetchRecord(
                source_id=source_id,
                url=source.url,
                outcome=source.fetch_outcome,
                http_status=status,
                error=source.fetch_error,
            )
        )
        return FetchedSource(source=source, page=page if ok else None, candidate=candidate)

    # --- helpers -----------------------------------------------------------------------

    def _lead_candidate(self, action: Action, lead: OriginLead) -> Candidate | None:
        canonical = canonicalize_url(lead.href or "")
        if canonical is None or self._usable_url(lead.href or "") is not None:
            lead.status = LeadStatus.REJECTED
            return None
        if canonical in self.fetched_urls:
            return None
        host = host_of(lead.href or "")
        candidate = self.candidates.get(canonical) or Candidate(
            canonical_url=canonical,
            url=lead.href or "",
            host=host,
            host_group=host_group(host),
            title=lead.title,
            snippet="",
            provider="citation",
            rank=0,
        )
        self.candidates[canonical] = candidate
        candidate.lead_id = lead.id
        _add(candidate.action_ids, action.id)
        _add(candidate.target_ids, action.target_id)
        for subject_id in action.subject_ids:
            _add(candidate.subject_ids, subject_id)
        return candidate

    def _remember(self, result, action: Action, provider: str) -> Candidate | None:
        canonical = canonicalize_url(result.url)
        if canonical is None:
            return None
        rejection = self._usable_url(result.url)
        if rejection is not None:
            if canonical not in self.candidates:
                self.trace.rejections.append(
                    RejectionRecord(stage="search", ref=result.url, reason=rejection)
                )
                self.candidates[canonical] = Candidate(
                    canonical_url=canonical,
                    url=result.url,
                    host=host_of(result.url),
                    host_group=host_group(host_of(result.url)),
                    title=result.title or "",
                    snippet=result.snippet or "",
                    provider=provider,
                    rank=result.rank,
                )
                self.fetched_urls.add(canonical)
            return None
        host = host_of(result.url)
        existing = self.candidates.get(canonical)
        if existing is None:
            existing = Candidate(
                canonical_url=canonical,
                url=result.url,
                host=host,
                host_group=host_group(host),
                title=result.title or "",
                snippet=result.snippet or "",
                provider=provider,
                rank=result.rank,
            )
            self.candidates[canonical] = existing
        existing.rank = min(existing.rank, result.rank)
        if not existing.snippet and result.snippet:
            existing.snippet = result.snippet
        _add(existing.action_ids, action.id)
        _add(existing.target_ids, action.target_id)
        for subject_id in action.subject_ids:
            _add(existing.subject_ids, subject_id)
        return existing

    #: Citations one page is allowed to contribute, so a long bibliography cannot flood a run.
    LEADS_PER_PAGE = 3

    def reference_leads(self, item: FetchedSource) -> list[OriginLead]:
        """The records a page points its reader at, taken from the page's own links.

        A page that cites its source is telling the run where the evidence came from, and that
        is worth following whether or not the judge happened to mention it. What is captured is
        a lead, never a confirmation: the page saying so does not make it so.
        """
        source, candidate = item.source, item.candidate
        if item.page is None or source.rejection_reason is not None or candidate is None:
            return []
        blocks = item.page.blocks
        leads: list[OriginLead] = []
        for link in source.links:
            if len(leads) >= self.LEADS_PER_PAGE:
                break
            href = canonicalize_url(link.href)
            title = normalize_ws(link.text)
            if href is None or self._usable_url(link.href) is not None:
                continue
            if host_group(host_of(href)) == source.host_group:
                continue  # a page pointing at itself is not a citation
            # A citation is a link a page offers as a record in its own right: an entry in a
            # list of them, or a link to a document. A link inside a sentence is not one.
            listed = (
                link.block_index is not None
                and 0 <= link.block_index < len(blocks)
                and blocks[link.block_index].kind == "list_item"
            )
            if not (listed or _document_like(href, title)) or len(token_set(title)) < 3:
                continue
            for target_id in candidate.target_ids[:2]:
                self._lead_counter += 1
                leads.append(
                    OriginLead(
                        id=f"lead-c{self._lead_counter}",
                        target_id=target_id,
                        kind=LeadKind.CITATION,
                        from_source_id=source.id,
                        independent_of=source.independence_key,
                        title=title[:300],
                        href=href,
                    )
                )
        return leads

    def _result_lead(
        self, result, action: Action, provider: str, vertical: str
    ) -> OriginLead | None:
        """A result that itself looks like the record the action is after.

        It becomes a lead, not a confirmation: a search engine ranking a page says nothing
        about who published it, so these leads never raise a source's provenance.
        """
        if action.intent not in (Intent.LOCATE_ORIGIN, Intent.DIRECT_ANSWER):
            return None
        if not (_document_like(result.url, result.snippet or "") or vertical == "scholarly"):
            return None
        forms = self._subject_forms(action.subject_ids)
        text = f"{result.title} {result.snippet or ''}"
        if forms and not mentions_any(text, forms):
            return None
        self._lead_counter += 1
        return OriginLead(
            id=f"lead-r{self._lead_counter}",
            target_id=action.target_id,
            kind=LeadKind.SEARCH_RESULT,
            from_action_id=action.id,
            independent_of=f"search:{provider}",
            title=(result.title or "")[:300],
            href=result.url,
        )

    @staticmethod
    def _usable_url(url: str) -> RejectionReason | None:
        canonical = canonicalize_url(url)
        if canonical is None:
            return RejectionReason.UNSUPPORTED_FORMAT
        base, _, query = canonical.partition("?")
        path = base.lower()
        if path.endswith(_NON_HTML_EXTENSIONS):
            return RejectionReason.UNSUPPORTED_FORMAT
        after_host = path.split("/", 3)[3] if path.count("/") >= 3 else ""
        if _APP_CHROME.search("/" + after_host) or (query and _SEARCH_PAGE.search("?" + query)):
            return RejectionReason.APP_CHROME
        return None

    def _subject_forms(self, subject_ids: list[str]) -> set[str]:
        forms: set[str] = set()
        for subject_id in subject_ids:
            entity = self.plan.entity(subject_id)
            if entity:
                forms |= name_forms(entity.names())
        return forms

    def _target_tokens(self, action: Action) -> set[str]:
        target = self.plan.target(action.target_id)
        wanted = token_set(action.query)
        if target is not None:
            wanted |= token_set(target.text)
            dimension = self.plan.dimension(target.dimension_id)
            if dimension:
                wanted |= token_set(dimension.name)
        for subject_id in action.subject_ids:
            entity = self.plan.entity(subject_id)
            if entity:
                for name in entity.names():
                    wanted -= token_set(name)
        return wanted

    @staticmethod
    def _looks_relevant(text: str, subject_forms: set[str], target_tokens: set[str]) -> bool:
        overlap = len(target_tokens & token_set(text))
        if subject_forms:
            return mentions_any(text, subject_forms) and (overlap >= 1 or not target_tokens)
        return overlap >= min(2, max(1, len(target_tokens)))

    def _attempt(
        self,
        action: Action,
        vertical: str,
        status: str,
        provider: str = "",
        *,
        error: str | None = None,
        result_count: int = 0,
        utility: ResultUtility | None = None,
    ) -> None:
        self.trace.search_attempts.append(
            SearchAttempt(
                task_id=action.id,
                query=action.query,
                round=action.step,
                strategy=action.intent.value,
                vertical=vertical,
                provider=provider,
                status=status,  # type: ignore[arg-type]
                result_count=result_count,
                usable=utility.usable if utility else 0,
                relevant=utility.relevant if utility else 0,
                unique_hosts=utility.unique_hosts if utility else 0,
                novelty=utility.novelty if utility else 0.0,
                error=(error or None) and str(error)[:240],
            )
        )


def _offers(provider: SearchProvider, vertical: str) -> bool:
    return vertical == "web" or vertical in getattr(provider, "verticals", frozenset({"web"}))


def _document_like(url: str, snippet: str) -> bool:
    path = (canonicalize_url(url) or url).split("?", 1)[0].lower()
    return path.endswith(".pdf") or bool(_DOI.search(url)) or bool(_DOI.search(snippet or ""))


def _best_grade_by_host(sources: list[ResearchSource]) -> dict[str, Provenance]:
    order = [Provenance.D, Provenance.C, Provenance.B, Provenance.A]
    best: dict[str, Provenance] = {}
    for source in sources:
        if source.assessment is None:
            continue
        grade = source.assessment.provenance
        current = best.get(source.host_group)
        if current is None or order.index(grade) > order.index(current):
            best[source.host_group] = grade
    return best


def _add(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _retryable(page: FetchedPage) -> bool:
    error = (page.error or "").lower()
    status = page.http_status or _status_from_error(page.error)
    return "timed out" in error or (status is not None and status >= 500)


def _status_from_error(error: str | None) -> int | None:
    match = re.search(r"\b([45]\d{2})\b", error or "")
    return int(match.group(1)) if match else None
