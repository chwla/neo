"""Launch only the executable content that was actually validated.

An engine is validated once -- a real run, checked end to end -- and the result is a
compatibility record. The problem is binding that record to what later runs execute.
File metadata cannot do it: ``(device, inode, size, mtime)`` is all forgeable by
anything that can write the file, and a timestamp can simply be restored after an
edit. A path is worse, because a symlink can be retargeted between the check and the
``execve``.

So validation **copies** the resolved install into a content-addressed, read-only
store under Neo's own data directory and hashes it while copying. Launches run that
copy, re-verified immediately beforehand, and the sandbox policy denies writes to the
whole store. The original install stays where the user's package manager put it; Neo
simply stops trusting it as the thing it runs.

What this does not claim: an unsandboxed process running as the same user could still
rewrite the store, because file permissions do not stop its own owner. That is out of
scope for the same reason it is for every other file Neo owns, and it is recorded as a
residual risk rather than papered over.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services.isolation.roots import roots

#: Files bigger than this are not staged. Claude Code is ~200 MB and Codex's tree is
#: ~272 MB, so the ceiling is generous; it exists to refuse something absurd rather
#: than to fill the user's disk.
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024


class ExecutableRejected(RuntimeError):
    """The executable could not be resolved or verified, so nothing is launched."""


@dataclass(frozen=True)
class Package:
    """A verified, read-only copy of one executable or one executable tree."""

    root: str
    entrypoint: str
    content_hash: str
    source_path: str
    source_hash: str
    #: The package root as the caller named it, when the install is a tree. Stored
    #: unresolved on purpose: ``matches_source`` has to re-resolve from the path Neo
    #: was given, or a symlink retargeted after validation would go unnoticed.
    source_tree: str = ""
    #: Hash of the interpreter, when the entrypoint is a script. Part of identity:
    #: what executes is the interpreter *plus* the script.
    interpreter_hash: str = ""
    interpreter: str | None = None

    def launch_argv(self, args: list[str]) -> list[str]:
        """The command line that runs the verified copy."""

        if self.interpreter:
            return [self.interpreter, self.entrypoint, *args]
        return [self.entrypoint, *args]

    def verify(self) -> None:
        """Re-hash the staged copy. Raises if it is not what was validated."""

        current = _hash_tree(Path(self.root))
        if current != self.content_hash:
            raise ExecutableRejected(
                "the verified engine package changed on disk since it was validated"
            )

    def matches_source(self) -> bool:
        """Whether the original install still has the content that was validated.

        Used to decide whether a compatibility record is still current. A ``False``
        here means "revalidate", never "launch the new thing anyway".
        """

        try:
            given = Path(self.source_tree or self.source_path)
            if _hash_tree(_resolve(given)) != self.source_hash:
                return False
            if self.interpreter:
                digest = hashlib.sha256()
                _hash_file(_resolve(Path(self.interpreter)), digest)
                return digest.hexdigest() == self.interpreter_hash
            return True
        except (ExecutableRejected, OSError):
            return False


def store_root() -> Path:
    return roots().exec_root


def is_inside_store(path: Path) -> bool:
    try:
        path.resolve().relative_to(store_root().resolve())
        return True
    except (ValueError, OSError):
        return False


def _resolve(path: Path) -> Path:
    """The real file behind ``path``, following symlinks exactly once, fully."""

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutableRejected(f"{path} could not be resolved: {exc}") from exc
    return resolved


def _hash_file(path: Path, digest) -> int:
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return size


def _hash_tree(root: Path) -> str:
    """A hash over names and contents, so a changed vendored file changes identity."""

    digest = hashlib.sha256()
    root = Path(root)
    if root.is_file():
        digest.update(b"file\0")
        _hash_file(root, digest)
        return digest.hexdigest()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
        _hash_file(path, digest)
    return digest.hexdigest()


def _is_executable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _interpreter_for(script: Path) -> tuple[str | None, str]:
    """The interpreter a script runs under, and a hash of it.

    A shebang is *content*: it is part of the file an attacker may be rewriting, so
    the interpreter it names is resolved and hashed, and that hash goes into the
    package identity. A script-based engine therefore has its interpreter covered by
    the same "has anything changed?" check as its own bytes -- a swapped Node or
    Python invalidates the compatibility record and forces revalidation.

    Hashing rather than refusing is the deliberate choice. An npm-installed CLI runs
    under whatever interpreter the user installed, and on a Homebrew setup that
    interpreter is writable by this account. Refusing it would make Neo unable to run
    those engines at all, while buying nothing: an attacker who can write as this
    user can already write Neo's own code. What is *not* acceptable is failing to
    notice, which is what this prevents. A world-writable interpreter is a different
    matter -- any account on the machine could change it -- and is refused.
    """

    try:
        with open(script, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return None, ""
    if not first.startswith(b"#!"):
        return None, ""
    parts = first[2:].decode("utf-8", "replace").strip().split()
    if not parts:
        return None, ""
    candidate = Path(parts[0])
    if candidate.name == "env" and len(parts) > 1:
        found = shutil.which(parts[1])
        if not found:
            raise ExecutableRejected(
                f"{script.name} asks for the interpreter {parts[1]!r}, which is not installed"
            )
        candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ExecutableRejected(
            f"{script.name}'s interpreter {candidate} could not be resolved: {exc}"
        ) from exc
    if info.st_mode & stat.S_IWOTH:
        raise ExecutableRejected(
            f"{script.name} runs under {resolved}, which is writable by any account on "
            "this machine; Neo will not launch an engine through it"
        )
    digest = hashlib.sha256()
    try:
        _hash_file(resolved, digest)
    except OSError as exc:
        raise ExecutableRejected(
            f"{script.name}'s interpreter {resolved} could not be read: {exc}"
        ) from exc
    return str(resolved), digest.hexdigest()


def _freeze(root: Path) -> None:
    """Strip every write bit, deepest first so a directory is sealed last.

    The sandbox policy denies writes to the whole store as well. Two independent
    mechanisms, so a mistake in either still leaves the package unmodifiable from
    inside a run.
    """

    paths = sorted(
        (p for p in root.rglob("*") if not p.is_symlink()),
        key=lambda p: len(p.parts),
        reverse=True,
    ) if root.is_dir() else []
    for path in [*paths, root]:
        try:
            if path.is_symlink():
                continue
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        except OSError:
            continue


def _hash_package(tree_root: Path, relative: Path, interpreter_hash: str = "") -> str:
    """Identity of what will be staged: the entrypoint's name plus every byte.

    The name is included because a program can read its own ``argv[0]`` and behave
    differently, so two identical files under different names are not
    interchangeable for this purpose.
    """

    digest = hashlib.sha256()
    digest.update(str(relative).encode("utf-8") + b"\0")
    digest.update(_hash_tree(tree_root).encode("ascii"))
    if interpreter_hash:
        digest.update(b"interp\0" + interpreter_hash.encode("ascii"))
    return digest.hexdigest()


def stage(path: str, *, package_root: str | None = None) -> Package:
    """Copy an executable (or its whole package) into the verified store.

    ``package_root`` names the tree to copy when the executable needs files beside
    it -- a vendored helper binary, a resources directory. Without it, just the one
    file is staged.

    Content-addressed and idempotent: staging the same thing twice reuses the copy
    that is already there, and any change to any byte produces a different identity
    rather than overwriting the old one.
    """

    source = Path(path).expanduser()
    resolved = _resolve(source)
    if resolved.is_dir():
        raise ExecutableRejected(f"{path} is a directory, not an executable")
    if not _is_executable(resolved):
        raise ExecutableRejected(f"{resolved} is not executable")

    tree_root = _resolve(Path(package_root)) if package_root else resolved
    if package_root:
        try:
            relative = resolved.relative_to(tree_root)
        except ValueError as exc:
            raise ExecutableRejected(
                f"{resolved} is not inside the package root {tree_root}"
            ) from exc
    else:
        relative = Path(resolved.name)

    total = sum(
        p.stat().st_size
        for p in ([tree_root] if tree_root.is_file() else tree_root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    )
    if total > MAX_PACKAGE_BYTES:
        raise ExecutableRejected(
            f"{tree_root} is {total} bytes, past the {MAX_PACKAGE_BYTES} byte ceiling"
        )

    source_hash = _hash_tree(tree_root)
    interpreter, interpreter_hash = _interpreter_for(resolved)


    store = store_root()
    store.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(store, 0o700)
    except OSError:
        pass
    destination = store / _hash_package(tree_root, relative, interpreter_hash)
    entry = destination / relative

    if not entry.exists():
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(store)))
        try:
            if tree_root.is_dir():
                shutil.copytree(tree_root, staging / "pkg", symlinks=True)
                staged_root = staging / "pkg"
            else:
                (staging / "pkg").mkdir()
                shutil.copy2(resolved, staging / "pkg" / relative)
                staged_root = staging / "pkg"
            # Moved into place before anything is made read-only: a sealed
            # directory cannot be renamed on some filesystems.
            os.replace(staged_root, destination)
            _freeze(destination)
        except OSError as exc:
            if not entry.exists():
                raise ExecutableRejected(f"could not stage {resolved}: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    if not entry.exists():
        raise ExecutableRejected(f"the staged package has no entrypoint at {relative}")
    if not _is_executable(entry):
        raise ExecutableRejected(f"the staged entrypoint {entry} is not executable")

    return Package(
        root=str(destination),
        entrypoint=str(entry),
        content_hash=_hash_tree(destination),
        source_path=str(source),
        source_hash=source_hash,
        source_tree=str(package_root) if package_root else "",
        interpreter=interpreter,
        interpreter_hash=interpreter_hash,
    )


__all__ = [
    "MAX_PACKAGE_BYTES",
    "ExecutableRejected",
    "Package",
    "is_inside_store",
    "stage",
    "store_root",
]
