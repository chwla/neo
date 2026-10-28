from __future__ import annotations

from urllib.parse import quote

import app.services.tasks.store as task_store
from app.services.disclosure import (
    Destination,
    GrantStore,
    Label,
    Labeled,
    content_hash,
    from_json,
    join,
    label_of,
    require,
    to_json,
)
from app.services.github import store
from app.services.github.client import GitHubClient
from app.services.github.redaction import redact
from app.services.integrations.vault import scoped_aad
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.labels import raise_task_label


class GitHubService:
    @staticmethod
    def _public_connection(connection):
        return {
            **connection,
            "credential_configured": store.credential_configured(connection["id"]),
        }

    def connections(self):
        return [
            self._public_connection(row) for row in store.list_rows("workspace_github_connections")
        ]

    @staticmethod
    def _connection_input(data, *, required=False):
        values = dict(data)
        token = values.pop("token", None)
        if hasattr(token, "get_secret_value"):
            token = token.get_secret_value()
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ValueError("Enter a GitHub access token.")
        if required and token is None:
            raise ValueError("Enter a GitHub access token for this profile.")
        # Legacy environment references are retained in old rows for migration
        # diagnostics, but cannot be selected or activated by API input.
        values.pop("token_ref", None)
        return values, token

    def create_connection(self, data):
        values, token = self._connection_input(data, required=True)
        return self._public_connection(store.save_connection(values, token_secret=token))

    def update_connection(self, id, data):
        if not store.get_row("workspace_github_connections", id):
            raise LookupError("GitHub connection not found.")
        values, token = self._connection_input(data)
        return self._public_connection(store.save_connection(values, id, token_secret=token))

    def disable_connection(self, id):
        if not store.get_row("workspace_github_connections", id):
            raise LookupError("GitHub connection not found.")
        return store.disable_connection(id)

    def _read(self, con, path, expected_intent, intent):
        destination = Destination(scoped_aad("github"), engine="github", thread_id=con["id"])
        if not isinstance(intent, Labeled) or intent.value != expected_intent:
            raise RuntimeError("This GitHub read requires an explicit user request.")
        require("connector", intent, destination)
        client = GitHubClient(con["id"], token=store.read_credential(con["id"]))
        selected_path = Labeled(path, label_of(intent))
        payload = client.payload(selected_path)
        grants = GrantStore()
        # The user's selected connection/read authorizes use of that connection's
        # credential for this exact request, not arbitrary model-derived content.
        grants.create(
            profile_id=destination.profile_id,
            engines=frozenset({"github"}),
            thread_scope=con["id"],
            tags=payload.label.tags,
            content_hash=content_hash(payload),
            sink="http",
            ttl_seconds=30,
        )
        result = client.get(selected_path, destination=destination, grants=grants)
        return Labeled(
            result.value, join(result.label, Label.private(f"connector:github:{con['id']}"))
        )

    def health(self, id, *, intent: Labeled | None = None):
        con = self._connection(id)
        try:
            profile = self._read(con, "/user", {"connection_id": id, "operation": "health"}, intent)
            profile = profile.value
            return store.operation(
                id, None, "health", "completed", response={"login": profile.get("login")}
            )
        except RuntimeError as exc:
            return store.operation(id, None, "health", "failed", error=str(exc))

    def import_item(self, id, number, kind, *, intent: Labeled | None = None):
        con = self._connection(id)
        if kind not in {"issue", "pr"} or not isinstance(number, int) or number < 1:
            raise ValueError("Choose a valid GitHub issue or pull request number.")
        endpoint = "issues" if kind == "issue" else "pulls"
        try:
            path = (
                f"/repos/{quote(con['owner'], safe='')}/{quote(con['repo'], safe='')}/"
                f"{endpoint}/{number}"
            )
            result = self._read(
                con,
                path,
                {
                    "connection_id": id,
                    "operation": f"import_{kind}",
                    "number": number,
                },
                intent,
            )
            raw = result.value
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
                    "source_label_json": to_json(result.label),
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
        # The link is written before the label record, deliberately. A crash between
        # them leaves a task linked to an imported item with no record, which reads
        # as UNKNOWN -- the strictest outcome -- instead of an unlinked task that
        # would read as an ordinary private one and lose the issue's provenance.
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
        # The task's text is the issue's text, so it carries the issue's label: the
        # connector and credential it was read under. An issue imported before
        # labels existed has no record and makes the task UNKNOWN, not public.
        metadata = item.get("metadata") or {}
        stored = metadata.get("source_label_json") if isinstance(metadata, dict) else None
        raise_task_label(
            task.id,
            from_json(stored) if stored else Label.unknown("legacy_github_item"),
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
