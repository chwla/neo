"""What a program Neo starts on the user's behalf is given, and how it is found.

Shared by the command sandbox and the test runner, which used to differ in two
ways that both mattered on Windows:

* The test runner handed children a bare environment -- no ``SystemRoot``, no
  ``TEMP``, no ``PATHEXT`` -- so anything using ``tempfile``, winsock or a ``.cmd``
  in a sub-child failed or wrote into the wrong place.
* ``python`` meant Neo's own interpreter, so a project's tests ran without the
  project's packages (``No module named pytest``).

And one way that mattered for safety: ``npm`` resolved to ``npm.cmd``, which
Windows runs through ``cmd.exe``, re-parsing the whole command line. An argument
the allowlist had passed could then carry ``&`` and run a second command. No
``.cmd`` or ``.bat`` file is ever started here: ``npm`` and ``npx`` are started as
``node`` with their JavaScript entry point, and any other shim is refused.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.core.paths import IS_WINDOWS, is_within

#: Variables a child process genuinely needs to start on Windows. ``SystemRoot``
#: is not a convenience: without it the CRT cannot initialise winsock and many
#: programs, Python included, fail before running. The environment stays an
#: allowlist -- these are added to it, not inherited wholesale.
WINDOWS_PASSTHROUGH = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "PATHEXT",
    "TEMP",
    "TMP",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

_BATCH_SUFFIXES = {".cmd", ".bat"}


class ProgramRefused(PermissionError):
    """The program resolved to something Neo will not start."""


def child_environment(cwd: Path, *, home: Path | None = None) -> dict[str, str]:
    """The allowlisted environment for a program run inside ``cwd``.

    ``HOME`` (and ``USERPROFILE`` on Windows) point at ``home`` or, failing that,
    the workspace: a tool that writes a cache or config file should put it in the
    sandbox, not in the user's profile.
    """

    env = {
        "PATH": os.environ.get("PATH", ""),
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    base = str(home if home is not None else cwd)
    env["HOME"] = base
    if IS_WINDOWS:
        env["USERPROFILE"] = base
        for name in WINDOWS_PASSTHROUGH:
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def project_interpreter(cwd: Path) -> str | None:
    """The project's own Python, if it has a virtual environment in the usual place."""

    for relative in (".venv", "venv", "env"):
        candidate = cwd / relative / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        if candidate.is_file():
            return str(candidate)
    return None


def _node_script_launch(found: Path) -> list[str] | None:
    """``node`` plus the entry script behind an ``npm``/``npx`` shim, or ``None``."""

    name = found.stem.lower()
    if name not in {"npm", "npx"}:
        return None
    node = shutil.which("node", path=str(found.parent)) or shutil.which("node")
    if not node:
        return None
    for script in (
        found.parent / "node_modules" / "npm" / "bin" / f"{name}-cli.js",
        found.parent.parent / "lib" / "node_modules" / "npm" / "bin" / f"{name}-cli.js",
    ):
        if script.is_file():
            return [str(Path(node).resolve()), str(script.resolve())]
    return None


def resolve_program(
    program: str, cwd: Path, path: str | None = None, *, python: str | None = None
) -> list[str]:
    """The argv prefix that starts the allowlisted ``program`` -- usually one element.

    ``python`` is the interpreter to use for that name (the project's own, found
    by :func:`project_interpreter`, or Neo's as a last resort). Resolution is
    confined to PATH, never the current directory, and a result inside the
    workspace is refused: a repository carrying its own ``pytest.exe`` must not
    be run in place of the real one.
    """

    if program == "python" and python:
        return [python]
    found = shutil.which(program, path=path if path is not None else os.environ.get("PATH", ""))
    if not found:
        raise FileNotFoundError(
            f"'{program}' is not installed on this machine, so Neo cannot run it here."
        )
    resolved = Path(found).resolve()
    if is_within(cwd.resolve(), resolved):
        raise ProgramRefused(
            f"'{program}' resolved to a file inside the workspace; refusing to run it."
        )
    if IS_WINDOWS and resolved.suffix.lower() in _BATCH_SUFFIXES:
        # A batch file runs through cmd.exe, which re-parses the whole command line.
        launch = _node_script_launch(resolved)
        if launch is None:
            raise ProgramRefused(
                f"'{program}' is installed as a batch file ({resolved.name}). Neo runs "
                "programs, not batch files, because cmd.exe would reinterpret the "
                "arguments."
            )
        return launch
    return [str(resolved)]
