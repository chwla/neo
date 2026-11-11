"""Search preferences must load without recursively rebuilding settings."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.services import chat_prefs, search_prefs


class SearchPreferencesChecks(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="neo-search-prefs-")
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        self.base_url = f"sqlite:///{root / 'base.db'}"
        self.first_url = f"sqlite:///{root / 'first.db'}"
        self.second_url = f"sqlite:///{root / 'second.db'}"
        for url, provider in (
            (self.base_url, "disabled"),
            (self.first_url, "wikipedia"),
            (self.second_url, "duckduckgo"),
        ):
            with closing(sqlite3.connect(url.removeprefix("sqlite:///"))) as conn, conn:
                conn.execute(
                    "CREATE TABLE chat_preferences "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO chat_preferences VALUES (?, ?, ?)",
                    (search_prefs.PROVIDER_KEY, provider, "2026-11-10"),
                )
        defaults = config.Settings(
            _env_file=None,
            data_dir=str(root),
            database_url=self.base_url,
            web_search_provider="disabled",
        )
        self.base_patch = patch.object(config, "_base_settings", return_value=defaults)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.recursion_guard = patch.object(
            chat_prefs,
            "get_settings",
            side_effect=AssertionError("Preference storage must not rebuild settings"),
        )
        self.recursion_guard.start()
        self.addCleanup(self.recursion_guard.stop)
        token = config.active_profile_database_url.set(None)
        self.addCleanup(config.active_profile_database_url.reset, token)
        search_prefs.forget()
        self.addCleanup(search_prefs.forget)

    def test_search_settings_load_from_each_profile_without_recursion(self):
        for url, provider in (
            (self.first_url, "wikipedia"),
            (self.second_url, "duckduckgo"),
            (self.first_url, "wikipedia"),
        ):
            token = config.active_profile_database_url.set(url)
            try:
                settings = config.get_settings()
                self.assertEqual(settings.database_url, url)
                self.assertEqual(settings.web_search_provider, provider)
                self.assertTrue(settings.web_search_enabled)
            finally:
                config.active_profile_database_url.reset(token)

    def test_preference_writes_stay_in_active_profile_and_base_fallback_works(self):
        token = config.active_profile_database_url.set(self.first_url)
        try:
            chat_prefs.set_preference("restart_probe", "first-only")
            self.assertEqual(chat_prefs.get_preference("restart_probe"), "first-only")
        finally:
            config.active_profile_database_url.reset(token)
        token = config.active_profile_database_url.set(self.second_url)
        try:
            self.assertIsNone(chat_prefs.get_preference("restart_probe"))
        finally:
            config.active_profile_database_url.reset(token)
        self.assertEqual(chat_prefs._db_path(), self.base_url.removeprefix("sqlite:///"))
        self.assertIsNone(chat_prefs.get_preference("restart_probe"))


if __name__ == "__main__":
    unittest.main()
