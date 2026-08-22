"""Tools that bring outside knowledge in: web, code intelligence, memory.

Each one adapts a service Neo already runs. None of them re-implements search,
indexing or recall -- they exist so the model can *reach* those services on its
own initiative rather than having their output pasted into a prompt by a
hardcoded pipeline step, which is what the old runner did.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext

_LIMIT = 9000


def web_search(arguments: dict, _context: ToolContext) -> str:
    from app.services.web_search import WebSearchService

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("A search query is required.")
    result = WebSearchService().build_context_forced(query)
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
    page = WebPageFetcher().fetch(url)
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
        lines.append(f"{location} — {kind} {item.get('name') or ''}".rstrip())
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
