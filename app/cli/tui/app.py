from __future__ import annotations

from app.cli.tui.client import collect
from app.cli.tui.keys import KEYS
from app.cli.tui.render import snapshot
from app.cli.tui.state import TuiState
from app.cli.tui.views import VIEWS
from app.services.isolation.launch import UNSANDBOXED_EXPOSURE

HELP = (
    "\nq quit · r refresh · j/k select · Enter open · a approve a proposed command "
    "(shown first, confirmed with y) · d deny it · b back to the dashboard"
)


def _proposed(client) -> list[dict]:
    return [
        item
        for item in collect(client)["commands"].get("runs", [])
        if item.get("status") == "proposed"
    ]


def _decide_first_proposed(client, *, approve: bool) -> str:
    """Approve or deny the first proposed command, after showing it and asking."""

    pending = _proposed(client)
    if not pending:
        return "\nNo proposed command is waiting."
    run = pending[0]
    command = " ".join(run.get("command", []))
    verb = "Approve" if approve else "Deny"
    print(f"\n{verb} this command in workspace {run.get('workspace_id', '?')}?\n  {command}")
    if approve:
        # Shown before the decision, as the interface shows it: approving here
        # records that the person saw what an unsandboxed run exposes.
        print(f"  {UNSANDBOXED_EXPOSURE}")
    answer = input(f"{verb}? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        return "\nLeft as proposed."
    if approve:
        client.post(
            f"/api/command-sandbox/runs/{run['id']}/approve",
            {"confirm": True, "acknowledge_unsandboxed": True},
        )
        return "\nApproved. Run it with `neo commands run`, or execute it from the interface."
    client.post(f"/api/command-sandbox/runs/{run['id']}/cancel", {})
    return "\nDenied."


def run_tui(client, *, snapshot_mode: bool = False, view: str = "dashboard") -> str | int:
    if view not in VIEWS:
        raise ValueError("Unknown TUI view.")
    data = collect(client)
    if data["health"].get("error"):
        raise RuntimeError("Neo server is unreachable: " + data["health"]["error"])
    rendered = snapshot(view, data)
    if snapshot_mode:
        return rendered
    state = TuiState(view=view, selected=VIEWS.index(view))
    help_text = ""
    try:
        while True:
            print("\033[2J\033[H" + snapshot(state.view, collect(client)) + help_text)
            help_text = ""
            key = input("neo> ").strip().lower()
            action = KEYS.get(key)
            if action == "quit":
                return 0
            if key in {"j", "k"}:
                state.selected = (state.selected + (1 if key == "j" else -1)) % len(VIEWS)
            elif key == "":
                state.view = VIEWS[state.selected]
            elif action == "refresh":
                continue
            elif action == "help":
                help_text = HELP
            elif action == "back":
                state.view = "dashboard"
                state.selected = VIEWS.index("dashboard")
            elif action == "detail":
                state.view = "commands"
                state.selected = VIEWS.index("commands")
            elif key in VIEWS:
                state.view = key
                state.selected = VIEWS.index(key)
            elif action in {"approve", "deny"}:
                help_text = _decide_first_proposed(client, approve=action == "approve")
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C, Ctrl+D or a closed stdin: leave quietly, the way a terminal
        # program is expected to.
        print()
        return 130
