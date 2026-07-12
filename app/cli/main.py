from __future__ import annotations

import argparse
import sys
from typing import Any

from app.cli.client import ApiError, ApiUnavailableError, NeoApiClient
from app.cli.config import from_args
from app.cli.formatters import emit

# ruff: noqa: E501, E701

EXIT_INPUT = 1
EXIT_UNAVAILABLE = 2
EXIT_API = 3
EXIT_DENIED = 4
GLOBAL_FLAGS_WITH_VALUE = {"--api", "--api-url", "--timeout"}
GLOBAL_FLAGS_BOOL = {"--json", "--no-color"}


def normalize_global_args(argv: list[str]) -> list[str]:
    """Allow global flags either before or after the command path.

    argparse only recognizes top-level options before the selected subcommand.
    The CLI examples intentionally use natural command ordering such as
    `neo status --api-url http://127.0.0.1:8000`, so we move only known global
    flags to the front before parsing. Command-specific flags stay in place.
    """

    prefix: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in GLOBAL_FLAGS_BOOL:
            prefix.append(token)
            index += 1
        elif token in GLOBAL_FLAGS_WITH_VALUE:
            if index + 1 >= len(argv):
                rest.append(token)
                index += 1
            else:
                prefix.extend([token, argv[index + 1]])
                index += 2
        elif any(token.startswith(f"{flag}=") for flag in GLOBAL_FLAGS_WITH_VALUE):
            prefix.append(token)
            index += 1
        else:
            rest.append(token)
            index += 1
    # Preserve the concise documented spelling: `neo bundles import file.zip`.
    if (
        len(rest) >= 3
        and rest[:2] == ["bundles", "import"]
        and rest[2] not in {"validate", "archive", "-h", "--help"}
    ):
        rest.insert(2, "archive")
    if len(rest) >= 3 and rest[:2] == ["commands", "run"] and "--yes" in rest[2:]:
        rest.remove("--yes")
        rest.insert(2, "--yes")
    return prefix + rest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neo")
    parser.add_argument("--api", "--api-url", dest="api_url")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    sub.add_parser("health")

    agents = sub.add_parser("agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list")
    show_agent = agents_sub.add_parser("show")
    show_agent.add_argument("agent_id")

    coding = sub.add_parser("coding")
    coding_sub = coding.add_subparsers(dest="coding_command", required=True)
    start = coding_sub.add_parser("start")
    start.add_argument("objective")
    start.add_argument("--project", dest="project_id")
    start.add_argument("--repo", dest="repo_id")
    start.add_argument("--task", dest="task_id")
    start.add_argument("--agent", dest="agent_definition_id")
    start.add_argument("--max-iterations", type=int, default=3)
    coding_sub.add_parser("list")
    show = coding_sub.add_parser("show")
    show.add_argument("run_id")
    actions = coding_sub.add_parser("actions")
    actions.add_argument("run_id")
    cancel = coding_sub.add_parser("cancel")
    cancel.add_argument("run_id")
    approve = coding_sub.add_parser("approve")
    approve.add_argument("action_id")
    approve.add_argument("--yes", action="store_true")
    reject = coding_sub.add_parser("reject")
    reject.add_argument("action_id")
    reject.add_argument("--reason")
    revise = coding_sub.add_parser("revise")
    revise.add_argument("run_id")
    revise.add_argument("--instructions", required=True)

    agentic = sub.add_parser("agentic")
    agentic_sub = agentic.add_subparsers(dest="agentic_command", required=True)
    agentic_start = agentic_sub.add_parser("start")
    agentic_start.add_argument(
        "--type",
        dest="run_type",
        choices=["coding", "research", "task"],
        required=True,
    )
    agentic_start.add_argument("--objective", required=True)
    agentic_start.add_argument("--project", dest="project_id")
    agentic_start.add_argument("--task", dest="task_id")
    agentic_start.add_argument("--repo", dest="repo_id")
    agentic_start.add_argument("--max-steps", type=int, default=20)
    agentic_sub.add_parser("list")
    for command in ("show", "steps", "continue", "context", "stop"):
        item = agentic_sub.add_parser(command)
        item.add_argument("run_id")

    web = sub.add_parser("web")
    web_sub = web.add_subparsers(dest="web_command", required=True)
    web_plan = web_sub.add_parser("plan")
    web_plan.add_argument("query")
    web_run = web_sub.add_parser("run")
    web_run.add_argument("query")
    web_run.add_argument("--mode", default="research")
    web_run.add_argument("--fresh", action="store_true")
    for name in ("show", "sources", "evidence", "conflicts"):
        command = web_sub.add_parser(name)
        command.add_argument("run_id")
    web_sub.add_parser("cache")

    providers = sub.add_parser("providers")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    for command in ("status", "health", "health-check", "requests", "usage", "rate-limits"):
        providers_sub.add_parser(command)
    provider_show = providers_sub.add_parser("show-request")
    provider_show.add_argument("request_id")
    provider_complete = providers_sub.add_parser("complete")
    provider_complete.add_argument("prompt")
    provider_complete.add_argument("--type", dest="request_type", default="chat")
    provider_complete.add_argument("--route", dest="route_name")
    provider_complete.add_argument("--max-tokens", type=int, default=1200)

    evals = sub.add_parser("eval")
    eval_sub = evals.add_subparsers(dest="eval_command", required=True)
    eval_sub.add_parser("suites")
    eval_run = eval_sub.add_parser("run")
    eval_run.add_argument("suite")
    eval_run.add_argument("--max-cases", type=int)
    eval_run.add_argument("--fail-fast", action="store_true")
    eval_sub.add_parser("list")
    for name in ("show", "cases", "report"):
        item = eval_sub.add_parser(name)
        item.add_argument("run_id")
    eval_baseline = eval_sub.add_parser("baseline")
    eval_baseline.add_argument("run_id")
    eval_baseline.add_argument("--name", default="stable")
    eval_compare = eval_sub.add_parser("compare")
    eval_compare.add_argument("run_id")
    eval_compare.add_argument("--baseline")

    workspace = sub.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    wc = workspace_sub.add_parser("create")
    wc.add_argument("--name", required=True)
    wc.add_argument("--goal", required=True)
    wc.add_argument("--scope", default="")
    workspace_sub.add_parser("list")
    for command in ("show", "plan", "graph", "timeline", "readiness", "health", "report", "index-memory"):
        item = workspace_sub.add_parser(command)
        item.add_argument("workspace_id")
    wl = workspace_sub.add_parser("link")
    wl.add_argument("workspace_id")
    wl.add_argument("--type", required=True)
    wl.add_argument("--id", required=True)

    continuity = sub.add_parser("continuity")
    continuity_sub = continuity.add_subparsers(dest="continuity_command", required=True)
    continuity_sub.add_parser("bundles")
    ce = continuity_sub.add_parser("export")
    ce.add_argument("--type", dest="bundle_type", required=True)
    ce.add_argument("--id", dest="root_entity_id", required=True)
    ce.add_argument("--root-type", default="workspace")
    for command in ("show", "manifest", "references", "validation", "report"):
        item = continuity_sub.add_parser(command)
        item.add_argument("bundle_id")
    cd = continuity_sub.add_parser("import-dry-run")
    cd.add_argument("bundle_path")
    ci = continuity_sub.add_parser("import")
    ci.add_argument("bundle_path")
    ci.add_argument("--mode", default="append")
    continuity_sub.add_parser("validate-references")
    cv = continuity_sub.add_parser("validate-entity")
    cv.add_argument("--type", required=True)
    cv.add_argument("--id", required=True)

    integration = sub.add_parser("integration")
    integration_sub = integration.add_subparsers(dest="integration_command", required=True)
    for command in ("status", "validate", "report", "smoke"):
        integration_sub.add_parser(command)

    research = sub.add_parser("research")
    research_sub = research.add_subparsers(dest="research_command", required=True)
    research_plan = research_sub.add_parser("plan")
    research_plan.add_argument("question")
    research_plan.add_argument("--mode", default="general")
    research_plan.add_argument("--fresh", action="store_true")
    research_plan.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    research_run = research_sub.add_parser("run")
    research_run.add_argument("question")
    research_run.add_argument("--mode", default="general")
    research_run.add_argument("--fresh", action="store_true")
    research_run.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    research_sub.add_parser("list")
    for command in (
        "show",
        "evidence",
        "claims",
        "conflicts",
        "report",
        "validate-citations",
        "continue",
        "refresh",
    ):
        item = research_sub.add_parser(command)
        item.add_argument("run_id")

    memory = sub.add_parser("memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_index = memory_sub.add_parser("index")
    memory_index.add_argument("--scope", default="")
    memory_retrieve = memory_sub.add_parser("retrieve")
    memory_retrieve.add_argument("query")
    memory_retrieve.add_argument("--scope", default="")
    memory_retrieve.add_argument("--type", dest="memory_type")
    memory_sub.add_parser("list")
    memory_show = memory_sub.add_parser("show")
    memory_show.add_argument("memory_id")
    memory_sub.add_parser("prune-preview")
    memory_apply = memory_sub.add_parser("prune-apply")
    memory_apply.add_argument("--yes", action="store_true")

    lsp = sub.add_parser("lsp")
    lsp_sub = lsp.add_subparsers(dest="lsp_command", required=True)
    lsp_sub.add_parser("status")
    lsp_sub.add_parser("servers")
    for command in ("start", "stop", "diagnostics"):
        item = lsp_sub.add_parser(command)
        item.add_argument("workspace_id")
    symbols = lsp_sub.add_parser("symbols")
    symbols.add_argument("workspace_id")
    symbols.add_argument("--query", default="")
    for command in ("hover", "definition", "references", "rename-preview"):
        item = lsp_sub.add_parser(command)
        item.add_argument("workspace_id")
        item.add_argument("--file", dest="file_path", required=True)
        item.add_argument("--line", type=int, default=0)
        item.add_argument("--character", type=int, default=0)

    recovery = sub.add_parser("recovery")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_sub.add_parser("list")
    rec_show = recovery_sub.add_parser("show")
    rec_show.add_argument("run_type")
    rec_show.add_argument("run_id")
    for command in ("resume", "retry"):
        item = recovery_sub.add_parser(command)
        item.add_argument("run_type")
        item.add_argument("run_id")
        item.add_argument("--yes", action="store_true")
    rec_fork = recovery_sub.add_parser("fork")
    rec_fork.add_argument("run_type")
    rec_fork.add_argument("run_id")
    rec_fork.add_argument("--objective")
    rec_fork.add_argument("--yes", action="store_true")

    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_sub.add_parser("list")
    resolve = rules_sub.add_parser("resolve")
    resolve.add_argument("--project", dest="project_id")
    resolve.add_argument("--repo", dest="repo_id")
    resolve.add_argument("--task", dest="task_id")
    resolve.add_argument("--context", dest="context_type", default="coding_agent")

    tools = sub.add_parser("tools")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list")
    tools_sub.add_parser("calls")
    skills = sub.add_parser("skills")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("list")

    tests = sub.add_parser("tests")
    tests_sub = tests.add_subparsers(dest="tests_command", required=True)
    tests_list = tests_sub.add_parser("list")
    tests_list.add_argument("--repo", dest="repo_id", required=True)
    tests_runs = tests_sub.add_parser("runs")
    tests_runs.add_argument("--repo", dest="repo_id")
    tests_run = tests_sub.add_parser("run")
    tests_run.add_argument("command_id")
    tests_run.add_argument("--yes", action="store_true")

    git = sub.add_parser("git")
    git_sub = git.add_subparsers(dest="git_command", required=True)
    git_status = git_sub.add_parser("status")
    git_status.add_argument("repo_id")
    git_checkpoints = git_sub.add_parser("checkpoints")
    git_checkpoints.add_argument("repo_id")
    git_checkpoint = git_sub.add_parser("checkpoint")
    git_checkpoint.add_argument("repo_id")
    git_checkpoint.add_argument("--title", required=True)
    git_checkpoint.add_argument("--message")
    git_checkpoint.add_argument("--yes", action="store_true")

    export = sub.add_parser("export")
    export_sub = export.add_subparsers(dest="export_command", required=True)
    exp_run = export_sub.add_parser("run")
    exp_run.add_argument("run_id")
    exp_run.add_argument("--type", choices=["coding_agent", "agent"], default="coding_agent")
    exp_run.add_argument("--out", required=True)
    exp_task = export_sub.add_parser("task")
    exp_task.add_argument("task_id")
    exp_task.add_argument("--out", required=True)
    github = sub.add_parser("github")
    github_sub = github.add_subparsers(dest="github_command", required=True)
    github_sub.add_parser("connections")
    github_health = github_sub.add_parser("health")
    github_health.add_argument("connection_id")
    for command in ("import-issue", "import-pr"):
        item = github_sub.add_parser(command)
        item.add_argument("connection_id")
        item.add_argument("number", type=int)
    github_task = github_sub.add_parser("create-task")
    github_task.add_argument("item_id")
    github_task.add_argument("--yes", action="store_true")
    github_sub.add_parser("ops")
    bundles = sub.add_parser("bundles")
    bundles_sub = bundles.add_subparsers(dest="bundles_command", required=True)
    bundles_sub.add_parser("list")
    bundle_import = bundles_sub.add_parser("import")
    bundle_import_sub = bundle_import.add_subparsers(dest="bundle_import_command", required=True)
    validate = bundle_import_sub.add_parser("validate")
    validate.add_argument("file")
    import_file = bundle_import_sub.add_parser("archive")
    import_file.add_argument("file")
    import_file.add_argument("--yes", action="store_true")
    context = sub.add_parser("context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_sub.add_parser("summaries")
    context_show = context_sub.add_parser("show")
    context_show.add_argument("summary_id")
    for command in ("preview", "compact"):
        item = context_sub.add_parser(command)
        item.add_argument("scope_type")
        item.add_argument("scope_id")
        item.add_argument("--max-summary-tokens", type=int, default=1200)
        if command == "compact":
            item.add_argument("--yes", action="store_true")
    commands = sub.add_parser("commands")
    commands_sub = commands.add_subparsers(dest="commands_command", required=True)
    commands_sub.add_parser("list")
    commands_show = commands_sub.add_parser("show")
    commands_show.add_argument("run_id")
    for command in ("validate", "propose", "run"):
        item = commands_sub.add_parser(command)
        item.add_argument("workspace_id")
        item.add_argument("--category", choices=["read_only", "test", "build"], default="test")
        item.add_argument("argv", nargs=argparse.REMAINDER)
        if command == "run":
            item.add_argument("--yes", action="store_true")
    tui = sub.add_parser("tui")
    tui.add_argument("--api", dest="tui_api")
    tui.add_argument("--refresh", type=float, default=2.0)
    tui.add_argument("--snapshot", action="store_true")
    tui.add_argument(
        "--view",
        choices=["dashboard", "tasks", "coding-runs", "agents", "commands", "context", "settings"],
        default="dashboard",
    )
    return parser


def confirm(args, message: str) -> bool:
    if getattr(args, "yes", False):
        return True
    print(message)
    answer = input("Continue? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def require_confirm(args, message: str) -> None:
    if not confirm(args, message):
        raise SafetyDenied("Confirmation denied.")


class SafetyDenied(RuntimeError):
    pass


def command_status(client: NeoApiClient) -> dict[str, Any]:
    health = client.get("/api/health")
    providers = client.get("/api/llm/providers")
    routes = client.get("/api/llm/routes")
    return {"health": health, "llm_providers": providers, "llm_routes": routes}


def handle(args, client: NeoApiClient) -> Any:
    if args.command in {"status", "health"}:
        return command_status(client)
    if args.command == "agents":
        if args.agents_command == "list":
            return client.get("/api/agents/definitions")
        return client.get(f"/api/agents/definitions/{args.agent_id}")
    if args.command == "coding":
        return handle_coding(args, client)
    if args.command == "agentic":
        return handle_agentic(args, client)
    if args.command == "web":
        return handle_web(args, client)
    if args.command == "providers":
        return handle_providers(args, client)
    if args.command == "eval":
        return handle_evals(args, client)
    if args.command == "workspace":
        return handle_workspace(args, client)
    if args.command == "continuity":
        return handle_continuity(args, client)
    if args.command == "integration":
        return handle_integration(args, client)
    if args.command == "research":
        return handle_research(args, client)
    if args.command == "memory":
        return handle_memory(args, client)
    if args.command == "lsp":
        return handle_lsp(args, client)
    if args.command == "recovery":
        return handle_recovery(args, client)
    if args.command == "rules":
        if args.rules_command == "list":
            return client.get("/api/rules/profiles")
        return client.post(
            "/api/rules/resolve",
            {
                "context_type": args.context_type,
                "project_id": args.project_id,
                "repo_id": args.repo_id,
                "task_id": args.task_id,
                "profile_ids": [],
            },
        )
    if args.command == "tools":
        path = "/api/tools/definitions" if args.tools_command == "list" else "/api/tools/calls"
        return client.get(path)
    if args.command == "skills":
        return client.get("/api/tools/skills")
    if args.command == "tests":
        return handle_tests(args, client)
    if args.command == "git":
        return handle_git(args, client)
    if args.command == "export":
        return handle_export(args, client)
    if args.command == "bundles":
        return handle_bundles(args, client)
    if args.command == "github":
        return handle_github(args, client)
    if args.command == "context":
        return handle_context(args, client)
    if args.command == "commands":
        return handle_commands(args, client)
    if args.command == "tui":
        from app.cli.tui import run_tui

        return run_tui(client, snapshot_mode=args.snapshot, view=args.view)
    raise ValueError("Unknown command.")


def handle_coding(args, client: NeoApiClient) -> Any:
    if args.coding_command == "start":
        return client.post(
            "/api/coding-agent/runs",
            {
                "objective": args.objective,
                "project_id": args.project_id,
                "repo_id": args.repo_id,
                "task_id": args.task_id,
                "agent_definition_id": args.agent_definition_id,
                "max_iterations": args.max_iterations,
            },
        )
    if args.coding_command == "list":
        return client.get("/api/coding-agent/runs")
    if args.coding_command == "show":
        return client.get(f"/api/coding-agent/runs/{args.run_id}")
    if args.coding_command == "actions":
        detail = client.get(f"/api/coding-agent/runs/{args.run_id}")
        return {"action_requests": detail.get("action_requests", [])}
    if args.coding_command == "cancel":
        return client.post(f"/api/coding-agent/runs/{args.run_id}/cancel")
    if args.coding_command == "approve":
        require_confirm(args, APPROVAL_WARNING)
        return client.post(
            f"/api/coding-agent/actions/{args.action_id}/approve",
            {"confirm": True, "options": {}},
        )
    if args.coding_command == "reject":
        return client.post(
            f"/api/coding-agent/actions/{args.action_id}/reject",
            {"reason": args.reason},
        )
    if args.coding_command == "revise":
        return client.post(
            f"/api/coding-agent/runs/{args.run_id}/revise-patch",
            {"instructions": args.instructions},
        )
    raise ValueError("Unknown coding command.")


def handle_agentic(args, client: NeoApiClient) -> Any:
    if args.agentic_command == "start":
        return client.post(
            "/api/agentic/runs",
            {
                "objective": args.objective,
                "run_type": args.run_type,
                "project_id": args.project_id,
                "task_id": args.task_id,
                "repo_id": args.repo_id,
                "max_steps": args.max_steps,
                "require_approval_for_actions": True,
            },
        )
    if args.agentic_command == "list":
        return client.get("/api/agentic/runs")
    if args.agentic_command == "show":
        return client.get(f"/api/agentic/runs/{args.run_id}")
    if args.agentic_command == "steps":
        return client.get(f"/api/agentic/runs/{args.run_id}/steps")
    if args.agentic_command == "context":
        return client.get(f"/api/agentic/runs/{args.run_id}/context")
    if args.agentic_command == "continue":
        return client.post(f"/api/agentic/runs/{args.run_id}/continue", {})
    if args.agentic_command == "stop":
        return client.post(f"/api/agentic/runs/{args.run_id}/stop")
    raise ValueError("Unknown agentic command.")


def handle_web(args, client: NeoApiClient) -> Any:
    if args.web_command == "plan":
        return client.post("/api/web-search/plan", {"query": args.query})
    if args.web_command == "run":
        return client.post(
            "/api/web-search/run",
            {"query": args.query, "mode": args.mode, "freshness_required": args.fresh},
        )
    if args.web_command == "cache":
        return client.get("/api/web-search/cache")
    return client.get(
        f"/api/web-search/runs/{args.run_id}"
        + ("" if args.web_command == "show" else f"/{args.web_command}")
    )


def handle_providers(args, client: NeoApiClient) -> Any:
    command = args.providers_command
    if command == "status":
        return client.get("/api/providers/runtime/status")
    if command == "health":
        return client.get("/api/providers/runtime/health")
    if command == "health-check":
        return client.post("/api/providers/runtime/health-check", {})
    if command == "requests":
        return client.get("/api/providers/runtime/requests")
    if command == "usage":
        return client.get("/api/providers/runtime/usage")
    if command == "rate-limits":
        return client.get("/api/providers/runtime/rate-limits")
    if command == "show-request":
        return client.get(f"/api/providers/runtime/requests/{args.request_id}")
    return client.post(
        "/api/providers/runtime/complete",
        {
            "request_type": args.request_type,
            "route_name": args.route_name,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens,
            "metadata": {"created_by": "cli"},
        },
    )


def handle_evals(args, client: NeoApiClient) -> Any:
    command = args.eval_command
    if command == "suites":
        return client.get("/api/evals/suites")
    if command == "list":
        return client.get("/api/evals/runs")
    if command == "run":
        return client.post(f"/api/evals/suites/{args.suite}/run", {"fixture_mode": True, "max_cases": args.max_cases, "fail_fast": args.fail_fast})
    if command == "baseline":
        return client.post(f"/api/evals/runs/{args.run_id}/set-baseline", {"name": args.name})
    if command == "compare":
        return client.get("/api/evals/compare", query={"run_id": args.run_id, "baseline_id": args.baseline})
    suffix = "" if command == "show" else f"/{command}"
    return client.get(f"/api/evals/runs/{args.run_id}{suffix}")


def handle_workspace(args, client: NeoApiClient) -> Any:
    command = args.workspace_command
    if command == "list": return client.get("/api/workspaces")
    if command == "create": return client.post("/api/workspaces", {"name": args.name, "goal": args.goal, "scope": args.scope})
    if command == "link": return client.post(f"/api/workspaces/{args.workspace_id}/link", {"entity_type": args.type, "entity_id": args.id})
    if command == "plan": return client.post(f"/api/workspaces/{args.workspace_id}/plan", {})
    if command == "index-memory": return client.post(f"/api/workspaces/{args.workspace_id}/index-memory", {})
    return client.get(f"/api/workspaces/{args.workspace_id}" + ("" if command == "show" else f"/{command}"))

def handle_continuity(args, client: NeoApiClient) -> Any:
    command = args.continuity_command
    if command == "bundles": return client.get("/api/continuity/bundles")
    if command == "export": return client.post("/api/continuity/export", {"bundle_type": args.bundle_type, "root_entity_type": args.root_type, "root_entity_id": args.root_entity_id})
    if command == "import-dry-run": return client.post("/api/continuity/import/dry-run", {"bundle_path": args.bundle_path})
    if command == "import": return client.post("/api/continuity/import", {"bundle_path": args.bundle_path, "mode": args.mode})
    if command == "validate-references": return client.post("/api/continuity/validate-references", {})
    if command == "validate-entity": return client.post("/api/continuity/validate-entity", {"entity_type": args.type, "entity_id": args.id})
    return client.get(f"/api/continuity/bundles/{args.bundle_id}" + ("" if command == "show" else f"/{command}"))


def handle_integration(args, client: NeoApiClient) -> Any:
    command = args.integration_command
    if command == "status":
        return client.get("/api/integration/status")
    if command == "report":
        return client.get("/api/integration/report")
    return client.post(f"/api/integration/{command}", {})


def handle_research(args, client: NeoApiClient) -> Any:
    if args.research_command == "plan":
        return client.post(
            "/api/research/plan",
            {
                "question": args.question,
                "mode": args.mode,
                "freshness_required": args.fresh,
                "depth": args.depth,
            },
        )
    if args.research_command == "run":
        return client.post(
            "/api/research/run",
            {
                "question": args.question,
                "mode": args.mode,
                "freshness_required": args.fresh,
                "depth": args.depth,
                "max_search_runs": 4 if args.depth == "deep" else 2,
                "max_sources": 20 if args.depth == "deep" else 12,
                "include_memory": True,
                "include_conflict_analysis": True,
                "created_by": "cli",
            },
        )
    if args.research_command == "list":
        return client.get("/api/research/runs")
    if args.research_command in {"validate-citations", "continue", "refresh"}:
        return client.post(f"/api/research/runs/{args.run_id}/{args.research_command}")
    return client.get(
        f"/api/research/runs/{args.run_id}"
        + ("" if args.research_command == "show" else f"/{args.research_command}")
    )


def _memory_scope(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if ":" not in value:
        raise ValueError("Memory scope must be type:id.")
    return tuple(value.split(":", 1))  # type: ignore[return-value]


def handle_memory(args, client: NeoApiClient) -> Any:
    scope_type, scope_id = _memory_scope(getattr(args, "scope", ""))
    if args.memory_command == "index":
        return client.post("/api/memory/index", {"scope_type": scope_type, "scope_id": scope_id})
    if args.memory_command == "retrieve":
        return client.post(
            "/api/memory/retrieve",
            {
                "query": args.query,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "memory_types": [args.memory_type] if args.memory_type else [],
                "include_score_breakdown": True,
                "created_by": "cli",
            },
        )
    if args.memory_command == "list":
        return client.get("/api/memory/items")
    if args.memory_command == "show":
        return client.get(f"/api/memory/items/{args.memory_id}")
    if args.memory_command == "prune-preview":
        return client.post("/api/memory/prune/preview", {})
    if args.memory_command == "prune-apply":
        require_confirm(
            args,
            "Memory pruning deletes only previewed non-protected memories. Use --yes to confirm.",
        )
        return client.post("/api/memory/prune/apply", {"confirm": True})
    raise ValueError("Unknown memory command.")


def handle_lsp(args, client: NeoApiClient) -> Any:
    if args.lsp_command == "status":
        return client.get("/api/lsp/status")
    if args.lsp_command == "servers":
        return client.get("/api/lsp/servers")
    if args.lsp_command == "diagnostics":
        return client.get(f"/api/lsp/workspaces/{args.workspace_id}/diagnostics")
    if args.lsp_command in {"start", "stop"}:
        path = f"/api/lsp/workspaces/{args.workspace_id}/{args.lsp_command}"
        return client.post(path, {"language": "python"})
    action = "workspace-symbols" if args.lsp_command == "symbols" else args.lsp_command
    return client.post(
        f"/api/lsp/workspaces/{args.workspace_id}/{action}",
        {
            "file_path": getattr(args, "file_path", ""),
            "line": getattr(args, "line", 0),
            "character": getattr(args, "character", 0),
            "query": getattr(args, "query", ""),
            "language": "python",
        },
    )


APPROVAL_WARNING = (
    "This action affects only Neo's managed workspace copy.\n"
    "It will not modify the original repository."
)


def handle_recovery(args, client: NeoApiClient) -> Any:
    if args.recovery_command == "list":
        return client.get("/api/recovery/runs")
    if args.recovery_command == "show":
        return client.get(f"/api/recovery/runs/{args.run_type}/{args.run_id}")
    if args.recovery_command == "resume":
        require_confirm(args, "Resume this run? Approval gates remain intact.")
        return client.post(
            f"/api/recovery/runs/{args.run_type}/{args.run_id}/resume",
            {"confirm": True},
        )
    if args.recovery_command == "retry":
        require_confirm(args, "Retry this run? Approval gates remain intact.")
        return client.post(
            f"/api/recovery/runs/{args.run_type}/{args.run_id}/retry",
            {"confirm": True},
        )
    if args.recovery_command == "fork":
        require_confirm(args, "Fork this run? No actions execute automatically.")
        return client.post(
            f"/api/recovery/runs/{args.run_type}/{args.run_id}/fork",
            {"confirm": True, "objective_override": args.objective},
        )
    raise ValueError("Unknown recovery command.")


def handle_tests(args, client: NeoApiClient) -> Any:
    if args.tests_command == "list":
        return client.get(f"/api/test-runner/repos/{args.repo_id}/commands")
    if args.tests_command == "runs":
        return client.get("/api/test-runner/runs", query={"repo_id": args.repo_id})
    if args.tests_command == "run":
        require_confirm(args, "Run this saved test command in Neo's managed workspace copy?")
        return client.post(
            f"/api/test-runner/commands/{args.command_id}/run",
            {"confirm": True},
        )
    raise ValueError("Unknown tests command.")


def handle_git(args, client: NeoApiClient) -> Any:
    if args.git_command == "status":
        return client.get(f"/api/git/repos/{args.repo_id}/status")
    if args.git_command == "checkpoints":
        return client.get(f"/api/git/repos/{args.repo_id}/checkpoints")
    if args.git_command == "checkpoint":
        require_confirm(args, "Create a local checkpoint in Neo's managed workspace copy?")
        return client.post(
            f"/api/git/repos/{args.repo_id}/checkpoints",
            {"title": args.title, "message": args.message, "confirm": True},
        )
    raise ValueError("Unknown git command.")


def handle_export(args, client: NeoApiClient) -> dict[str, str]:
    kind, entity_id = (
        (("coding_run" if args.type == "coding_agent" else "agent_run"), args.run_id)
        if args.export_command == "run"
        else ("task", args.task_id)
    )
    created = client.post(
        "/api/bundles/export",
        {
            "bundle_type": kind,
            "entity_id": entity_id,
            "include_files": True,
            "include_patch_text": True,
            "include_test_output": True,
            "redact_secrets": True,
        },
    )
    bundle = created["bundle"]
    from pathlib import Path

    Path(args.out).write_bytes(client.download(f"/api/bundles/exports/{bundle['id']}/download"))
    return {"exported": args.out, "bundle_id": bundle["id"], "type": kind, "id": entity_id}


def handle_bundles(args, client: NeoApiClient) -> Any:
    if args.bundles_command == "list":
        return {
            "exports": client.get("/api/bundles/exports"),
            "imports": client.get("/api/bundles/imports"),
        }
    from pathlib import Path

    path = Path(args.file)
    data = path.read_bytes()
    if args.bundle_import_command == "validate":
        return client.upload("/api/bundles/import/validate", path.name, data)
    require_confirm(
        args,
        "Archive this bundle as inert data? It will not execute, apply patches, "
        "run tests, or create checkpoints.",
    )
    return client.upload(
        "/api/bundles/import", path.name, data, {"confirm": "true", "mode": "archive_only"}
    )


def handle_github(args, client: NeoApiClient) -> Any:
    if args.github_command == "connections":
        return client.get("/api/github/connections")
    if args.github_command == "ops":
        return client.get("/api/github/operations")
    if args.github_command == "health":
        return client.post(f"/api/github/connections/{args.connection_id}/health")
    if args.github_command in {"import-issue", "import-pr"}:
        path = "issues" if args.github_command == "import-issue" else "pulls"
        return client.post(
            f"/api/github/connections/{args.connection_id}/{path}/{args.number}/import"
        )
    require_confirm(
        args, "Create a Neo task from this imported issue? This does not modify GitHub."
    )
    return client.post(f"/api/github/items/{args.item_id}/create-task")


def handle_context(args, client: NeoApiClient) -> Any:
    if args.context_command == "summaries":
        return client.get("/api/context-memory/summaries")
    if args.context_command == "show":
        return client.get(f"/api/context-memory/summaries/{args.summary_id}")
    payload = {
        "scope_type": args.scope_type,
        "scope_id": args.scope_id,
        "mode": "safe",
        "max_summary_tokens": args.max_summary_tokens,
        "include_events": True,
        "include_files": True,
        "include_tests": True,
        "include_checkpoints": True,
    }
    if args.context_command == "preview":
        return client.post("/api/context-memory/preview", payload)
    require_confirm(
        args,
        "Save a redacted deterministic context summary? It never executes actions "
        "or changes source data.",
    )
    return client.post("/api/context-memory/compact", payload)


def handle_commands(args, client: NeoApiClient) -> Any:
    if args.commands_command == "list":
        return client.get("/api/command-sandbox/runs")
    if args.commands_command == "show":
        return client.get(f"/api/command-sandbox/runs/{args.run_id}")
    command = list(args.argv)
    if command and command[0] == "--":
        command = command[1:]
    payload = {
        "workspace_id": args.workspace_id,
        "command": command,
        "cwd": ".",
        "category": args.category,
        "created_by": "cli",
    }
    if args.commands_command == "validate":
        return client.post("/api/command-sandbox/validate", payload)
    proposed = client.post("/api/command-sandbox/propose", payload)
    if args.commands_command == "propose":
        return proposed
    require_confirm(args, "Approve this allowlisted command in Neo's managed workspace?")
    client.post(f"/api/command-sandbox/runs/{proposed['id']}/approve", {"confirm": True})
    return client.post(f"/api/command-sandbox/runs/{proposed['id']}/execute")


def main(argv: list[str] | None = None, client: NeoApiClient | None = None) -> int:
    parser = build_parser()
    try:
        parse_argv = normalize_global_args(sys.argv[1:] if argv is None else argv)
        args = parser.parse_args(parse_argv)
        config = from_args(args)
        api_url = getattr(args, "tui_api", None) or config.api_url
        api = client or NeoApiClient(api_url, timeout=config.timeout)
        result = handle(args, api)
        if args.command == "tui" and args.snapshot:
            print(result)
            return 0
        emit(result, json_output=args.json)
        return 0
    except SafetyDenied as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DENIED
    except ApiUnavailableError as exc:
        print(f"Neo API unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ApiError as exc:
        print(f"Neo API error ({exc.status}): {exc.detail}", file=sys.stderr)
        return EXIT_API
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
