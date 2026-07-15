from __future__ import annotations

from fastapi.testclient import TestClient

from app.cli.main import build_parser, handle_integration
from app.main import create_app
from app.services import integration
from app.services.context_memory import store as context_store
from app.services.memory_retrieval import store as memory_store
from app.services.provider_runtime import store as provider_store
from app.services.workspace_orchestration import WorkspaceService


class StubClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        return {"path": path}

    def post(self, path: str, body: dict | None = None):
        self.calls.append(("POST", path, body or {}))
        return {"path": path, "body": body or {}}


def test_integration_routes_and_report_payload():
    client = TestClient(create_app())
    smoke = client.post("/api/integration/smoke")
    assert smoke.status_code == 200
    status = client.get("/api/integration/status")
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["references"]["summary"]["checked"] >= 1
    assert "provider_runtime" in status_body["systems"]

    report = client.get("/api/integration/report")
    assert report.status_code == 200
    body = report.json()
    assert body["references"]["integration_map"]["workspace"]["workspace_link_supported"] is True
    assert body["systems"]["evaluation"]["builtin_core_integration_smoke"] is True
    assert isinstance(body["manual_review_pending"], list)


def test_workspace_links_and_continuity_reference_validation():
    client = TestClient(create_app())
    smoke = client.post("/api/integration/smoke").json()
    workspace_id = smoke["workspace_id"]
    bundle_id = smoke["bundle_id"]

    workspace_report = client.get(f"/api/workspaces/{workspace_id}/report")
    assert workspace_report.status_code == 200
    workspace_body = workspace_report.json()
    assert workspace_body["linked_entities"]
    assert workspace_body["integration_validation"]["summary"]["failed"] == 0

    continuity_report = client.get(f"/api/continuity/bundles/{bundle_id}/report")
    assert continuity_report.status_code == 200
    continuity_body = continuity_report.json()
    assert continuity_body["validation_summary"]["failed"] == 0
    assert "workspace_graph" in continuity_body["integration_payload"]


def test_reference_validation_detects_missing_target():
    workspace = WorkspaceService().create("Broken links", "Validate failures")
    WorkspaceService().link(workspace["id"], "provider_request", "missing-request")

    validation = integration.validate()
    failed = [item for item in validation["references"]["results"] if item["status"] == "failed"]
    assert failed
    assert any(item["category"] == "workspace_link" for item in failed)


def test_workspace_index_memory_is_idempotent():
    client = TestClient(create_app())
    workspace = WorkspaceService().create("Memory", "Index summary")
    WorkspaceService().event(
        workspace["id"],
        "decision",
        "Decision",
        "Keep things linked.",
    )
    first = client.post(f"/api/workspaces/{workspace['id']}/index-memory")
    second = client.post(f"/api/workspaces/{workspace['id']}/index-memory")
    assert first.status_code == 200
    assert second.status_code == 200
    items = client.get("/api/memory/items").json()["items"]
    titles = [item["title"] for item in items]
    assert len(titles) == len(set(titles))


def test_api_payloads_omit_secrets_and_absolute_paths():
    client = TestClient(create_app())
    request = provider_store.create_request(
        {
            "route_name": "chat",
            "provider_name": "fixture",
            "model_name": "fixture",
            "request_type": "chat",
            "status": "completed",
            "metadata": {"api_key": "secret-value", "path": "/Users/example/secret.txt"},
        }
    )
    summary = context_store.save_summary(
        {
            "scope_type": "workspace",
            "scope_id": "scope-1",
            "source_type": "provider_request",
            "source_id": request["id"],
            "summary_text": "Path /Users/example/secret.txt and api_key=abc should be redacted",
            "decisions": [],
            "constraints": [],
            "open_items": [],
            "completed_items": [],
            "files": [],
            "tests": [],
            "checkpoints": [],
            "safety_notes": [],
            "token_estimate_before": 10,
            "token_estimate_after": 5,
            "redaction_summary": {"redacted": True},
        }
    )
    memory_store.upsert_item(
        {
            "scope_type": "workspace",
            "scope_id": "scope-1",
            "source_type": "provider_request",
            "source_id": request["id"],
            "memory_type": "summary",
            "title": "secret memory",
            "content_text": "api_key=abc path /Users/example/secret.txt",
            "content_json": {},
            "tags": [],
            "importance": 3,
            "confidence": 1.0,
        }
    )
    body = client.get("/api/integration/report").json()
    dumped = str(body)
    assert "secret-value" not in dumped
    assert "/Users/example" not in dumped
    assert summary["id"]


def test_cli_integration_commands_dispatch():
    parser = build_parser()
    client = StubClient()

    args = parser.parse_args(["integration", "status"])
    assert handle_integration(args, client)["path"] == "/api/integration/status"

    args = parser.parse_args(["integration", "report"])
    assert handle_integration(args, client)["path"] == "/api/integration/report"

    args = parser.parse_args(["integration", "validate"])
    assert handle_integration(args, client)["path"] == "/api/integration/validate"

    args = parser.parse_args(["integration", "smoke"])
    assert handle_integration(args, client)["path"] == "/api/integration/smoke"


def test_core_integration_smoke_suite_exists():
    suites = integration.report()["systems"]["evaluation"]
    assert suites["builtin_core_integration_smoke"] is True
