from __future__ import annotations

from app.cli.tui.client import collect
from app.cli.tui.keys import KEYS
from app.cli.tui.render import snapshot
from app.cli.tui.state import TuiState
from app.cli.tui.views import VIEWS


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
    while True:
        print("\033[2J\033[H" + snapshot(state.view, collect(client)) + help_text)
        help_text = ""
        key = input("neo> ").strip().lower()
        if KEYS.get(key) == "quit":
            return 0
        if key in {"j", "k"}:
            state.selected = (state.selected + (1 if key == "j" else -1)) % len(VIEWS)
        elif key == "":
            state.view = VIEWS[state.selected]
        elif key == "r":
            continue
        elif key == "?":
            help_text = "\nq quit · r refresh · j/k select · Enter open · a approve"
        elif key in VIEWS:
            state.view = key
            state.selected = VIEWS.index(key)
        elif key == "a":
            pending = [
                item
                for item in collect(client)["commands"].get("runs", [])
                if item.get("status") == "proposed"
            ]
            if pending:
                client.post(
                    f"/api/command-sandbox/runs/{pending[0]['id']}/approve", {"confirm": True}
                )
