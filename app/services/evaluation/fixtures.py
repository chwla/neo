from __future__ import annotations

BUILTIN_SUITES = {
    "agentic_basic": ("agentic_task", "Agentic Core basic planning and verification"),
    "coding_agent_patch_safety": ("coding_task", "Coding claims and patch safety"),
    "web_search_grounding": ("web_search_task", "Reliable Web Search evidence grounding"),
    "research_citation_accuracy": ("research_task", "Enterprise Research citation accuracy"),
    "memory_retrieval_relevance": (
        "memory_retrieval_task",
        "Memory retrieval relevance and redaction",
    ),
    "provider_runtime_reliability": (
        "provider_runtime_task",
        "Provider route, fallback, and degraded handling",
    ),
    "context_compaction_regression": ("context_compaction_task", "Context compaction usefulness"),
    "tool_safety_regression": ("tool_safety_task", "Command sandbox approval enforcement"),
}


def builtin_cases(suite_id: str) -> list[dict]:
    case_type, title = BUILTIN_SUITES[suite_id]
    output = {
        "answer": "Fixture response supported by fixture evidence.",
        "evidence": [
            {"id": "fixture-source", "url": "https://fixture.invalid/evidence", "supports": True}
        ],
        "citations": ["fixture-source"],
        "claims": [{"text": "Fixture response", "supported": True}],
        "memory_ids": ["fixture-memory"],
        "provider_request_ids": ["fixture-provider-request"],
        "route_used": "chat",
        "retry_count": 0,
        "fallback_chain": [],
        "degraded_reason": None,
        "patch_applied": True,
        "tests_passed": True,
        "command_approved": True,
        "compaction_preserved": True,
        "latency_ms": 1,
        "token_estimate": 12,
    }
    return [
        {
            "name": f"{suite_id} fixture",
            "case_type": case_type,
            "input": {"objective": title, "fixture": True},
            "expected": output,
            "fixture": output,
        }
    ]
