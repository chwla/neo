"""Path comparison and validation that means the same thing on every platform.

Neo's safety rules are all path comparisons: is this folder a system directory,
is this file inside the workspace, is this a symlink pointing somewhere else.
Written naively those comparisons are subtly wrong on two of the three platforms
Neo runs on, and each way they are wrong is a way out of the sandbox:

* **Case.** Windows and macOS are case-insensitive by default, so ``/Users/me``
  and ``/users/me`` are the same directory -- but ``==`` on two ``Path`` objects
  says they are not. Every refusal written as an equality test is bypassed by
  typing a different case. ``os.path.normcase`` fixes this on Windows and is a
  no-op on macOS, so it is not enough on its own.

* **Roots.** ``PurePosixPath("C:/Windows")`` is not absolute, so a check that
  only asks ``is_absolute()`` accepts a Windows drive path as a repository
  *relative* one -- and joining it onto the root discards the root entirely.
  The same is true of ``C:foo`` (drive-relative) and ``\\\\server\\share``.

* **Reparse points.** A Windows *junction* is not a symlink: ``is_symlink()``
  returns False for one, so a check written only against symlinks lets a
  junction redirect writes anywhere on the volume.

Everything here is one implementation parameterised by platform rather than a
branch per operating system, so the Linux path and the Windows path cannot drift
apart. The platform arguments exist so the rules are testable from any host.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

IS_WINDOWS = os.name == "nt"

#: Whether the host's default filesystem ignores case. Windows and macOS do;
#: Linux does not. Folding case where the filesystem does not would let ``/a/B``
#: pass a containment check against ``/a/b``, which on Linux is a different
#: directory -- so this is deliberately not "fold everywhere".
CASE_INSENSITIVE_FS = IS_WINDOWS or sys.platform == "darwin"

#: Device names that Windows resolves before it ever reaches the filesystem, in
#: any directory and with any extension. ``NUL`` silently swallows writes and
#: ``COM1`` talks to a serial port, so neither is a file the agent may name.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

#: ``C:``, ``C:/x``, ``C:x``. The last is drive-*relative* and the most dangerous,
#: because it looks like an ordinary relative path to every POSIX-shaped check.
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def path_key(value: str | os.PathLike[str]) -> str:
    """A string two paths can be compared by, honouring the platform's rules.

    ``normcase`` alone is not enough: it lowercases on Windows but is the
    identity on macOS, whose default filesystem is case-insensitive too. Folding
    explicitly is what closes the macOS hole where ``/users/me/desktop`` slips
    past a refusal written against ``/Users/me/Desktop``.
    """

    text = os.path.normcase(os.fspath(value))
    return text.casefold() if CASE_INSENSITIVE_FS else text


def same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return path_key(left) == path_key(right)


def is_within(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` is ``root`` or sits underneath it.

    Compares by ``path_key`` rather than using ``Path.parents``, because
    ``parents`` compares components exactly and would answer "no" for a path the
    filesystem considers identical.
    """

    root_key = path_key(root)
    candidate_key = path_key(candidate)
    if candidate_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return candidate_key.startswith(prefix)


def is_reparse_point(path: Path) -> bool:
    """Whether following this path would leave for somewhere else.

    On POSIX that means a symlink. On Windows it also means a *junction*, which
    redirects exactly like a symlink but which ``is_symlink`` reports as False.
    """

    try:
        if path.is_symlink():
            return True
        return IS_WINDOWS and os.path.isjunction(path)
    except OSError:
        # An unreadable path is not something to assume is safe.
        return True


def split_roots(raw: str) -> list[str]:
    """Parse a configured list of directories.

    Split on ``os.pathsep`` -- ``;`` on Windows, ``:`` elsewhere -- which is how
    every platform already writes ``PATH``. A colon cannot be the separator
    everywhere: ``C:\\code`` would parse as ``C`` plus ``\\code``, silently
    turning a configured root into two nonexistent ones.
    """

    return [part.strip() for part in raw.split(os.pathsep) if part.strip()]


def has_windows_root(raw: str) -> bool:
    """Whether this text names a location rather than a path relative to one.

    Checked on every platform, not only Windows. A repository-relative path is
    never legitimately ``C:/x`` or ``\\\\server\\share``, and a Linux server that
    accepted one would be storing a path its own Windows client could later
    resolve outside the workspace.
    """

    text = raw.strip().replace("\\", "/")
    if _DRIVE_LETTER.match(text):
        return True
    if text.startswith("//"):  # UNC, and \\?\ device paths once normalised
        return True
    return bool(PureWindowsPath(raw.strip()).anchor)


def is_windows_reserved(component: str) -> bool:
    """Whether a single path component is unusable or misleading on Windows.

    Three things are refused, all of which make the name that reaches the
    filesystem differ from the name that was checked:

    * device names, which never reach the filesystem at all;
    * a trailing dot or space, which Windows strips -- so ``.git.`` would pass a
      check against ``.git`` and then open it;
    * an embedded colon, which opens an NTFS alternate data stream.
    """

    if not component:
        return False
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES or component.upper() in _WINDOWS_DEVICE_NAMES:
        return True
    if component != component.rstrip(". "):
        return True
    return ":" in component


def normalize_relative_parts(raw_path: str) -> tuple[str, ...]:
    """Turn user- or model-supplied text into safe relative path components.

    Raises ``ValueError`` for anything that is not a plain relative path. This is
    the single gate every workspace-relative path passes through, so the rules
    are stated once and cannot disagree between the file tools and the sandbox.
    """

    if not raw_path or not raw_path.strip():
        raise ValueError("A path is required.")
    text = raw_path.strip().replace("\\", "/")
    if text.startswith("/") or PurePosixPath(text).is_absolute():
        # Naming the fix matters: this message is fed back to the model as a tool
        # error, and an absolute path is its most common first guess.
        raise ValueError(
            "Path must be relative to the repository root -- pass '.' for the root, "
            "or 'src/main.py', not an absolute path."
        )
    if has_windows_root(raw_path):
        raise ValueError("Path must be relative to the repository root, with no drive or share.")
    parts = tuple(part for part in PurePosixPath(text).parts if part != ".")
    if ".." in parts:
        raise ValueError("Path traversal is not allowed.")
    if not parts:
        raise ValueError("Path must name a file.")
    for part in parts:
        if is_windows_reserved(part):
            raise ValueError(f"'{part}' is not a usable file name.")
    return parts


def system_roots(windows: bool = IS_WINDOWS) -> set[Path]:
    """Trees nothing may be registered from, matched against a path and its parents.

    On Windows the locations are read from the environment rather than hardcoded
    to ``C:``, because Windows is routinely installed on another drive and a
    hardcoded list would then guard nothing.
    """

    if not windows:
        return {
            Path(name)
            for name in (
                "/System", "/Library", "/usr", "/etc", "/var", "/bin", "/sbin",
                "/opt", "/Applications", "/proc", "/sys", "/dev", "/boot",
                "/lib", "/lib64", "/run", "/root", "/srv", "/snap",
            )
        }
    roots = set()
    for variable in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(variable)
        if value:
            roots.add(Path(value))
    system_drive = os.environ.get("SystemDrive") or "C:"
    for name in ("Windows", "Program Files", "Program Files (x86)", "ProgramData", "$Recycle.Bin"):
        roots.add(Path(f"{system_drive}\\{name}"))
    return roots


def account_containers(windows: bool = IS_WINDOWS) -> set[Path]:
    """Directories holding *every* account, matched exactly and never as parents.

    ``C:\\Users`` is not registrable but ``C:\\Users\\me\\code`` is, which is why
    these are kept apart from the system trees above.
    """

    if not windows:
        return {Path("/Users"), Path("/home"), Path("/mnt"), Path("/media")}
    system_drive = os.environ.get("SystemDrive") or "C:"
    return {Path(f"{system_drive}\\Users"), Path("/Users"), Path("/home")}


def in_container() -> bool:
    """Whether this process can only see a filesystem someone mounted for it.

    Used to turn "that folder does not exist" into the accurate "that folder is
    not mounted", which sends the user to the right fix. The environment
    override exists for runtimes that leave neither marker file.
    """

    override = os.environ.get("NEO_IN_CONTAINER", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
