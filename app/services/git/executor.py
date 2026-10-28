from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import active_profile_storage_dir
from app.services import isolation
from app.services.git.safety import validate_git_args

OUTPUT_LIMIT = 300 * 1024

#: Configuration overrides on every git command line Neo runs.
#:
#: Git reads configuration from the repository it is pointed at, and several settings
#: name a program to run. Neo inspects repositories an external agent has just
#: edited, from its own unsandboxed process, so a repo-local ``core.fsmonitor``,
#: filter driver or ``textconv`` was a way to run code as Neo -- reaching everything
#: the agent itself was denied. Measured: before this, a planted ``core.fsmonitor``
#: ran on a plain ``git status`` and on the post-run change snapshot.
#:
#: ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_NOSYSTEM`` in the environment below already
#: discard the user's own configuration; these close the repository's own.
_CONFIG_OVERRIDES = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.sshCommand=",
    "-c", "core.pager=cat",
    "-c", "core.editor=true",
    "-c", "core.attributesFile=/dev/null",
    "-c", "diff.external=",
    "-c", "protocol.ext.allow=never",
    "-c", "uploadpack.packObjectsHook=",
    "--no-replace-objects",
    "--no-optional-locks",
)

#: Flags that only exist on particular subcommands, so they cannot go above. These
#: are what actually stop an attribute-selected ``textconv`` or external diff driver
#: from running during a diff.
_SUBCOMMAND_FLAGS = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff",),
    "blame": ("--no-textconv",),
}


def _execute(
    executable: str,
    args: list[str],
    cwd: Path,
    environment: dict[str, str],
    *,
    timeout: int,
    raw_stdout: bool = False,
) -> subprocess.CompletedProcess:
    """Run even Git's setup probes inside the tooling policy when available."""

    if isolation.status()["supported"]:
        storage = active_profile_storage_dir.get()
        profile_id = Path(storage).name if storage else ""
        child_env = {
            **environment,
            "HOME": str(cwd),
            "XDG_CONFIG_HOME": str(cwd),
        }
        result = isolation.run(
            [executable, *args],
            cwd=str(cwd),
            env=child_env,
            purpose="git_tooling",
            profile_id=profile_id,
            workspace=str(cwd),
            timeout=float(timeout),
            raw_stdout=raw_stdout,
            max_stdout_bytes=4 * 1024 * 1024 + 1 if raw_stdout else None,
        )
        if result.quarantined:
            raise RuntimeError(
                f"Git tooling process could not be cleaned up: {result.quarantine_reason}"
            )
        if result.output_limit_exceeded:
            raise RuntimeError("Git output exceeds the safe restore limit.")
        if result.timed_out:
            raise subprocess.TimeoutExpired([executable, *args], timeout)
        return subprocess.CompletedProcess(
            [executable, *args],
            result.exit_code if result.exit_code is not None else 1,
            result.stdout_bytes if raw_stdout else result.stdout.encode("utf-8", errors="replace"),
            result.stderr.encode("utf-8", errors="replace"),
        )
    return subprocess.run(
        [executable, *args],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _attribute_overrides(executable: str, cwd: Path, environment: dict[str, str]) -> list[str]:
    """Stop the repository's own ``.gitattributes`` from selecting a driver.

    ``textconv``, ``diff.external`` and the ``clean``/``smudge``/``process`` filters
    are chosen per path by attributes, and the drivers they name are commands. A
    ``git diff`` on a repository an agent has just edited therefore ran the clean
    filter -- measured, before this existed.

    ``--attr-source`` points git at a tree to read attributes from; an empty tree
    means no attributes apply, whatever the working copy or any commit says. The
    empty tree's id depends on the repository's hash algorithm, so git computes it.

    Where ``--attr-source`` is unavailable (git before 2.40), the drivers named in
    the effective configuration are emptied individually instead. That needs the
    driver names, which is why it is the fallback rather than the rule.
    """

    try:
        empty = _execute(
            executable, ["hash-object", "-t", "tree", os.devnull], cwd, environment, timeout=10
        )
        tree = empty.stdout.decode("utf-8", errors="replace").strip()
        if empty.returncode == 0 and tree:
            return [f"--attr-source={tree}"]
    except (OSError, subprocess.SubprocessError):
        pass

    overrides: list[str] = []
    try:
        listed = _execute(executable, ["config", "--list"], cwd, environment, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return overrides
    for line in listed.stdout.decode("utf-8", errors="replace").splitlines():
        key = line.split("=", 1)[0].strip().lower()
        parts = key.split(".")
        if len(parts) != 3:
            continue
        family, name, leaf = parts
        if family == "filter" and leaf in {"clean", "smudge", "process"}:
            overrides += ["-c", f"filter.{name}.{leaf}="]
        elif family == "diff" and leaf in {"textconv", "command"}:
            overrides += ["-c", f"diff.{name}.{leaf}="]
    return overrides


def _harden(
    args: list[str], executable: str, cwd: Path, environment: dict[str, str]
) -> list[str]:
    """The approved argv with hardening added, subcommand flags in the right place."""

    overrides = [*_CONFIG_OVERRIDES, *_attribute_overrides(executable, cwd, environment)]
    extra = _SUBCOMMAND_FLAGS.get(args[0], ()) if args else ()
    if not extra:
        return [*overrides, *args]
    return [*overrides, args[0], *extra, *args[1:]]


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    stdout_bytes: bytes = b""


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(
    cwd: Path, args: list[str], *, timeout: int = 30, check: bool = True,
    raw_stdout: bool = False,
) -> GitResult:
    validate_git_args(args)
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("Git is not installed in this runtime.")
    home = tempfile.mkdtemp(prefix="neo-git-home-")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
        # Stop the repository discovery walk at the directory Neo named. Without it
        # a managed workspace that is itself inside Neo's checkout resolved to that
        # outer repository, so the change journal reported on the wrong tree.
        "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent),
        # Attribute files drive textconv and the clean/smudge filters; the repo's own
        # must not be consulted for anything Neo runs.
        "GIT_ATTR_NOSYSTEM": "1",
        # No filter or diff helper gets a terminal to prompt on, and none of the
        # credential helpers run.
        "GIT_ASKPASS": "",
        "GIT_CONFIG_COUNT": "0",
    }
    try:
        completed = _execute(
            executable,
            _harden(args, executable, cwd, environment),
            cwd,
            environment,
            timeout=timeout,
            raw_stdout=raw_stdout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Git operation timed out after {timeout} seconds.") from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)
    stdout_limit = 4 * 1024 * 1024 if raw_stdout else OUTPUT_LIMIT
    stdout_bytes = completed.stdout[:stdout_limit]
    stderr_bytes = completed.stderr[:OUTPUT_LIMIT]
    truncated = len(completed.stdout) > stdout_limit or len(completed.stderr) > OUTPUT_LIMIT
    result = GitResult(
        returncode=completed.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        truncated=truncated,
        stdout_bytes=stdout_bytes,
    )
    if check and completed.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git operation failed."
        raise RuntimeError(detail[:2000])
    return result
