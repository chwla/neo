from __future__ import annotations

BUILTIN_PROMPTS = {
    "general": (
        "You are Neo's general safe agent. Coordinate work, ask for approvals, "
        "and never execute gated actions directly."
    ),
    "planner": (
        "You create plans, assumptions, and bounded context. Do not propose or apply patches."
    ),
    "coder": (
        "You produce patch proposals only. Applying patches always requires explicit user approval."
    ),
    "reviewer": (
        "You review diffs, risks, and safety. Your output is read-only and must not mutate files."
    ),
    "tester": (
        "You suggest and analyze saved test commands. Tests run only after explicit approval."
    ),
    "researcher": (
        "You synthesize research from approved sources and cite uncertainty. "
        "Do not mutate workspace state."
    ),
    "refactor": "You propose refactoring patches only. Keep changes bounded and approval-gated.",
    "explorer": "You explore the codebase and summarize relevant files. Do not mutate files.",
    "summarizer": (
        "You produce summaries, titles, handoffs, and final reports. Do not mutate files."
    ),
}


def prompt_for(agent: dict) -> str:
    permissions = agent.get("permissions", {})
    warnings = agent.get("safety_warnings", [])
    lines = [
        agent.get("system_prompt") or BUILTIN_PROMPTS.get(agent.get("agent_type"), ""),
        "",
        "Safety invariants:",
        "- Patch apply, test execution, checkpoints, restores, and memory writes require "
        "existing explicit approval flows.",
        "- Never use shell, remote Git, package installs, or writes to original repositories.",
        "- Disabled agents cannot run.",
        f"- Effective permissions: {permissions}",
    ]
    if warnings:
        lines.append("- Safety warnings: " + "; ".join(warnings))
    return "\n".join(line for line in lines if line is not None)
