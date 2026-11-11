"""Reading and writing workspace text without rewriting what was not edited.

Python's text mode translates line endings twice: reading turns ``\\r\\n`` into
``\\n``, and writing turns ``\\n`` back into ``os.linesep``. On one machine those
cancel out. Across machines they do not, and the result is a file where every
line changed because one line was edited:

* A Windows-authored (CRLF) repository edited by Neo on Linux or macOS -- the
  ordinary case for Docker Desktop bind-mounting a Windows folder -- comes back
  entirely LF.
* An LF repository edited by Neo on Windows comes back entirely CRLF.

Either one turns a one-line change into a whole-file diff in the user's own git,
and makes the snapshot journal's undo restore bytes that never existed. So all
workspace text goes through here: bytes in, bytes out, translation never
implicit, and the file's existing convention preserved.

The same goes for the encoding. A file that is not UTF-8 -- a cp1252 source file
is ordinary on Windows -- used to be decoded with replacement characters and
written back as UTF-8, which silently destroyed every non-ASCII byte in it. An
edit now reads the file in the encoding it is actually in and writes it back the
same way, or refuses.

And for the write itself: content goes to a temporary file beside the target and
is renamed over it. Opening the target for writing truncates it first, so a write
that then failed (a file locked by another program, a full disk) left an empty
file where the user's work had been.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

LF = "\n"
CRLF = "\r\n"

UTF8 = "utf-8"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
#: Tried, in order, for a file that is not UTF-8. cp1252 is what Windows editors
#: wrote for decades; latin-1 maps every byte, so it always round-trips.
_LEGACY_ENCODINGS = ("cp1252", "latin-1")


class NotTextError(ValueError):
    """The file cannot be edited as text without changing bytes that were not edited."""


@dataclass(frozen=True)
class TextFile:
    """A file's text together with what is needed to write it back unchanged."""

    text: str
    encoding: str
    newline: str


def decode(raw: bytes) -> str:
    """Bytes to text with line endings normalised to ``\\n``, for *display*.

    The model reasons in ``\\n`` and its ``old_string`` arguments are written that
    way, so an editable view of the file has to be normalised; the original
    convention is re-applied on write rather than carried through the middle.

    Lossy on purpose -- an undecodable byte becomes U+FFFD -- so it is only for
    showing content. Anything that will be written back goes through
    :func:`read_editable`.
    """

    return raw.decode("utf-8", errors="replace").replace(CRLF, LF).replace("\r", LF)


def is_utf8(raw: bytes) -> bool:
    try:
        raw.decode(UTF8)
    except UnicodeDecodeError:
        return False
    return True


def _newline_of(raw: bytes, default: str = LF) -> str:
    index = raw.find(b"\n")
    if index == -1:
        return default
    return CRLF if index > 0 and raw[index - 1 : index] == b"\r" else LF


def detect_encoding(raw: bytes) -> str:
    """The encoding that turns ``raw`` into text and back into the same bytes.

    UTF-8 first (a byte-order mark stays in the text as U+FEFF, so it is written
    back where it was). UTF-16 is recognised by its mark. Anything else is tried
    as a single-byte legacy encoding, and accepted only if it round-trips exactly.
    """

    if is_utf8(raw):
        return UTF8
    if raw[:2] in _UTF16_BOMS:
        try:
            if raw.decode("utf-16").encode("utf-16") == raw:
                return "utf-16"
        except UnicodeError:
            pass
        raise NotTextError("This file is not text Neo can edit safely (unreadable UTF-16).")
    if b"\x00" in raw:
        raise NotTextError("This file is binary and cannot be edited as text.")
    for encoding in _LEGACY_ENCODINGS:
        try:
            if raw.decode(encoding).encode(encoding) == raw:
                return encoding
        except UnicodeError:
            continue
    raise NotTextError("This file is not text Neo can edit safely.")  # pragma: no cover


def read_text(path: Path) -> str:
    return decode(path.read_bytes())


def read_editable(path: Path) -> TextFile:
    """A file's text for editing, with the encoding and line ending to keep."""

    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    text = raw.decode(encoding).replace(CRLF, LF).replace("\r", LF)
    return TextFile(text=text, encoding=encoding, newline=_newline_of(raw))


def detect_newline(path: Path, default: str = LF) -> str:
    """The line ending a file already uses, so an edit can keep using it.

    Decided by which convention appears first, not by counting: a file is
    virtually never mixed, and the first ending is what the author's editor
    produced.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return default
    return _newline_of(raw, default)


def encode(text: str, newline: str = LF, encoding: str = UTF8) -> bytes:
    normalized = text.replace(CRLF, LF).replace("\r", LF)
    if newline != LF:
        normalized = normalized.replace(LF, newline)
    try:
        return normalized.encode(encoding)
    except UnicodeEncodeError as exc:
        raise NotTextError(
            f"This file is {encoding} text, not UTF-8, and the new content has characters "
            f"{encoding} cannot hold. Convert the file to UTF-8 first."
        ) from exc


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` without ever leaving it half written.

    The data is written and flushed to a new file in the same directory, which is
    then renamed over the target. If anything fails before the rename -- the
    target is locked or read-only, the disk is full -- the original is untouched
    and the temporary file is removed.
    """

    temporary = path.with_name(f".neo-write-{secrets.token_hex(6)}")
    try:
        existing = path.stat()
    except OSError:
        existing = None
    if existing is not None and not os.access(path, os.W_OK):
        # A read-only target. POSIX would let the rename replace it (only the
        # directory matters there), which is not what "read-only" means to the
        # person who set it.
        raise PermissionError(13, "The file is read-only", str(path))
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing is not None:
            try:
                os.chmod(temporary, stat.S_IMODE(existing.st_mode))
            except OSError:
                pass
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_text(
    path: Path, text: str, *, newline: str | None = None, encoding: str | None = None
) -> None:
    """Write text, keeping the file's existing line endings and encoding.

    ``newline=None`` means "whatever this file already used", which is what makes
    an edit to a CRLF file stay a CRLF file. A new file gets ``\\n``: it is what
    the model produced, and it is what git expects to be told about. ``encoding``
    works the same way: an existing file that is not UTF-8 keeps its encoding when
    the new text fits in it, and becomes UTF-8 only when the whole content is being
    replaced by something that does not.

    Writing bytes rather than using text mode is what keeps the platform out of
    it -- ``write_text`` would re-translate on Windows whatever we decided here.
    """

    ending = newline if newline is not None else detect_newline(path)
    chosen = encoding
    if chosen is None:
        chosen = UTF8
        try:
            existing = path.read_bytes()
        except OSError:
            existing = None
        if existing is not None and not is_utf8(existing):
            try:
                legacy = detect_encoding(existing)
                encode(text, ending, legacy)
                chosen = legacy
            except NotTextError:
                chosen = UTF8
    write_bytes_atomic(path, encode(text, ending, chosen))
