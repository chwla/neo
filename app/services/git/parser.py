from __future__ import annotations


def parse_status(output: str) -> list[dict]:
    items = []
    labels = {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "U": "unmerged",
        "?": "untracked",
        "!": "ignored",
    }
    for line in output.splitlines():
        if len(line) < 3:
            continue
        index_status, worktree_status = line[0], line[1]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        code = worktree_status if worktree_status not in {" ", "?"} else index_status
        if index_status == "?" and worktree_status == "?":
            code = "?"
        items.append(
            {
                "path": path.strip('"'),
                "status": labels.get(code, "changed"),
                "staged": index_status not in {" ", "?"},
            }
        )
    return items


def parse_name_only(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]
