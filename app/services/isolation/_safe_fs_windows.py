"""The anchored walk of ``safe_fs``, built from Windows handles.

POSIX anchors a walk with ``openat``: each component is opened relative to the
descriptor of the one before it, so the kernel -- not an earlier check of a path --
decides what is reached. Windows has the same primitive under a different name:
``NtCreateFile`` takes a ``RootDirectory`` handle and a name relative to it. Every
open here goes through it with ``FILE_OPEN_REPARSE_POINT``, which opens a symlink,
junction or mount point *itself* instead of following it, and the object that was
actually opened is then inspected and refused if it is one. Nothing is decided by a
name, and nothing is opened by a full path after the root.

The root is the one thing opened by path. It is opened without following a final
link, and the path Windows reports for the opened handle must be the path that was
asked for -- so a root reached through a junction anywhere above it is refused.

Writes are atomic in the same way as on POSIX: content goes to a new temporary file
in the target's directory and is renamed over the target through its own handle.
The existing file is never opened for writing, so a target that is locked by
another program, read-only, or swapped mid-write is left exactly as it was.
"""

from __future__ import annotations

import ctypes
import os
import secrets
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import is_windows_reserved, path_key

# --- access, share, disposition and option bits ------------------------------------

_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_DATA = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_WRITE_ATTRIBUTES = 0x0100
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000

_SHARE_READ = 0x1
_SHARE_WRITE = 0x2
_SHARE_DELETE = 0x4
_SHARE_ALL = _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE

_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3

_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_FILE_OPEN_REPARSE_POINT = 0x00200000

_OBJ_CASE_INSENSITIVE = 0x40

_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value

# FILE_INFO_BY_HANDLE_CLASS (Win32) and FILE_INFORMATION_CLASS (native)
_FILE_DISPOSITION_INFO = 4
_FILE_DISPOSITION_INFO_EX = 21
_NT_FILE_RENAME_INFORMATION = 10
_NT_FILE_RENAME_INFORMATION_EX = 65

_RENAME_REPLACE_IF_EXISTS = 0x1
_RENAME_POSIX_SEMANTICS = 0x2
_DISPOSITION_DELETE = 0x1
_DISPOSITION_POSIX_SEMANTICS = 0x2

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_FUNCTION = 1
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_ERROR_DIRECTORY = 267

_WRITE_CHUNK = 1 << 20


class _UnicodeString(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_wchar_p),
    )


class _ObjectAttributes(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    )


class _IoStatusBlock(ctypes.Structure):
    _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p))


class _FileTime(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    )


_api = None


def _load():
    """The handful of kernel entry points this module uses, typed once."""

    global _api
    if _api is not None:
        return _api
    ntdll = ctypes.WinDLL("ntdll")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    ntdll.NtCreateFile.restype = ctypes.c_ulong
    ntdll.NtCreateFile.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    ntdll.NtSetInformationFile.restype = ctypes.c_ulong
    ntdll.NtSetInformationFile.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    )
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    ntdll.RtlNtStatusToDosError.argtypes = (ctypes.c_ulong,)

    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    kernel.FlushFileBuffers.restype = wintypes.BOOL
    kernel.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    _api = (ntdll, kernel)
    return _api


@dataclass(frozen=True)
class Info:
    """What the opened object is, read from the handle rather than a name."""

    attributes: int
    volume: int
    index: int
    size: int
    links: int
    modified: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def identity(self) -> tuple[int, int, int, int]:
        return self.volume, self.index, self.size, self.modified


def _oserror(code: int, name: str = "") -> OSError:
    """A Windows error code as the ``OSError`` subclass Python would raise for it."""

    error = ctypes.WinError(code)
    if name:
        error.filename = name
    return error


def _close(handle: int | None) -> None:
    if handle:
        _load()[1].CloseHandle(handle)


def _info(handle: int) -> Info:
    kernel = _load()[1]
    data = _ByHandleFileInformation()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(data)):
        raise _oserror(ctypes.get_last_error())
    return Info(
        attributes=data.dwFileAttributes,
        volume=data.dwVolumeSerialNumber,
        index=(data.nFileIndexHigh << 32) | data.nFileIndexLow,
        size=(data.nFileSizeHigh << 32) | data.nFileSizeLow,
        links=data.nNumberOfLinks,
        modified=(data.ftLastWriteTime.high << 32) | data.ftLastWriteTime.low,
    )


def check_name(name: str) -> None:
    """Refuse a component Windows would read as something other than one plain name.

    A relative open takes the name literally, so the Win32 conveniences that would
    normally get in the way are refused by hand: a separator, an alternate data
    stream (``file:stream``), a device name, and the trailing dot or space that
    Win32 strips -- which would leave a file no ordinary program can open.
    """

    if (
        not name
        or name in {".", ".."}
        or any(character in name for character in '\\/:*?"<>|')
        or any(ord(character) < 32 for character in name)
        or name[-1] in {".", " "}
        or is_windows_reserved(name)
    ):
        raise ValueError(f"{name!r} is not a usable file name on Windows")


def _open_relative(
    parent: int, name: str, *, access: int, share: int, disposition: int, options: int
) -> int:
    """Open ``name`` under the directory handle ``parent``, never following a link."""

    ntdll = _load()[0]
    length = len(name.encode("utf-16-le"))
    text = _UnicodeString(length, length, name)
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(text),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    handle = wintypes.HANDLE()
    status_block = _IoStatusBlock()
    status = ntdll.NtCreateFile(
        ctypes.byref(handle),
        access | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        share,
        disposition,
        options | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )
    if status != 0:
        raise _oserror(ntdll.RtlNtStatusToDosError(status), name)
    return handle.value


def _final_path(handle: int) -> str:
    kernel = _load()[1]
    size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not size:
        raise _oserror(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    if not kernel.GetFinalPathNameByHandleW(handle, buffer, size + 1, 0):
        raise _oserror(ctypes.get_last_error())
    text = buffer.value
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    return text[4:] if text.startswith("\\\\?\\") else text


def open_root(root: str | os.PathLike[str]) -> int:
    """A handle to the tree root, refused if anything on the way to it is a link."""

    path = Path(root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("the tree root must be an absolute path without traversal")
    kernel = _load()[1]
    handle = kernel.CreateFileW(
        str(path),
        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in (None, 0, _INVALID_HANDLE):
        raise _oserror(ctypes.get_last_error(), str(path))
    try:
        info = _info(handle)
        if info.is_reparse_point:
            raise ValueError("the tree root is a symbolic link or junction")
        if not info.is_directory:
            raise ValueError("the tree root is not a directory")
        # Opening by path followed any link *above* the root. The handle knows
        # where it really is; if that is not where it was asked to be, something
        # on the way redirected it.
        if path_key(_final_path(handle)) != path_key(os.path.abspath(path)):
            raise ValueError("the tree root is reached through a link")
        return handle
    except BaseException:
        _close(handle)
        raise


def open_dir(parent: int, name: str, *, create: bool) -> int:
    """Open (or create) the directory ``name`` under ``parent``; links are refused."""

    check_name(name)
    handle = _open_relative(
        parent,
        name,
        access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
        share=_SHARE_ALL,
        disposition=_FILE_OPEN_IF if create else _FILE_OPEN,
        # No FILE_DIRECTORY_FILE: with it a junction to a directory and a plain
        # file are told apart by error code alone. Opening whatever is there and
        # asking the handle is unambiguous.
        options=_FILE_OPEN_FOR_BACKUP_INTENT | (_FILE_DIRECTORY_FILE if create else 0),
    )
    try:
        info = _info(handle)
        if info.is_reparse_point:
            raise ValueError(f"{name!r} is a link or junction, so it may leave the tree")
        if not info.is_directory:
            raise NotADirectoryError(name)
        return handle
    except BaseException:
        _close(handle)
        raise


def inspect(parent: int, name: str) -> Info | None:
    """What ``name`` under ``parent`` is right now, or ``None`` if nothing is there."""

    check_name(name)
    try:
        handle = _open_relative(
            parent,
            name,
            access=_FILE_READ_ATTRIBUTES,
            share=_SHARE_ALL,
            disposition=_FILE_OPEN,
            options=_FILE_OPEN_FOR_BACKUP_INTENT,
        )
    except FileNotFoundError:
        return None
    try:
        return _info(handle)
    finally:
        _close(handle)


def read(parent: int, name: str) -> tuple[bytes, Info]:
    """The bytes of the plain file ``name``, with what the opened handle is."""

    check_name(name)
    handle = _open_relative(
        parent,
        name,
        access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES,
        share=_SHARE_ALL,
        disposition=_FILE_OPEN,
        options=_FILE_OPEN_FOR_BACKUP_INTENT,
    )
    kernel = _load()[1]
    try:
        info = _info(handle)
        if info.is_reparse_point or info.is_directory:
            return b"", info
        chunks = []
        buffer = ctypes.create_string_buffer(_WRITE_CHUNK)
        count = wintypes.DWORD()
        while True:
            if not kernel.ReadFile(handle, buffer, _WRITE_CHUNK, ctypes.byref(count), None):
                raise _oserror(ctypes.get_last_error(), name)
            if not count.value:
                break
            chunks.append(buffer.raw[: count.value])
        return b"".join(chunks), info
    finally:
        _close(handle)


def _set_disposition(handle: int) -> None:
    """Mark the open file for deletion when its handle closes."""

    kernel = _load()[1]
    flags = wintypes.DWORD(_DISPOSITION_DELETE | _DISPOSITION_POSIX_SEMANTICS)
    if kernel.SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)
    ):
        return
    code = ctypes.get_last_error()
    if code not in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED, _ERROR_INVALID_FUNCTION}:
        raise _oserror(code)
    # A filesystem without POSIX delete semantics (FAT, some network shares).
    plain = ctypes.c_ubyte(1)
    if not kernel.SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO, ctypes.byref(plain), ctypes.sizeof(plain)
    ):
        raise _oserror(ctypes.get_last_error())


def _rename_over(handle: int, parent: int, name: str) -> None:
    """Rename the open file to ``name`` inside the directory ``parent``, replacing.

    Asked of the kernel directly, with the destination directory given as a handle
    and the name relative to it. The Win32 wrapper for the same call resolves a
    bare name against the *process's working directory*, which would move the file
    out of the tree entirely; here there is no path for anything to redirect.
    """

    ntdll = _load()[0]
    encoded = name.encode("utf-16-le")
    # FILE_RENAME_INFORMATION: flags (or a BOOLEAN), padding, the destination
    # directory's handle, the name's length in bytes, then the name itself.
    handle_size = ctypes.sizeof(wintypes.HANDLE)
    header = handle_size * 2 + 4
    size = header + len(encoded) + 2

    def attempt(information_class: int, flags: int) -> int:
        buffer = ctypes.create_string_buffer(size)
        base = ctypes.addressof(buffer)
        ctypes.memmove(base, flags.to_bytes(4, "little"), 4)
        ctypes.memmove(base + handle_size, int(parent).to_bytes(handle_size, "little"), handle_size)
        ctypes.memmove(base + handle_size * 2, len(encoded).to_bytes(4, "little"), 4)
        ctypes.memmove(base + header, encoded, len(encoded))
        status_block = _IoStatusBlock()
        status = ntdll.NtSetInformationFile(
            handle, ctypes.byref(status_block), buffer, size, information_class
        )
        return ntdll.RtlNtStatusToDosError(status) if status else 0

    code = attempt(
        _NT_FILE_RENAME_INFORMATION_EX, _RENAME_REPLACE_IF_EXISTS | _RENAME_POSIX_SEMANTICS
    )
    if code in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED, _ERROR_INVALID_FUNCTION}:
        # A filesystem without POSIX rename semantics (FAT, some network shares).
        code = attempt(_NT_FILE_RENAME_INFORMATION, 1)
    if code:
        raise _oserror(code, name)


def write(parent: int, name: str, data: bytes, *, unchanged_since: Info | None) -> None:
    """Replace ``name`` under ``parent`` with ``data``, atomically.

    ``unchanged_since`` is what the target was when the caller looked. It is checked
    again immediately before the rename, so a target swapped while the temporary
    file was being written is refused rather than replaced.
    """

    check_name(name)
    kernel = _load()[1]
    temporary = f".neo-write-{secrets.token_hex(6)}"
    handle = _open_relative(
        parent,
        temporary,
        access=_FILE_WRITE_DATA | _FILE_APPEND_DATA | _FILE_WRITE_ATTRIBUTES
        | _FILE_READ_ATTRIBUTES | _DELETE,
        share=_SHARE_READ | _SHARE_DELETE,
        disposition=_FILE_CREATE,
        options=_FILE_NON_DIRECTORY_FILE,
    )
    try:
        view = memoryview(data)
        written = wintypes.DWORD()
        offset = 0
        while offset < len(view):
            chunk = bytes(view[offset : offset + _WRITE_CHUNK])
            if not kernel.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
                raise _oserror(ctypes.get_last_error(), name)
            if not written.value:
                raise OSError("anchored write made no progress")
            offset += written.value
        if not kernel.FlushFileBuffers(handle):
            raise _oserror(ctypes.get_last_error(), name)
        current = inspect(parent, name)
        if (unchanged_since is None) != (current is None) or (
            unchanged_since is not None
            and current is not None
            and unchanged_since.identity != current.identity
        ):
            raise ValueError("the target changed during the anchored write")
        if current is not None and current.attributes & _FILE_ATTRIBUTE_READONLY:
            raise _oserror(_ERROR_ACCESS_DENIED, name)
        _rename_over(handle, parent, name)
    except BaseException:
        try:
            _set_disposition(handle)
        except OSError:
            pass
        raise
    finally:
        _close(handle)


def delete(parent: int, name: str) -> Info | None:
    """Remove the plain file ``name``; returns what it was, or ``None`` if absent.

    The file is opened without following a link, inspected through that handle,
    and deleted through the same handle -- so what is removed is what was checked.
    """

    check_name(name)
    try:
        handle = _open_relative(
            parent,
            name,
            access=_DELETE | _FILE_READ_ATTRIBUTES,
            share=_SHARE_ALL,
            disposition=_FILE_OPEN,
            options=_FILE_OPEN_FOR_BACKUP_INTENT,
        )
    except FileNotFoundError:
        return None
    try:
        info = _info(handle)
        if info.is_reparse_point or info.is_directory or info.links != 1:
            return info
        _set_disposition(handle)
        return info
    finally:
        _close(handle)


def close(handle: int | None) -> None:
    _close(handle)


def is_missing(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}


def is_busy(error: OSError) -> bool:
    """Whether the refusal means another program has the file open or locked."""

    return getattr(error, "winerror", None) in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}
