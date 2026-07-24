from __future__ import annotations

import app.services.tasks.store as task_store
from app.services.github import store
from app.services.github.client import GitHubClient
from app.services.github.redaction import redact
from app.services.tasks import TaskCreate, TasksService


class GitHubService:
    def connections(self):
        return store.list_rows("workspace_github_connections")

    def create_connection(self, data):
        return store.save_connection(data)

    def update_connection(self, id, data):
        if not store.get_row("workspace_github_connections", id):
            raise LookupError("GitHub connection not found.")
        return store.save_connection(data, id)

    def disable_connection(self, id):
        if not store.get_row("workspace_github_connections", id):
            raise LookupError("GitHub connection not found.")
        return store.disable_connection(id)

    def health(self, id):
        con = self._connection(id)
        try:
            profile = GitHubClient(con["token_ref"]).get("/user")
            return store.operation(
                id, None, "health", "completed", response={"login": profile.get("login")}
            )
        except RuntimeError as exc:
            return store.operation(id, None, "health", "failed", error=str(exc))

    def import_item(self, id, number, kind):
        con = self._connection(id)
        endpoint = "issues" if kind == "issue" else "pulls"
        try:
            raw = GitHubClient(con["token_ref"]).get(
                f"/repos/{con['owner']}/{con['repo']}/{endpoint}/{number}"
            )
        except RuntimeError as exc:
            store.operation(id, None, f"import_{kind}", "failed", error=str(exc))
            raise
        item = store.save_item(
            {
                "connection_id": id,
                "item_type": kind,
                "github_number": number,
                "github_id": str(raw.get("id", "")),
                "title": raw.get("title") or f"{kind} #{number}",
                "state": raw.get("state"),
                "author": (raw.get("user") or {}).get("login"),
                "body_text": raw.get("body") or "",
                "labels": [x.get("name") for x in raw.get("labels", []) if x.get("name")],
                "url": raw.get("html_url"),
                "metadata": {
                    "changed_files": raw.get("changed_files"),
                    "comments": raw.get("comments"),
                },
            }
        )
        store.operation(id, item["id"], f"import_{kind}", "completed", response={"number": number})
        return item

    def create_task(self, item_id):
        item = self.item(item_id)
        if item["item_type"] != "issue":
            raise ValueError("Only imported issues can create tasks.")
        desc = (
            f"Imported from GitHub: {item.get('url') or ''}\n\n{item.get('body_text') or ''}"
        ).strip()
        task = TasksService().create_task(
            TaskCreate(title=item["title"], description=desc, tags=item.get("labels", []))
        )
        task_store.insert_link(
            {
                "task_id": task.id,
                "link_type": "github_item",
                "target_id": item_id,
                "target_url": item.get("url"),
                "title": f"GitHub issue #{item['github_number']}",
                "metadata": redact(
                    {"connection_id": item["connection_id"], "github_number": item["github_number"]}
                ),
            }
        )
        return store.update_item(item_id, imported_task_id=task.id), task.model_dump()

    def create_pr_draft(self, item_id, request):
        item = self.item(item_id)
        if not request.get("confirm"):
            raise ValueError("PR draft creation requires explicit confirmation.")
        return store.operation(
            item["connection_id"],
            item_id,
            "create_pr_draft",
            "blocked",
            request=request,
            error=(
                "Branch push is not supported. Neo will not create a remote draft PR until "
                "a branch already exists remotely and an approved write integration is enabled."
            ),
        )

    def items(self):
        return store.list_rows("workspace_github_items")

    def item(self, id):
        result = store.get_row("workspace_github_items", id)
        if not result:
            raise LookupError("GitHub item not found.")
        return result

    def operations(self):
        return store.list_rows("workspace_github_operations")

    def _connection(self, id):
        result = store.get_row("workspace_github_connections", id)
        if not result or not result["enabled"]:
            raise LookupError("GitHub connection not found or disabled.")
        return result
