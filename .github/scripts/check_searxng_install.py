"""Neo's built-in SearXNG installs itself, on every platform, without touching Neo's packages.

* The commit Neo installs is the one the Docker image bakes in.
* The download is unpacked whole or not at all, without the development-only folders, and
  without file names Windows cannot hold (SearXNG's tree has some).
* SearXNG's packages go into its own folder with ``pip --target``, never Neo's environment,
  and that folder comes after Neo's on the import path.
* Neo installs it on first start when it is missing, unless told not to, and boots it after.
* SearXNG imports on Windows, which needs a stand-in for the Unix-only ``pwd`` module.
"""

import io
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.search import searxng_embedded, searxng_install

ROOT = Path(__file__).resolve().parents[2]


def archive(commit, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, text in files.items():
            zipped.writestr(f"searxng-{commit}/{name}", text)
    return buffer.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InstallChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "searxng"
        patcher = patch.object(searxng_embedded, "_source_dir", return_value=self.source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, files):
        payload = archive(searxng_install.SEARXNG_COMMIT, files)
        return patch.object(searxng_install.urllib.request, "urlopen",
                            return_value=Response(payload))

    def test_the_image_and_a_local_install_use_the_same_commit(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        pinned = re.search(r"ARG SEARXNG_COMMIT=([0-9a-f]{40})", dockerfile).group(1)
        self.assertEqual(pinned, searxng_install.SEARXNG_COMMIT)

    def test_download_unpacks_only_what_runs(self):
        files = {
            "searx/webapp.py": "app = None\n",
            "requirements.txt": "flask==3.1.3\n",
            "utils/templates/etc/searxng.conf:socket": "unix only\n",
            "docs/index.rst": "docs\n",
            "searx/odd:name.py": "never written\n",
        }
        (self.source / ".deps" / "kept").mkdir(parents=True)  # packages already installed
        with self.download(files):
            searxng_install.fetch_source(log=lambda _: None)
        self.assertTrue((self.source / "searx" / "webapp.py").is_file())
        self.assertFalse((self.source / "utils").exists())
        self.assertFalse((self.source / "docs").exists())
        self.assertFalse(any(":" in p.name for p in self.source.rglob("*")))
        self.assertEqual((self.source / searxng_install.MARKER).read_text(),
                         searxng_install.SEARXNG_COMMIT)
        self.assertTrue((self.source / "searx" / "version_frozen.py").is_file())
        self.assertTrue((self.source / ".deps" / "kept").is_dir())
        self.assertFalse(self.source.with_name("searxng.download").exists())

    def test_a_broken_download_leaves_no_tree_behind(self):
        with self.download({"README.md": "not searxng\n"}), self.assertRaises(RuntimeError):
            searxng_install.fetch_source(log=lambda _: None)
        self.assertFalse(self.source.exists())
        self.assertFalse(searxng_install.source_present())

    def test_packages_go_into_their_own_folder(self):
        with patch.object(searxng_install.subprocess, "run") as run:
            searxng_install.install_packages(log=lambda _: None)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(command[command.index("--target") + 1], str(self.source / ".deps"))
        self.assertEqual(command[command.index("-r") + 1], str(self.source / "requirements.txt"))

    def test_its_folder_comes_after_neos_own(self):
        (self.source / ".deps").mkdir(parents=True)
        deps = str(self.source / ".deps")
        self.addCleanup(lambda: deps in sys.path and sys.path.remove(deps))
        searxng_install.add_deps_to_path()
        self.assertEqual(sys.path[-1], deps)
        self.assertLess(sys.path.index(sys.prefix) if sys.prefix in sys.path else -1,
                        sys.path.index(deps))

    def test_what_counts_as_installed(self):
        self.assertTrue(searxng_install.needs_install())
        (self.source / "searx").mkdir(parents=True)
        (self.source / "searx" / "webapp.py").write_text("")
        with patch.object(searxng_install.importlib.util, "find_spec", return_value=None):
            self.assertTrue(searxng_install.needs_install())
        with patch.object(searxng_install.importlib.util, "find_spec", return_value=object()):
            self.assertFalse(searxng_install.needs_install())

    def test_background_install_boots_it_and_never_raises(self):
        with patch.object(searxng_install, "install") as install, \
                patch.object(searxng_embedded, "warm_up") as warm, \
                patch.object(searxng_embedded, "reset_for_tests") as reset:
            searxng_install.install_in_background().join(5)
        install.assert_called_once()
        reset.assert_called_once()
        warm.assert_called_once()
        with patch.object(searxng_install, "install", side_effect=OSError("offline")), \
                patch.object(searxng_embedded, "warm_up") as warm:
            searxng_install.install_in_background().join(5)
        warm.assert_not_called()


class StartupChecks(unittest.TestCase):
    def run_startup(self, *, provider="searxng", missing=True, auto=True):
        from app import main

        settings = SimpleNamespace(web_search_provider=provider, searxng_auto_install=auto)
        with patch.object(main, "get_settings", return_value=settings), \
                patch.object(searxng_install, "needs_install", return_value=missing), \
                patch.object(searxng_install, "install_in_background") as install, \
                patch.object(main.threading, "Thread") as thread:
            main._warm_embedded_searxng()
        return install.called, thread.called

    def test_first_start_installs_it(self):
        self.assertEqual(self.run_startup(), (True, False))

    def test_installed_it_is_only_warmed(self):
        self.assertEqual(self.run_startup(missing=False), (False, True))

    def test_auto_install_can_be_turned_off(self):
        self.assertEqual(self.run_startup(auto=False), (False, False))

    def test_another_provider_installs_nothing(self):
        self.assertEqual(self.run_startup(provider="duckduckgo"), (False, False))


class WindowsChecks(unittest.TestCase):
    def test_pwd_stand_in_on_windows_only(self):
        modules = {k: v for k, v in sys.modules.items() if k != "pwd"}
        with patch.dict(sys.modules, modules, clear=True), \
                patch.object(searxng_embedded.os, "name", "nt"):
            searxng_embedded._windows_compat()
            entry = sys.modules["pwd"].getpwuid(0)
            self.assertTrue(entry.pw_name)
            self.assertEqual(entry.pw_uid, 0)
        with patch.dict(sys.modules, modules, clear=True), \
                patch.object(searxng_embedded.os, "name", "posix"):
            searxng_embedded._windows_compat()
            self.assertNotIn("pwd", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
