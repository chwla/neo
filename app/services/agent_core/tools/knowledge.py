"""Tools that bring outside knowledge in: web, code intelligence, memory.

Each one adapts a service Neo already runs. None of them re-implements search,
indexing or recall -- they exist so the model can *reach* those services on its
own initiative rather than having their output pasted into a prompt by a
hardcoded pipeline step, which is what the old runner did.
"""

from __future__ import annotations

from app.services.agent_core.approvals import arguments_hash
from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.disclosure import Labeled
from app.services.disclosure.gate import ApprovedCall, GrantStore

_LIMIT = 9000


#: How long one approved call's grant lasts. Long enough for the call's own
#: requests, short enough that nothing later in the run can reach it.
_APPROVED_CALL_SECONDS = 120


def _approved_grants(
    context: ToolContext, tool_name: str, arguments: dict, *, destinations: frozenset[str]
) -> GrantStore | None:
    """Grants for this exact approved call, or ``None``.

    Only an :class:`ApprovedCall` the loop placed in the server-set context counts,
    and only for this session, this tool and these exact arguments. The grants it
    yields live in a store created here for this call alone and dropped with it; a
    later call in the same run starts without them.
    """

    approved = context.extras.get("approved_call")
    if not isinstance(approved, ApprovedCall):
        return None
    if approved.session_id != context.session_id or approved.tool_name != tool_name:
        return None
    if approved.arguments_hash != arguments_hash(arguments):
        return None
    store = GrantStore.in_memory()
    store.create_scoped(
        approval_id=approved.approval_id,
        tags=approved.tags,
        # "search" is the embedded SearXNG dispatch; "http" every other request.
        sinks=frozenset({"http", "search"}),
        destinations=destinations,
        ttl_seconds=_APPROVED_CALL_SECONDS,
        covers_unknown=approved.covers_unknown,
    )
    return store


def _approved_search(query: str, label, grants: GrantStore) -> str:
    """Send the approved query verbatim, and report what the providers returned.

    Narrow on purpose. The full search path rewrites the query with a model and
    chooses result pages to read; both are payloads derived from the query that the
    user did not see when they approved it. Here the only thing sent is the query
    they approved, and reading any particular result is a separate ``web_fetch``
    that asks again.
    """

    from app.services.search.providers import ProviderRegistry

    errors: list[str] = []
    for provider in ProviderRegistry(source_label=label, grants=grants).chain():
        response = provider.search(query, 8)
        if response.results:
            lines = [
                f"- {item.title}\n  {item.url}\n  {item.snippet}".rstrip()
                for item in response.results[:8]
            ]
            return (
                f"Search results for the approved query (from {provider.name}). "
                "Pages were not opened; fetch one to read it.\n" + "\n".join(lines)
            )[:_LIMIT]
        if response.error:
            errors.append(f"{provider.name}: {response.error}")
    detail = "; ".join(errors) or "no results"
    return f"No results for the approved query ({detail})."


def web_search(arguments: dict, _context: ToolContext) -> str:
    from app.services.web_search import WebSearchService

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("A search query is required.")
    if not _context.source_label.is_public():
        grants = _approved_grants(
            _context, "web_search", arguments, destinations=frozenset({"web-search"})
        )
        if grants is not None:
            try:
                return _approved_search(query, _context.source_label, grants)
            finally:
                grants.revoke_all()
    result = WebSearchService().build_context_forced(Labeled(query, _context.source_label))
    if result.context_text:
        return result.context_text[:_LIMIT]
    warning = result.warning or (result.search.error if result.search else None)
    return f"No usable web evidence was found. {warning or ''}".strip()


def web_fetch(arguments: dict, _context: ToolContext) -> str:
    from app.services.search.content import WebPageFetcher

    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError("A URL is required.")
    # WebPageFetcher carries Neo's SSRF guards, including the DNS-rebinding fix
    # from the readiness review; fetching with plain requests would bypass them.
    grants = None
    if not _context.source_label.is_public():
        grants = _approved_grants(
            _context, "web_fetch", arguments, destinations=frozenset({"web-fetch"})
        )
    try:
        page = WebPageFetcher(grants=grants).fetch(Labeled(url, _context.source_label))
    finally:
        if grants is not None:
            grants.revoke_all()
    if page is None or not page.fetched or not page.text:
        reason = getattr(page, "error", None) or "no readable content"
        return f"Could not retrieve {url}: {reason}."
    return f"{page.title or url}\n\n{page.text[:_LIMIT]}"


def find_symbol(arguments: dict, context: ToolContext) -> str:
    from app.services.code_index.service import CodeIndexService

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("A symbol or file query is required.")
    if not context.repo_id:
        raise ValueError("This action needs a repository. Attach one to the session first.")
    try:
        results = CodeIndexService().search(context.repo_id, query, limit=25)
    except Exception as exc:
        return f"The code index is not available for this repository ({exc}). Use grep instead."
    if not results:
        return "(no indexed symbols matched; try grep)"
    lines = []
    for item in results[:25]:
        location = item.get("relative_path") or "?"
        if item.get("line_start"):
            location = f"{location}:{item['line_start']}"
        kind = item.get("symbol_type") or item.get("result_type") or "match"
        lines.append(f"{location}: {kind} {item.get('name') or ''}".rstrip())
    return "\n".join(lines)


def recall_memory(arguments: dict, _context: ToolContext) -> str:
    from app.services.memory_retrieval.service import MemoryRetrievalService

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("A query is required.")
    try:
        result = MemoryRetrievalService().retrieve_for_agent(
            query,
            scope_type="project" if _context.project_id else None,
            scope_id=_context.project_id,
            source="agent_core",
        )
    except Exception as exc:
        return f"Memory recall is unavailable ({exc})."
    items = (result or {}).get("results") or []
    if not items:
        return "(nothing relevant in memory)"
    lines = [
        f"- [{item.get('memory_type', 'memory')}] {item.get('title', '')}:"
        f" {item.get('snippet', '')}"
        for item in items
    ]
    return "\n".join(lines)[:_LIMIT]


TOOLS = [
    AgentTool(
        name="web_search",
        description="Search the web and return extracted evidence with sources.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        risk="read",
        handler=web_search,
        summary=lambda a: f"Search the web for {a.get('query')!r}",
    ),
    AgentTool(
        name="web_fetch",
        description="Retrieve the readable text of a specific URL.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        risk="read",
        handler=web_fetch,
        summary=lambda a: f"Fetch {a.get('url')}",
    ),
    AgentTool(
        name="find_symbol",
        description="Look up a symbol or file in the repository's code index.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        risk="read",
        requires_repo=True,
        handler=find_symbol,
        summary=lambda a: f"Find symbol {a.get('query')!r}",
    ),
    AgentTool(
        name="recall_memory",
        description="Recall relevant facts from the user's long-term memory.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        risk="read",
        handler=recall_memory,
        summary=lambda a: f"Recall memory about {a.get('query')!r}",
    ),
]
