"""Workspace containment and data-safety checks, run on each supported OS.

What these hold Neo to:

* a walk of the workspace (``glob``, ``grep``, ``list_dir``) never lists or reads
  anything behind a symlink or a Windows junction;
* a write that fails leaves the bytes that were there;
* editing a file that is not UTF-8 changes only what was edited;
* undo, checkpoints and reviewed writes (the anchored operations) work, and refuse
  every path that would leave the folder;
* folders that hold credentials or another application's private data are refused
  before Neo looks inside them.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import textio
from app.core.fs_errors import permission_message
from app.services.agent_core.tools.base import ToolContext
from app.services.agent_core.tools.registry import ToolRegistry
from app.services.agent_core.types import ToolCall
from app.services.isolation import fs_ops, safe_fs
from app.services.repos import safety

WINDOWS = os.name == "nt"


def make_directory_link(link: Path, target: Path) -> bool:
    """A junction on Windows (no privilege needed), a symlink elsewhere."""

    if WINDOWS:
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        return done.returncode == 0
    link.symlink_to(target, target_is_directory=True)
    return True


class Workspace(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.base = Path(self._scratch.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("SECRET-OUTSIDE", encoding="utf-8")
        self.registry = ToolRegistry()
        for target, value in (("repo_root", self.root), ("is_live", False)):
            patcher = patch(f"app.services.agent_core.tools.files.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, name, **arguments):
        return self.registry.execute(
            ToolCall(id="check", name=name, arguments=arguments),
            ToolContext(session_id="check", repo_id="folder"),
        )


class ContainmentChecks(Workspace):
    def test_walks_never_follow_a_directory_link(self):
        (self.root / "inside.txt").write_text("SECRET-OUTSIDE is not here", encoding="utf-8")
        self.assertTrue(make_directory_link(self.root / "jlink", self.outside))
        self.assertTrue((self.root / "jlink" / "secret.txt").exists())  # the link works

        grep = self.call("grep", pattern="SECRET-OUTSIDE")
        self.assertEqual(grep.status, "ok")
        self.assertIn("inside.txt", grep.content)
        self.assertNotIn("jlink", grep.content)

        glob = self.call("glob", pattern="*.txt")
        self.assertNotIn("jlink", glob.content)
        self.assertNotIn("secret", self.call("glob", pattern="**/*.txt").content)
        self.assertNotIn("jlink", self.call("list_dir", path=".").content)

        for name, arguments in (
            ("read_file", {"path": "jlink/secret.txt"}),
            ("list_dir", {"path": "jlink"}),
            ("write_file", {"path": "jlink/pwned.txt", "content": "x"}),
            ("create_directory", {"path": "jlink/newdir"}),
        ):
            with self.subTest(tool=name):
                self.assertEqual(self.call(name, **arguments).status, "error")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])

    @unittest.skipIf(WINDOWS, "file symlinks need a privilege on Windows")
    def test_walks_skip_a_file_symlink(self):
        (self.root / "link.txt").symlink_to(self.outside / "secret.txt")
        self.assertNotIn("link.txt", self.call("glob", pattern="*.txt").content)
        self.assertEqual(self.call("grep", pattern="SECRET").content, "(no matches)")

    def test_anchored_operations_refuse_every_way_out(self):
        self.assertTrue(safe_fs.supported())
        self.assertTrue(make_directory_link(self.root / "jlink", self.outside))
        for operation in (
            lambda: safe_fs.read_file(self.root, "jlink/secret.txt"),
            lambda: safe_fs.write_file(self.root, "jlink/pwned.txt", b"x"),
            lambda: safe_fs.delete(self.root, "jlink/secret.txt"),
            lambda: safe_fs.makedirs(self.root, "jlink/new"),
            lambda: safe_fs.write_file(self.root, "../escape.txt", b"x"),
            lambda: safe_fs.write_file(self.root, str(self.outside / "abs.txt"), b"x"),
        ):
            with self.assertRaises(safe_fs.UnsafePath):
                operation()
        self.assertFalse(safe_fs.exists(self.root, "jlink/secret.txt"))
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])
        # A root that is itself reached through a link is refused as well.
        self.assertTrue(make_directory_link(self.base / "linked-root", self.root))
        with self.assertRaises(safe_fs.UnsafePath):
            safe_fs.write_file(self.base / "linked-root", "x.txt", b"x")
        self.assertFalse((self.root / "x.txt").exists())


class DataSafetyChecks(Workspace):
    def test_anchored_write_is_atomic_and_reviewed(self):
        safe_fs.write_file(self.root, "a/b/file.txt", b"one")
        self.assertEqual(safe_fs.read_file(self.root, "a/b/file.txt"), b"one")
        fs_ops.apply(self.root, "a/b/file.txt", replacement=b"two", expected=b"one")
        with self.assertRaises(safe_fs.UnsafePath):
            # The reviewed version is no longer what is on disk.
            fs_ops.apply(self.root, "a/b/file.txt", replacement=b"three", expected=b"one")
        self.assertEqual((self.root / "a/b/file.txt").read_bytes(), b"two")
        fs_ops.apply(self.root, "a/b/file.txt", replacement=None, expected=b"two")
        self.assertFalse((self.root / "a/b/file.txt").exists())
        self.assertEqual(list((self.root / "a/b").iterdir()), [])  # no temporary file left

    def test_hard_linked_target_is_refused(self):
        (self.root / "original.txt").write_bytes(b"original")
        os.link(self.root / "original.txt", self.root / "second-name.txt")
        with self.assertRaises(safe_fs.UnsafePath):
            safe_fs.write_file(self.root, "second-name.txt", b"changed")
        self.assertEqual((self.root / "original.txt").read_bytes(), b"original")

    def test_refused_write_leaves_the_original_bytes(self):
        target = self.root / "readonly.txt"
        target.write_bytes(b"keep me")
        os.chmod(target, 0o444)
        self.addCleanup(os.chmod, target, 0o644)
        result = self.call("write_file", path="readonly.txt", content="overwritten")
        self.assertEqual(result.status, "error")
        self.assertEqual(target.read_bytes(), b"keep me")
        self.assertNotIn("NEO_UID", result.error or result.content)
        self.assertEqual([p.name for p in self.root.iterdir()], ["readonly.txt"])

    @unittest.skipUnless(WINDOWS, "mandatory file locks are a Windows behaviour")
    def test_write_to_a_file_open_elsewhere_keeps_its_content(self):
        target = self.root / "locked.txt"
        target.write_bytes(b"locked")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; f=open(sys.argv[1],'r+b'); print('held',flush=True); time.sleep(60)",
             str(target)],
            stdout=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"held")
            result = self.call("write_file", path="locked.txt", content="overwritten")
            self.assertEqual(result.status, "error")
            self.assertIn("another program", result.error or result.content)
            self.assertEqual(target.read_bytes(), b"locked")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_editing_a_cp1252_file_preserves_every_other_byte(self):
        target = self.root / "latin1.txt"
        target.write_bytes(b"caf\xe9 na\xefve\r\nsecond line\r\n")
        result = self.call("edit_file", path="latin1.txt", old_string="second", new_string="SECOND")
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(target.read_bytes(), b"caf\xe9 na\xefve\r\nSECOND line\r\n")
        # Text the file's encoding cannot hold is refused, not written as UTF-8.
        refused = self.call(
            "edit_file", path="latin1.txt", old_string="SECOND", new_string="中文"
        )
        self.assertEqual(refused.status, "error")
        self.assertEqual(target.read_bytes(), b"caf\xe9 na\xefve\r\nSECOND line\r\n")

    def test_utf8_bom_and_line_endings_survive_an_edit(self):
        target = self.root / "bom.txt"
        target.write_bytes(b"\xef\xbb\xbfone\r\ntwo \xe2\x82\xac\r\n")
        self.assertEqual(
            self.call("edit_file", path="bom.txt", old_string="one", new_string="ONE").status, "ok"
        )
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfONE\r\ntwo \xe2\x82\xac\r\n")

    def test_binary_file_is_not_edited(self):
        target = self.root / "blob.bin"
        target.write_bytes(b"\x00\x01abc\xff")
        result = self.call("edit_file", path="blob.bin", old_string="abc", new_string="xyz")
        self.assertEqual(result.status, "error")
        self.assertEqual(target.read_bytes(), b"\x00\x01abc\xff")

    def test_encoding_detection_round_trips(self):
        for raw in (b"plain", "héllo".encode(), b"caf\xe9", b"\x81\x8d"):
            encoding = textio.detect_encoding(raw)
            self.assertEqual(raw.decode(encoding).encode(encoding), raw)

    def test_permission_message_matches_the_platform(self):
        with patch("app.core.fs_errors.in_container", return_value=False):
            self.assertNotIn("NEO_UID", permission_message("file.txt"))
        with patch("app.core.fs_errors.in_container", return_value=True):
            self.assertIn("NEO_UID", permission_message("file.txt"))


class FolderDenylistChecks(unittest.TestCase):
    def refused(self, path: Path) -> str:
        with patch("os.scandir", side_effect=AssertionError("walked a refused folder")):
            with self.assertRaises(ValueError) as caught:
                safety.validate_repo_root(str(path))
        return str(caught.exception)

    @unittest.skipUnless(WINDOWS, "Windows application-data folders")
    def test_windows_application_data_is_refused_without_a_walk(self):
        for variable in ("APPDATA", "LOCALAPPDATA"):
            base = Path(os.environ[variable])
            self.assertIn("private data", self.refused(base))
            child = next((entry for entry in base.iterdir() if entry.is_dir()), None)
            if child is not None:
                self.assertIn("private data", self.refused(child))
        self.assertIn("private data", self.refused(Path.home() / "AppData"))

    def test_private_application_roots_follow_the_environment(self):
        with tempfile.TemporaryDirectory() as scratch:
            roaming = Path(scratch).resolve() / "Roaming"
            (roaming / "Vendor" / "Profile").mkdir(parents=True)
            roots = safety._private_app_roots({"APPDATA": str(roaming)})
            self.assertEqual(roots, [roaming])
            with patch.object(safety, "_private_app_roots", return_value=roots):
                self.assertEqual(
                    safety._sensitive_overlap(roaming / "Vendor" / "Profile"), str(roaming)
                )

    @unittest.skipIf(WINDOWS, "POSIX configuration folders")
    def test_xdg_folders_are_refused(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as scratch:
            home = Path(scratch).resolve()
            for name in (".config", ".gnupg", ".kube", ".docker", ".local/share/keyrings"):
                (home / name / "child").mkdir(parents=True)
            with patch.object(Path, "home", return_value=home):
                for name in (".config", ".config/child", ".gnupg", ".kube", ".docker"):
                    with self.subTest(folder=name):
                        self.assertIsNotNone(safety._sensitive_overlap(home / name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
