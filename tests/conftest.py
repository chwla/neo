from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "index.html").write_text("<html><body>Neo test</body></html>", encoding="utf-8")
    monkeypatch.setenv("NEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NEO_DATABASE_URL", f"sqlite:///{tmp_path / 'neo.db'}")
    monkeypatch.setenv("NEO_FRONTEND_DIR", str(frontend_dir))
    monkeypatch.setenv("NEO_SEARCH_PROVIDER", "disabled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
