"""Filesystem writes that cannot be redirected out of the tree they target.

Undo, restore and delivery all write to paths inside a workspace an agent has just
been editing, so every component of those paths is attacker-chosen. Two standard
approaches are not enough here:

* ``O_NOFOLLOW`` on the final component misses a *parent* that is a symlink, which
  is the easier attack to set up -- ``escape/private.db`` where ``escape`` points
  somewhere else entirely.
* ``realpath`` then write is a check against a path, and the path can change in
  between. The window is small and entirely winnable: a loop that swaps a directory
  for a symlink thousands of times a second is the normal way this is exploited.

So the walk here is anchored. Each component is opened relative to the directory
descriptor of the one before it, with ``O_NOFOLLOW`` and ``O_DIRECTORY``, which means
the kernel -- not a prior check -- enforces that nothing on the way is a link. The
final file is opened from that descriptor, and the object that is actually open is
inspected (type, link count, device) before a single byte is written. Nothing is
decided by a name.

Writes are atomic: content goes to a temporary file in the same directory and is
renamed over the target, so a crash or a refusal never leaves a half-written file.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path, PurePosixPath

#: Components that can never be part of a safe relative path.
_REFUSED_PARTS = {"", ".", ".."}


class UnsafePath(OSError):
    """The path could not be used without risking a write outside the tree."""


class PlatformUnsupported(UnsafePath, ValueError):
    """This platform cannot do anchored operations at all.

    Also a ``ValueError`` -- as ``io.UnsupportedOperation`` is both -- so every route
    that turns a ``ValueError`` into a 400 reports the reason instead of a 500.
    """


#: Windows has no ``openat`` -- ``dir_fd``, ``O_DIRECTORY`` and ``O_NOFOLLOW`` do not
#: exist there -- but it has the same primitive: a handle-relative open that does
#: not follow links. ``_safe_fs_windows`` builds the walk from that, and every
#: operation below hands over to it. Only if those entry points cannot be loaded
#: at all is the platform refused, up front, with a reason a person can act on --
#: never a weaker write by path presented as this one.
_WINDOWS = os.name == "nt"
_windows = None
if _WINDOWS:
    try:
        from app.services.isolation import _safe_fs_windows as _windows

        _windows._load()
    except Exception:  # noqa: BLE001 - any failure here means "cannot anchor"
        _windows = None
_SUPPORTED = not _WINDOWS or _windows is not None
PLATFORM_UNSUPPORTED_MESSAGE = (
    "Undo, patch apply, delivery and checkpoints are not available on this computer: "
    "Neo could not load the Windows file operations that keep a restoring write "
    "inside the folder."
)


def supported() -> bool:
    """Whether anchored in-tree operations work on this platform."""

    return _SUPPORTED


def _relative_parts(relative: str | os.PathLike[str]) -> tuple[str, ...]:
    text = str(relative).replace("\\", "/")
    if text.startswith("/"):
        raise UnsafePath(f"{text!r} is absolute; only paths inside the tree are allowed")
    parts = tuple(part for part in PurePosixPath(text).parts)
    if not parts:
        raise UnsafePath("an empty path cannot be written")
    for part in parts:
        if part in _REFUSED_PARTS:
            raise UnsafePath(f"{text!r} contains {part!r}, which can leave the tree")
    return parts


class _WindowsWalk:
    """Open handles for a path's directories on Windows, closed on the way out.

    The same shape as :class:`_Walk`, with handles in place of descriptors. A
    refusal from the handle layer arrives as a ``ValueError`` and leaves as
    :class:`UnsafePath`; a missing directory stays a ``FileNotFoundError``.
    """

    def __init__(self, root, parts: tuple[str, ...], *, create: bool):
        self._root = root
        self._parts = parts
        self._create = create
        self._handles: list[int] = []

    def __enter__(self) -> tuple[int, str]:
        if _windows is None:
            raise PlatformUnsupported(PLATFORM_UNSUPPORTED_MESSAGE)
        if isinstance(self._root, int):
            raise UnsafePath("a tree root must be named by its path on Windows")
        try:
            handle = _windows.open_root(self._root)
            self._handles.append(handle)
            for name in self._parts[:-1]:
                handle = _windows.open_dir(handle, name, create=self._create)
                self._handles.append(handle)
            _windows.check_name(self._parts[-1])
            return handle, self._parts[-1]
        except BaseException as exc:
            self.__exit__(None, None, None)
            self._handles.clear()
            raise _windows_refusal(exc) from exc

    def __exit__(self, *exc_info) -> None:
        for handle in reversed(self._handles):
            _windows.close(handle)


def _windows_refusal(exc: BaseException) -> BaseException:
    """Translate the handle layer's refusals into this module's exceptions."""

    if isinstance(exc, UnsafePath) or not isinstance(exc, Exception):
        return exc
    if isinstance(exc, ValueError):
        return UnsafePath(str(exc))
    if isinstance(exc, FileNotFoundError | PermissionError):
        return exc
    if isinstance(exc, OSError):
        return UnsafePath(f"the path could not be opened: {exc}")
    return exc


def _windows_plain_file(info, name: str) -> None:
    """Refuse a target that is not a plain, unshared file."""

    if info.is_reparse_point:
        raise UnsafePath(f"{name!r} is a link or junction; writing it would follow it out")
    if info.is_directory:
        raise UnsafePath(f"{name!r} is a directory, not a file")
    if info.links != 1:
        raise UnsafePath(
            f"{name!r} has {info.links} hard links, so writing it would also write "
            "whatever else points at the same data"
        )


def _open_root(root: int | str | os.PathLike[str]) -> int:
    if not _SUPPORTED:
        raise PlatformUnsupported(PLATFORM_UNSUPPORTED_MESSAGE)
    if isinstance(root, int):
        fd = os.dup(root)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise UnsafePath("the supplied root descriptor is not a directory")
        except BaseException:
            os.close(fd)
            raise
        return fd
    path = Path(root)
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafePath("the tree root must be an absolute path without traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open("/", flags)
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            finally:
                os.close(fd)
            fd = child
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise UnsafePath("the tree root is not a directory")
        except BaseException:
            os.close(fd)
            raise
        return fd
    except OSError as exc:
        raise UnsafePath(f"the tree root {root} could not be opened: {exc}") from exc


def root_identity(root: int | str | os.PathLike[str]) -> tuple[int, int]:
    """Device and inode of an anchored root, for cross-process binding."""

    if _WINDOWS:
        with _WindowsWalk(root, ("-",), create=False) as (handle, _name):
            info = _windows._info(handle)
            return info.volume, info.index
    fd = _open_root(root)
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _open_child_dir(parent_fd: int, name: str, *, create: bool) -> int:
    """Open ``name`` under ``parent_fd`` as a directory, never following a link."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafePath(f"{name!r} is a symbolic link, so it may leave the tree") from exc
        if exc.errno == errno.ENOTDIR:
            raise UnsafePath(f"{name!r} is not a directory") from exc
        if exc.errno == errno.ENOENT and not create:
            raise FileNotFoundError(name) from exc
        if exc.errno != errno.ENOENT or not create:
            raise UnsafePath(f"{name!r} could not be opened: {exc}") from exc
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        # Another writer created it; fall through and open what is there now.
        pass
    except OSError as exc:
        raise UnsafePath(f"{name!r} could not be created: {exc}") from exc
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise UnsafePath(f"{name!r} could not be opened after creation: {exc}") from exc


class _Walk:
    """Open descriptors for a path's directories, closing them on the way out."""

    def __init__(self, root: str | os.PathLike[str], parts: tuple[str, ...], *, create: bool):
        self._root = root
        self._parts = parts
        self._create = create
        self._fds: list[int] = []

    def __enter__(self) -> tuple[int, str]:
        # A refusal part-way down raises from here, and Python never calls
        # ``__exit__`` for a context that did not enter -- so whatever was opened
        # must be closed before re-raising. Without this every refused path leaked
        # its descriptors, and refusing is exactly what this walk exists to do.
        try:
            fd = _open_root(self._root)
            self._fds.append(fd)
            for name in self._parts[:-1]:
                fd = _open_child_dir(fd, name, create=self._create)
                self._fds.append(fd)
            return fd, self._parts[-1]
        except BaseException:
            self.__exit__(None, None, None)
            self._fds.clear()
            raise

    def __exit__(self, *exc_info) -> None:
        for fd in reversed(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _check_target(dir_fd: int, name: str) -> os.stat_result | None:
    """Refuse a target that is not a plain, unshared file -- if it exists at all."""

    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafePath(f"{name!r} could not be inspected: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePath(f"{name!r} is a symbolic link; writing it would follow it out")
    if stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"{name!r} is a directory, not a file")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath(
            f"{name!r} is not a regular file (it is a device, socket or pipe)"
        )
    if info.st_nlink != 1:
        raise UnsafePath(
            f"{name!r} has {info.st_nlink} hard links, so writing it would also write "
            "whatever else points at the same data"
        )
    return info


def write_file(
    root: str | os.PathLike[str],
    relative: str | os.PathLike[str],
    data: bytes,
    *,
    create_parents: bool = True,
    permission_mode: int | None = None,
) -> None:
    """Write ``data`` to ``relative`` inside ``root``, atomically and anchored.

    The existing file is never opened for writing: content is written to a new
    temporary file in the same directory and renamed over the target. An existing
    inode is therefore never truncated, which is what keeps a hardlinked or
    unexpectedly-shared target from being damaged before it is noticed.
    """

    parts = _relative_parts(relative)
    if _WINDOWS:
        # ``permission_mode`` has no meaning here: Windows files carry no mode
        # bits, and the new file inherits the folder's access list as any other.
        with _WindowsWalk(root, parts, create=create_parents) as (handle, name):
            try:
                before = _windows.inspect(handle, name)
                if before is not None:
                    _windows_plain_file(before, name)
                _windows.write(handle, name, data, unchanged_since=before)
            except Exception as exc:
                raise _windows_refusal(exc) from exc
        return
    with _Walk(root, parts, create=create_parents) as (dir_fd, name):
        before = _check_target(dir_fd, name)
        temporary = f".neo-write-{secrets.token_hex(6)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("anchored write made no progress")
                    remaining = remaining[written:]
                if permission_mode is not None:
                    if permission_mode not in {0o600, 0o644, 0o755}:
                        raise UnsafePath("unsupported restored file mode")
                    os.fchmod(fd, permission_mode)
                elif before is not None:
                    os.fchmod(fd, stat.S_IMODE(before.st_mode))
                os.fsync(fd)
            finally:
                os.close(fd)
            # Re-checked immediately before the rename: the target may have been
            # swapped while the temporary file was being written.
            after = _check_target(dir_fd, name)
            if (before is None) != (after is None) or (
                before is not None
                and after is not None
                and (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise UnsafePath("the target changed during the anchored write")
            os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        try:
            os.fsync(dir_fd)
        except OSError:
            pass


def write_text(
    root: str | os.PathLike[str],
    relative: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> None:
    """``write_file`` for text, preserving the caller's line ending exactly."""

    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        body = body.replace("\n", newline)
    write_file(root, relative, body.encode(encoding))


def read_file(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> bytes:
    """Read a file through the same anchored walk, refusing links on the way."""

    parts = _relative_parts(relative)
    if _WINDOWS:
        with _WindowsWalk(root, parts, create=False) as (handle, name):
            try:
                data, info = _windows.read(handle, name)
            except Exception as exc:
                raise _windows_refusal(exc) from exc
            # Decided from the handle that was read, so there is no gap between
            # checking the file and opening it.
            _windows_plain_file(info, name)
            return data
    with _Walk(root, parts, create=False) as (dir_fd, name):
        before = _check_target(dir_fd, name)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            opened = os.fstat(fd)
            if (
                before is None
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise UnsafePath("the target changed while being opened for an anchored read")
            chunks = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


def file_mode(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> int:
    """Return a regular file's mode through an anchored directory walk."""

    parts = _relative_parts(relative)
    if _WINDOWS:
        with _WindowsWalk(root, parts, create=False) as (handle, name):
            found = _windows.inspect(handle, name)
            if found is None:
                raise FileNotFoundError(name)
            _windows_plain_file(found, name)
            # No mode bits on Windows; the one every restore accepts.
            return 0o644
    with _Walk(root, parts, create=False) as (dir_fd, name):
        info = _check_target(dir_fd, name)
        if info is None:
            raise FileNotFoundError(name)
        return stat.S_IMODE(info.st_mode)


def delete(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> None:
    """Remove a file, refusing anything that is not a plain file in this tree."""

    parts = _relative_parts(relative)
    if _WINDOWS:
        with _WindowsWalk(root, parts, create=False) as (handle, name):
            try:
                removed = _windows.delete(handle, name)
            except PermissionError:
                raise
            except OSError as exc:
                raise UnsafePath(f"{name!r} could not be removed: {exc}") from exc
            if removed is not None:
                # Deleted only if it was a plain file; anything else was left alone.
                _windows_plain_file(removed, name)
        return
    with _Walk(root, parts, create=False) as (dir_fd, name):
        _check_target(dir_fd, name)
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise UnsafePath(f"{name!r} could not be removed: {exc}") from exc


def mkdir(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> None:
    """Create the leaf directory without following links or creating parents."""

    parts = _relative_parts(relative)
    if _WINDOWS:
        _windows_make_directory(root, parts, parents=False)
        return
    with _Walk(root, parts, create=False) as (dir_fd, name):
        fd = _open_child_dir(dir_fd, name, create=True)
        os.close(fd)


def makedirs(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> None:
    """Create a directory and its parents, following no links on the way."""

    parts = _relative_parts(relative)
    if _WINDOWS:
        _windows_make_directory(root, parts, parents=True)
        return
    with _Walk(root, parts, create=True) as (dir_fd, name):
        fd = _open_child_dir(dir_fd, name, create=True)
        os.close(fd)


def _windows_make_directory(root, parts: tuple[str, ...], *, parents: bool) -> None:
    with _WindowsWalk(root, parts, create=parents) as (handle, name):
        try:
            _windows.close(_windows.open_dir(handle, name, create=True))
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise _windows_refusal(exc) from exc


def exists(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> bool:
    try:
        parts = _relative_parts(relative)
    except UnsafePath:
        return False
    if _WINDOWS:
        try:
            with _WindowsWalk(root, parts, create=False) as (handle, name):
                return _windows.inspect(handle, name) is not None
        except PlatformUnsupported:
            raise
        except (OSError, ValueError):
            return False
    try:
        with _Walk(root, parts, create=False) as (dir_fd, name):
            try:
                os.lstat(name, dir_fd=dir_fd)
                return True
            except FileNotFoundError:
                return False
    except PlatformUnsupported:
        # "Cannot check here" is not "does not exist"; a caller must not act on it.
        raise
    except (UnsafePath, FileNotFoundError):
        return False


def within(root: str | os.PathLike[str], candidate: str | os.PathLike[str]) -> str | None:
    """``candidate`` as a path relative to ``root``, or ``None`` if it is outside.

    Lexical only, and deliberately so: it answers "did the caller mean something
    inside this tree", while the anchored walk is what enforces it at open time.
    """

    try:
        return str(Path(candidate).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return None


__all__ = [
    "PLATFORM_UNSUPPORTED_MESSAGE",
    "PlatformUnsupported",
    "UnsafePath",
    "delete",
    "exists",
    "file_mode",
    "makedirs",
    "read_file",
    "root_identity",
    "supported",
    "within",
    "write_file",
    "write_text",
]
