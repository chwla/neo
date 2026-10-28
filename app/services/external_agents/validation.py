"""Checking an engine once, properly, so later runs can rely on it.

Validation is a real run: the engine is started under the real policy, in a
throwaway git repository, and asked to do one harmless thing. What it has to
demonstrate is exactly what every later run assumes:

* it authenticates without Neo touching its credentials;
* it takes its prompt from **stdin**, because Neo will not put the user's text in a
  command line;
* it reports a result Neo's adapter can read;
* its writes land in the workspace it was given, and nowhere else;
* it cannot read Neo's own storage, which the launcher's attestation checks on the
  same launch.

Nothing about the user's data is involved: the objective is a fixed instruction and
the repository is empty apart from one file this function wrote.

A pass is recorded as a compatibility record bound to the engine's verified content,
the resolved install path and the policy. Anything that changes those makes the
record stale, and the engine is refused until someone validates it again.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.services.external_agents import compat, credentials, detect
from app.services.external_agents import env as env_module
from app.services.external_agents.packaging import resolve_package
from app.services.external_agents.types import ExternalAgentError, InvocationContext
from app.services.isolation import executables
from app.services.isolation import policy as isolation_policy
from app.services.isolation.roots import roots
from app.services.repos.safety import prepare_managed_root

_LOG = logging.getLogger(__name__)

#: The one thing a validation run is asked to do. Chosen so that success is
#: unambiguous from the filesystem alone, without trusting what the engine says.
MARKER_FILE = "neo-validation.txt"
MARKER_TEXT = "neo-ok"
OBJECTIVE = (
    f"Create a file named {MARKER_FILE} in the current directory containing exactly "
    f"the text {MARKER_TEXT}. Do not do anything else."
)

#: Generous, because a real engine thinks before it acts, but far short of a
#: coding task: this is one file write.
TIMEOUT_SECONDS = 240


@dataclass(frozen=True)
class ValidationResult:
    executor: str
    ok: bool
    record: compat.Record | None
    detail: str
    checks: dict


def _adapter(executor: str):
    from app.services.external_agents.adapters import antigravity, claude_code, codex, cursor

    return {
        "claude_code": claude_code,
        "codex": codex,
        "cursor": cursor,
        "antigravity": antigravity,
    }.get(executor)


def _scratch_repo(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir(parents=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(parent / "githome")}
    (repo / "README.md").write_text("# validation\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=neo", "-c", "user.email=neo@localhost", "commit", "-qm", "init"],
    ):
        subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)
    return repo


def _validation_directories(executor: str) -> tuple[Path, Path]:
    """One code workspace and one CLI home, both isolated from other runs."""

    from app.core.paths import is_reparse_point, private_dir_mode
    from app.services.external_agents.service import _thread_state_dir

    run_id = uuid.uuid4().hex
    profile_root = roots().data_root / "workspaces" / "validation"
    prepare_managed_root(profile_root)
    parent = profile_root / run_id
    if is_reparse_point(parent):
        raise ExternalAgentError("Validation workspace is a link.")
    parent.mkdir(mode=private_dir_mode())
    parent.chmod(private_dir_mode())
    home = _thread_state_dir("validation", executor, f"validation:{run_id}")
    return parent, home


def validate(executor: str) -> ValidationResult:
    """Run one benign task through the full path, and record the result."""

    from app.services import isolation

    spec = detect.spec(executor)
    if spec is None:
        raise ExternalAgentError(f"Unknown engine '{executor}'.")
    adapter = _adapter(executor)
    if adapter is None:
        raise ExternalAgentError(f"No adapter for engine '{executor}'.")

    isolation.require_available()
    detect.require_available(executor)
    binary = detect.resolve_binary(executor)
    if not binary:
        raise ExternalAgentError(f"{spec.name} is not installed.")

    entrypoint, package_root = resolve_package(executor, binary)
    try:
        package = executables.stage(entrypoint, package_root=package_root)
    except executables.ExecutableRejected as exc:
        raise ExternalAgentError(
            f"{spec.name} could not be verified, so it was not run: {exc}"
        ) from exc

    settings = get_settings()
    # Shared host temp is readable to other concurrent runs. Both directories
    # therefore live in their scoped classes under Neo's data root.
    parent, home = _validation_directories(executor)
    cleanup_allowed = False
    checks: dict = {}
    try:
        repo = _scratch_repo(parent)
        plan = adapter.invocation(
            InvocationContext(
                binary=package.entrypoint,
                prompt=OBJECTIVE,
                mode="normal",
                cwd=str(repo),
                resume_id=None,
                new_session_id=str(uuid.uuid4()),
                disabled_tools=[],
                model=None,
                effort=None,
                unsafe=False,
            )
        )
        checks["prompt_off_argv"] = OBJECTIVE not in "\0".join(plan.argv)
        checks["prompt_on_stdin"] = plan.stdin_text == OBJECTIVE
        if not checks["prompt_on_stdin"]:
            return ValidationResult(
                executor,
                False,
                None,
                f"{spec.name}'s adapter does not send the prompt on stdin.",
                checks,
            )

        # A per-run HOME, so the engine has somewhere to keep its own state while
        # the real one stays denied, plus the access-token handoff: the sandbox
        # denies the keychain, so without this the engine reports itself signed out.
        try:
            credential = credentials.for_executor(
                executor, binary=binary, home=str(home)
            )
        except credentials.CredentialUnavailable as exc:
            raise ExternalAgentError(str(exc)) from exc
        credentials.write_files(credential)
        checks["credential_scoped"] = bool(credential.env or credential.files)

        reported: list[dict] = []

        def on_line(line) -> None:
            if isinstance(line, dict):
                reported.append(line)

        result = isolation.run(
            plan.argv,
            cwd=str(repo),
            env={
                **env_module.build_env(
                    home_env=spec.home_env,
                    home_dir=str(getattr(settings, spec.home_setting, "") or ""),
                ),
                "HOME": str(home),
                **credential.env,
            },
            purpose="engine_validation",
            profile_id="validation",
            workspace=str(repo),
            thread_state=str(home),
            exec_paths=(package.root,),
            deny_ports=[int(getattr(settings, "port", 8000) or 8000)],
            timeout=float(TIMEOUT_SECONDS),
            stdin_text=plan.stdin_text,
            on_line=on_line,
        )
        cleanup_allowed = not result.quarantined
        checks["exit_code"] = result.exit_code
        checks["quarantined"] = result.quarantined
        checks["events_parsed"] = len(reported)

        marker = repo / MARKER_FILE
        checks["wrote_marker"] = marker.is_file()
        checks["marker_contents_ok"] = (
            marker.read_text(encoding="utf-8").strip() == MARKER_TEXT
            if marker.is_file()
            else False
        )
        # Writes outside the workspace would have been refused by the policy; this
        # confirms nothing landed in the scratch parent either.
        expected = {"repo", "githome"}
        checks["no_stray_writes"] = not any(
            child.name not in expected for child in parent.iterdir()
        )

        if result.quarantined:
            return ValidationResult(
                executor, False, None, result.quarantine_reason, checks
            )
        if not checks["wrote_marker"]:
            tail = (result.stderr or "").strip()[-300:]
            return ValidationResult(
                executor,
                False,
                None,
                (
                    f"{spec.name} ran but did not complete the task"
                    + (f": {tail}" if tail else ". It produced no error output.")
                ),
                checks,
            )
        if not checks["events_parsed"]:
            return ValidationResult(
                executor,
                False,
                None,
                f"{spec.name} produced no output Neo could read.",
                checks,
            )

        record = compat.current(
            executor,
            package_hash=package.content_hash,
            source_path=package.source_path,
            policy_hash=isolation_policy.policy_version(),
        )
        compat.save(record)
        _LOG.info("engine_validated executor=%s", executor)
        return ValidationResult(
            executor, True, record, f"{spec.name} is ready to use here.", checks
        )
    finally:
        if cleanup_allowed:
            shutil.rmtree(parent, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


__all__ = ["MARKER_FILE", "MARKER_TEXT", "OBJECTIVE", "ValidationResult", "validate"]
