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

import functools
import logging
import os
import re
import shutil
import stat
import subprocess
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
                "/System",
                "/Library",
                "/usr",
                "/etc",
                "/var",
                "/bin",
                "/sbin",
                "/opt",
                "/Applications",
                "/proc",
                "/sys",
                "/dev",
                "/boot",
                "/lib",
                "/lib64",
                "/run",
                "/root",
                "/srv",
                "/snap",
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


def install_root() -> Path:
    """The directory Neo is installed in.

    Relative storage defaults are anchored here rather than to the process's
    working directory. Resolving them against the cwd meant that starting Neo from
    another folder created a second set of databases there, each holding real
    profile data and none of it protected -- this repository still contains two
    such sets, from exactly that.
    """

    return Path(__file__).resolve().parents[2]


def private_dir_mode() -> int:
    """Permissions for a directory holding one account's private data.

    ``0o700`` where Neo owns its own storage. In a container the image deliberately
    keeps ``/app/data`` group-owned by 0 and group-writable so the container can run
    as an arbitrary uid against a bind mount, so group access is preserved there and
    only *other* is removed.
    """

    # The image supports an arbitrary runtime UID through group 0 on the data
    # mount. Without group write a profile created by one UID becomes unusable
    # when the container starts under another UID.
    return 0o770 if in_container() else 0o700


def private_file_mode() -> int:
    return 0o660 if in_container() else 0o600


#: Who private storage may let in on Windows besides the account itself: LocalSystem
#: and the local Administrators group.
_WINDOWS_ADMIN_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
#: OWNER RIGHTS, i.e. whoever owns the object. Python's ``mkdir(mode=0o700)`` grants it.
_OWNER_RIGHTS_SID = "S-1-3-4"
#: A protected DACL giving the account, SYSTEM and Administrators full control; on a
#: directory, inherited by everything created inside.
_PRIVATE_DIRECTORY_SDDL = "D:PAI(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
_PRIVATE_FILE_SDDL = "D:PAI(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1


@functools.cache
def windows_account_sid() -> str:
    """The SID of the Windows account Neo runs as."""

    completed = subprocess.run(  # noqa: S603 - fixed argv
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=False
    )
    fields = [field.strip().strip('"') for field in completed.stdout.strip().split('","')]
    sid = fields[-1].strip('"') if fields else ""
    if completed.returncode != 0 or not sid.startswith("S-1-"):
        raise OSError("Neo could not determine the current Windows account.")
    return sid


@functools.cache
def _win32():
    """ctypes, advapi32 and kernel32, with the prototypes the ACL helpers call."""

    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    out = ctypes.POINTER(ctypes.c_void_p)
    void = ctypes.c_void_p
    for function, restype, argtypes in (
        (
            advapi.GetNamedSecurityInfoW,
            wintypes.DWORD,
            (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, out, out, out, out, out),
        ),
        (
            advapi.SetNamedSecurityInfoW,
            wintypes.DWORD,
            (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, void, void, void, void),
        ),
        (advapi.ConvertSidToStringSidW, wintypes.BOOL, (void, ctypes.POINTER(ctypes.c_wchar_p))),
        (
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            wintypes.BOOL,
            (wintypes.LPCWSTR, wintypes.DWORD, out, void),
        ),
        (
            advapi.GetSecurityDescriptorDacl,
            wintypes.BOOL,
            (void, ctypes.POINTER(wintypes.BOOL), out, ctypes.POINTER(wintypes.BOOL)),
        ),
        (advapi.GetAclInformation, wintypes.BOOL, (void, void, wintypes.DWORD, ctypes.c_int)),
        (advapi.GetAce, wintypes.BOOL, (void, wintypes.DWORD, out)),
        (kernel.LocalFree, void, (void,)),
    ):
        function.restype = restype
        function.argtypes = argtypes
    return ctypes, advapi, kernel


def _sid_text(sid) -> str:
    ctypes, advapi, kernel = _win32()
    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value or ""
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _windows_dacl(path: Path) -> tuple[str, list[tuple[int, str | None]] | None]:
    """``path``'s owner and its DACL as (ACE type, trustee SID) pairs.

    ``None`` for the list is a null DACL, which lets everyone in. Trustees come back as
    SID strings: SDDL text names some accounts by alias (``LA`` for the built-in
    Administrator) and readable output is translated, so neither can be compared.
    """

    ctypes, advapi, kernel = _win32()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = advapi.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error, f"{ctypes.FormatError(error)}: {path}")
    try:
        owner_sid = _sid_text(owner) if owner.value else ""
        if not dacl.value:
            return owner_sid, None
        size = (ctypes.c_uint32 * 3)()  # ACL_SIZE_INFORMATION: AceCount, bytes in use, free
        if not advapi.GetAclInformation(dacl, size, ctypes.sizeof(size), _ACL_SIZE_INFORMATION):
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[tuple[int, str | None]] = []
        for index in range(size[0]):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            kind = ctypes.c_ubyte.from_address(ace.value).value
            # An allowed or denied ACE is a 4-byte header and a 4-byte mask, then the
            # SID. Other kinds (object, callback) are reported without a trustee.
            simple = kind in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE)
            entries.append((kind, _sid_text(ctypes.c_void_p(ace.value + 8)) if simple else None))
        return owner_sid, entries
    finally:
        kernel.LocalFree(descriptor)


def _windows_acl_is_private(path: Path) -> bool:
    """Whether only this account, SYSTEM and Administrators are allowed into ``path``.

    OWNER RIGHTS counts as the account when one of those owns ``path``, which is how a
    directory Python creates with ``mode=0o700`` is made private.
    """

    owner, entries = _windows_dacl(path)
    if entries is None:
        return False
    allowed = _WINDOWS_ADMIN_SIDS | {windows_account_sid()}
    if owner in allowed:
        allowed |= {_OWNER_RIGHTS_SID}
    granted = [sid for kind, sid in entries if kind != _ACCESS_DENIED_ACE_TYPE]
    return bool(granted) and all(sid in allowed for sid in granted)


def is_private(path: Path) -> bool:
    """Whether other accounts are kept out of ``path`` (a container's group aside).

    POSIX reads the mode's "other" bits. Windows reports 0o777 for every directory,
    whatever its ACL says, so there the ACL itself is read.
    """

    if IS_WINDOWS:
        return _windows_acl_is_private(path)
    return not path.stat().st_mode & 0o007


def restrict_to_owner(path: Path) -> None:
    """Keep other accounts out of a directory or file Neo owns.

    POSIX sets :func:`private_dir_mode` or :func:`private_file_mode`. Windows ignores
    mode bits, so an ACL that is not already private is replaced whole, explicit
    entries included, by a protected one giving this account, SYSTEM and
    Administrators full control. Inside a directory, whatever inherits its ACL is
    brought in line and what is created later inherits it; an entry with explicit
    grants of its own keeps them until it is restricted itself.
    """

    directory = path.is_dir()
    if not IS_WINDOWS:
        path.chmod(private_dir_mode() if directory else private_file_mode())
        return
    if _windows_acl_is_private(path):
        return
    ctypes, advapi, kernel = _win32()
    descriptor = ctypes.c_void_p()
    template = _PRIVATE_DIRECTORY_SDDL if directory else _PRIVATE_FILE_SDDL
    sddl = template.format(sid=windows_account_sid())
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = ctypes.c_int()
        dacl = ctypes.c_void_p()
        defaulted = ctypes.c_int()
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error, f"{ctypes.FormatError(error)}: {path}")
    finally:
        kernel.LocalFree(descriptor)


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


def _make_owner_writable(root: Path) -> None:
    """Give the owner write access throughout ``root`` so it can be deleted.

    Read-only entries are ordinary: git writes its objects read-only, and a tree
    copied from one is read-only too. On Windows that stops a delete outright; on
    POSIX a read-only directory stops removal of everything inside it. Links are
    never followed or changed -- only real files and directories under ``root``.
    """

    def loosen(path: str, *, directory: bool) -> None:
        if os.path.islink(path):
            return
        extra = stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR
        try:
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | extra)
        except OSError:
            pass

    loosen(str(root), directory=True)
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        for name in directories:
            loosen(os.path.join(current, name), directory=True)
        for name in files:
            loosen(os.path.join(current, name), directory=False)


def remove_tree(path: str | os.PathLike[str], *, ignore_errors: bool = False) -> bool:
    """Delete a directory tree, read-only entries included. Returns whether it is gone.

    A link (symlink or Windows junction) at ``path`` is removed itself and never
    followed. With ``ignore_errors`` a failure is logged rather than raised -- it
    used to vanish silently, which on a shared machine meant a guest's files could
    outlive the guest with nobody told.
    """

    target = Path(path)
    try:
        if target.is_symlink():
            target.unlink()
            return True
        if is_reparse_point(target):
            os.rmdir(target)  # a junction is removed like an empty directory
            return True
        try:
            shutil.rmtree(target)
        except PermissionError:
            _make_owner_writable(target)
            shutil.rmtree(target)
        return True
    except FileNotFoundError:
        if ignore_errors:
            return True
        raise
    except OSError:
        if not ignore_errors:
            raise
        logging.getLogger(__name__).warning("remove_tree_incomplete path=%s", target, exc_info=True)
        return False


#: MoveFileExW flags: replace an existing destination; return only once the move is on disk.
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def fsync_file(path: str | os.PathLike[str]) -> None:
    """Flush a file that was written earlier, and has since been closed, to disk.

    Windows flushes only through a handle with write access (a read-only one fails
    with EBADF), so the file is opened for writing there, and a read-only file is
    made writable for just as long as that takes. POSIX flushes through any handle.
    """

    if not IS_WINDOWS:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    mode = os.stat(path).st_mode
    read_only = not mode & stat.S_IWRITE
    if read_only:
        os.chmod(path, mode | stat.S_IWRITE)
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if read_only:
            os.chmod(path, stat.S_IMODE(mode))


def fsync_directory(path: str | os.PathLike[str]) -> None:
    """Commit the entries of directory ``path``, so a file created in it survives a crash.

    POSIX needs the directory itself fsynced for that. Windows can neither open a
    directory with ``os.open`` (EACCES) nor flush one, so this does nothing there,
    as in LevelDB and RocksDB: NTFS journals the change, so after a crash the entry
    is either present or absent, never damaged. A rename that has to be on disk when
    it returns goes through :func:`rename_durably`, which has a Windows equivalent.
    """

    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_durably(
    source: str | os.PathLike[str], destination: str | os.PathLike[str], *, replace: bool
) -> None:
    """Rename ``source`` to ``destination`` and return only once the rename is on disk.

    With ``replace`` an existing destination file is replaced, as by ``os.replace``;
    without it the rename fails if the destination exists on Windows, as by ``os.rename``.
    POSIX renames, then fsyncs the parent directory. Windows has no directory fsync, so
    it asks ``MoveFileExW`` to write the move through to disk before returning instead.
    """

    if not IS_WINDOWS:
        (os.replace if replace else os.rename)(source, destination)
        fsync_directory(Path(destination).parent)
        return
    import ctypes
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    flags = _MOVEFILE_WRITE_THROUGH | (_MOVEFILE_REPLACE_EXISTING if replace else 0)
    if not move(os.fspath(source), os.fspath(destination), flags):
        code = ctypes.get_last_error()
        # WinError builds the matching OSError subclass (PermissionError, FileExistsError...).
        raise ctypes.WinError(code, f"{ctypes.FormatError(code)}: {os.fspath(destination)!r}")
