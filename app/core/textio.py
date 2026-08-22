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
"""

from __future__ import annotations

from pathlib import Path

LF = "\n"
CRLF = "\r\n"


def decode(raw: bytes) -> str:
    """Bytes to text with line endings normalised to ``\\n``.

    The model reasons in ``\\n`` and its ``old_string`` arguments are written that
    way, so an editable view of the file has to be normalised; the original
    convention is re-applied on write rather than carried through the middle.
    """

    return raw.decode("utf-8", errors="replace").replace(CRLF, LF).replace("\r", LF)


def read_text(path: Path) -> str:
    return decode(path.read_bytes())


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
    index = raw.find(b"\n")
    if index == -1:
        return default
    return CRLF if index > 0 and raw[index - 1 : index] == b"\r" else LF


def encode(text: str, newline: str = LF) -> bytes:
    normalized = text.replace(CRLF, LF).replace("\r", LF)
    if newline != LF:
        normalized = normalized.replace(LF, newline)
    return normalized.encode("utf-8")


def write_text(path: Path, text: str, *, newline: str | None = None) -> None:
    """Write text, keeping the file's existing line endings.

    ``newline=None`` means "whatever this file already used", which is what makes
    an edit to a CRLF file stay a CRLF file. A new file gets ``\\n``: it is what
    the model produced, and it is what git expects to be told about.

    Writing bytes rather than using text mode is what keeps the platform out of
    it -- ``write_text`` would re-translate on Windows whatever we decided here.
    """

    ending = newline if newline is not None else detect_newline(path)
    path.write_bytes(encode(text, ending))
