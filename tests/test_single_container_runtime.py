import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.files.service import WorkspaceFilesService
from app.services.repos.service import RepoWorkspaceService
from app.services.repos.types import RepoRegisterRequest
from app.services.research.searcher import ResearchSearcher
from app.services.search.core import WebSearchService
from app.services.search.providers import ProviderRegistry


class SingleContainerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NEO_DATA_DIR"] = self.tmp.name
        os.environ["NEO_SEARCH_PROVIDER"] = "disabled"
        os.environ["NEO_FRONTEND_DIR"] = str(Path(self.tmp.name) / "missing-frontend")
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        for name in ("NEO_DATA_DIR", "NEO_SEARCH_PROVIDER", "NEO_SEARXNG_URL", "NEO_FRONTEND_DIR"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def test_data_dir_derives_database_and_workspace_paths(self):
        settings = get_settings()
        root = Path(self.tmp.name).resolve()
        create_app()

        self.assertEqual(settings.database_url, f"sqlite:///{root / 'neo.db'}")
        self.assertFalse(settings.web_search_enabled)
        self.assertEqual(Path(settings.workspace_files_dir), root / "workspace_files")
        self.assertEqual(Path(settings.workspace_repos_dir), root / "workspace_repos")

        file_item = WorkspaceFilesService().import_bytes(
            original_filename="runtime.txt", content=b"persistent runtime data"
        )
        self.assertTrue((root / "workspace_files").is_dir())
        self.assertTrue(Path(file_item["storage_path"]).is_relative_to(root))

        source = root / "source"
        source.mkdir()
        (source / "main.py").write_text("def runtime():\n    return True\n", encoding="utf-8")
        repo = RepoWorkspaceService().register(
            RepoRegisterRequest(path=str(source), name="runtime", confirm=True)
        )
        self.assertTrue(Path(repo["workspace_path"]).is_relative_to(root / "workspace_repos"))

    def test_disabled_provider_returns_clean_unavailable_without_network(self):
        provider = Mock()
        with patch("app.services.search.providers.requests.get") as request_get:
            response = ProviderRegistry().primary_provider().search("current news", 5)

        self.assertEqual(response.provider, "disabled")
        self.assertEqual(response.error, "Web search is disabled in this runtime.")
        request_get.assert_not_called()
        provider.search.assert_not_called()

    def test_search_api_and_research_degrade_when_disabled(self):
        client = TestClient(create_app())
        tested = client.post("/api/search/test", json={"query": "current news"})
        searched = client.post("/api/search", json={"query": "current news"})
        research = ResearchSearcher().search_query("current news")

        self.assertEqual(tested.status_code, 200)
        self.assertEqual(
            tested.json(),
            {
                "success": False,
                "available": False,
                "provider": "disabled",
                "provider_used": "disabled",
                "result_count": 0,
                "latency_ms": tested.json()["latency_ms"],
                "error": "Web search is disabled in this runtime.",
                "message": "Web search is disabled in this runtime.",
            },
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.json()["provider_used"], "disabled")
        self.assertIn("Web search is disabled", searched.json()["errors"][0])
        self.assertEqual(research.provider_used, "disabled")
        self.assertTrue(research.errors)

    def test_chat_search_service_keeps_non_search_prompts_local(self):
        context = WebSearchService().build_context("hello neo")

        self.assertFalse(context.needed)
        self.assertIn("no web trigger", (context.warning or "").lower())

    def test_external_searxng_unavailable_is_clean(self):
        os.environ["NEO_SEARCH_PROVIDER"] = "external_searxng"
        os.environ["NEO_SEARXNG_URL"] = "http://127.0.0.1:9"
        get_settings.cache_clear()

        with patch(
            "app.services.search.providers.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            response = ProviderRegistry().primary_provider().search("current news", 5)

        self.assertTrue(get_settings().web_search_enabled)
        self.assertEqual(response.provider, "external_searxng")
        self.assertEqual(response.error, "Configured SearXNG endpoint is unavailable.")

    def test_health_and_static_frontend(self):
        frontend = Path(self.tmp.name) / "frontend"
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("<main>Neo container</main>", encoding="utf-8")
        (frontend / "assets" / "app.js").write_text("window.NEO = true;", encoding="utf-8")
        os.environ["NEO_FRONTEND_DIR"] = str(frontend)
        get_settings.cache_clear()

        with patch("app.api.routes.health._ollama_available", return_value=True):
            client = TestClient(create_app())
            health = client.get("/api/health")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(health.json()["data_dir"], self.tmp.name)
        self.assertEqual(health.json()["search_provider"], "disabled")
        self.assertFalse(health.json()["search_available"])
        self.assertTrue(health.json()["ollama_available"])
        self.assertIn("Neo container", client.get("/").text)
        self.assertIn("Neo container", client.get("/projects/example").text)
        self.assertIn("window.NEO", client.get("/assets/app.js").text)
        self.assertEqual(client.get("/api/not-a-route").status_code, 404)


if __name__ == "__main__":
    unittest.main()
