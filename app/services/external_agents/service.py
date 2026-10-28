"""Running one external executor as a Neo agent session.

The division of labour: Neo owns the session, the event log, the workspace, the
change journal and cancellation; the CLI owns its model, its loop, its tools and
its own credentials. This module is the seam. It is called from
``agent_core.worker`` in place of ``AgentLoop.run``, and everything around it --
the lease, the heartbeat, turning an exception into ``run.failed`` -- is the
worker's, unchanged, so both kinds of run get identical lifecycle handling.

Ordering matters in two places and is worth stating:

* **Every refusal happens before the process starts.** A disabled feature, a
  missing binary, a signed-out CLI, a workspace that is not a git repository --
  all are decided up front. Discovering any of them afterwards means discovering
  it once an agent has already edited files, which is the one moment the check
  is worthless.
* **The pre-run snapshot is taken before the process starts and recorded after
  it ends.** That is what makes "what changed" mean this run, and not the last
  commit (see ``snapshot``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services import chat_prefs
from app.services.agent_core import events, store
from app.services.agent_core.types import AgentSession, Budgets
from app.services.agent_core.workspace import repo_root
from app.services.disclosure import (
    Destination,
    Label,
    Labeled,
    authorize,
    from_json,
    public,
    scoped_copy,
    to_json,
)
from app.services.disclosure import approvals as disclosure_approvals
from app.services.external_agents import credentials, detect, models, snapshot
from app.services.external_agents import disclosure as ext_disclosure
from app.services.external_agents import env as env_module
from app.services.external_agents.adapters import antigravity as antigravity_adapter
from app.services.external_agents.adapters import claude_code as claude_adapter
from app.services.external_agents.adapters import codex as codex_adapter
from app.services.external_agents.adapters import cursor as cursor_adapter
from app.services.external_agents.provenance import output_label
from app.services.external_agents.types import (
    ExternalAgentError,
    ExternalEvent,
    InvocationContext,
    RunOutcome,
)
from app.services.repos import store as repos_store
from app.services.skills import resolver as skills_resolver

#: Which adapter drives which CLI. The whole of Neo's engine-specific dispatch,
#: in one table -- everything past this point is uniform.
_ADAPTERS = {
    "claude_code": claude_adapter,
    "codex": codex_adapter,
    "cursor": cursor_adapter,
    "antigravity": antigravity_adapter,
}

_LOG = logging.getLogger(__name__)

#: Emitted between the steps of a handoff chain so the transcript can show where
#: one executor handed over to the next. The only addition to the vocabulary.
STEP_STARTED = "step.started"


def _session(session_id: str) -> AgentSession:
    row = store.get_session(session_id)
    if row is None:
        raise LookupError("Agent session not found.")
    return AgentSession(**{**row, "budgets": Budgets(**(row.get("budgets") or {}))})


def _is_cancelled(session_id: str) -> bool:
    """Only an explicit cancellation counts.

    Deliberately *not* ``row is None or ...``. A missing row means the probe
    could not read it -- the wrong database, a locked file -- and treating that
    as a cancellation lets a transient read failure kill a healthy run. The
    session is cancelled when it says so, and not otherwise.
    """

    row = store.get_session(session_id)
    return bool(row) and row["status"] == "cancelled"


def _unsafe_allowed(repo_id: str | None) -> bool:
    """Both gates, and both must be open.

    Two independent switches rather than one, because either alone is a state
    somebody can reach without meaning to: a global flag set once for a scratch
    machine, or a per-repository box ticked in a hurry. Requiring both means
    unsandboxed execution is always a thing someone decided twice.
    """

    if not get_settings().external_agent_allow_unsafe:
        return False
    if not repo_id:
        return False
    try:
        from app.services.repos import store as repos_store

        repo = repos_store.get_repo(repo_id) or {}
    except Exception:
        return False
    return bool(repo.get("external_unsafe_opt_in"))


def _resume_id(session: AgentSession, executor: str) -> str | None:
    """The executor's own conversation id from earlier in this chat, if any.

    Read per executor, so Claude -> Codex -> Claude resumes the *original* Claude
    conversation rather than starting a third one.
    """

    spec = detect.spec(executor)
    if spec is None or not spec.capabilities.resume:
        # An executor that cannot continue its own conversation always starts a
        # fresh one; the shared Neo transcript is what carries the context.
        return None
    meta = (session.external_meta or {}).get(executor) or {}
    existing = meta.get(spec.session_id_key)
    if existing:
        return str(existing)
    if not session.chat_id:
        return None
    try:
        return store.last_external_session_id(session.chat_id, executor, exclude=session.id)
    except Exception:  # pragma: no cover - continuity is a nicety, never fatal
        return None


def _merge_meta(session_id: str, executor: str, patch: dict[str, Any]) -> None:
    """Fold new facts into this session's per-executor metadata."""

    if not patch:
        return
    row = store.get_session(session_id) or {}
    meta = dict(row.get("external_meta") or {})
    current = dict(meta.get(executor) or {})
    current.update({key: value for key, value in patch.items() if value is not None})
    meta[executor] = current
    store.update_session(session_id, {"external_meta": meta})


def _launch(
    argv: list[str],
    *,
    root: Path,
    env: dict[str, str],
    timeout: float,
    on_line,
    session_id: str,
    profile_id: str,
    mode: str,
    stdin_text: str | None,
    exec_paths: tuple[str, ...] = (),
    thread_state: str | None = None,
) -> RunOutcome:
    """Start the CLI under the isolation launcher, and translate its result.

    Everything the launcher refuses -- an unsupported platform, a workspace that
    overlaps Neo's own storage, a failed self-check -- becomes an
    :class:`ExternalAgentError`, because from the user's point of view those are all
    "this run did not start, and here is what to fix". There is no unsandboxed path.
    """

    from app.services import isolation

    try:
        result = isolation.run(
            argv,
            cwd=str(root),
            env=env,
            purpose="external_agent",
            profile_id=profile_id,
            workspace=str(root),
            thread_state=thread_state,
            exec_paths=tuple(exec_paths),
            shared_config_paths=_shared_config_paths(),
            deny_ports=_neo_ports(),
            mode=mode,
            timeout=timeout,
            stdin_text=stdin_text,
            on_line=on_line,
            is_cancelled=lambda: _is_cancelled(session_id),
        )
    except isolation.IsolationUnavailable as exc:
        raise ExternalAgentError(
            f"External engines are unavailable on this machine: {exc}"
        ) from exc
    except isolation.IsolationRefused as exc:
        raise ExternalAgentError(str(exc)) from exc

    outcome = RunOutcome()
    outcome.exit_code = result.exit_code
    outcome.timed_out = result.timed_out
    outcome.cancelled = result.cancelled
    if result.quarantined:
        # Not "completed": something from this run is still alive, so the workspace
        # and the undo journal are not safe to act on yet.
        outcome.outcome = "failed"
        outcome.error = result.quarantine_reason
        return outcome
    if result.cancelled:
        outcome.outcome = "failed"
        outcome.error = "cancelled"
    elif result.timed_out:
        outcome.outcome = "failed"
        outcome.error = f"the run exceeded its {int(timeout)}s time limit and was stopped"
    elif result.exit_code == 0:
        outcome.outcome = "completed"
    else:
        outcome.outcome = "failed"
        tail = " | ".join(result.stderr.splitlines()[-6:])[:1000]
        outcome.error = f"{os.path.basename(argv[0])} exited with code {result.exit_code}" + (
            f": {tail}" if tail else ""
        )
    return outcome


def _verified_package(executor: str, binary: str):
    """Stage this engine's install into the verified store, or refuse the run."""

    from app.services.external_agents.packaging import resolve_package
    from app.services.isolation import executables

    entrypoint, package_root = resolve_package(executor, binary)
    try:
        return executables.stage(entrypoint, package_root=package_root)
    except executables.ExecutableRejected as exc:
        raise ExternalAgentError(
            f"{executor} was not started because Neo could not verify the program it "
            f"would run: {exc}"
        ) from exc


def _require_validated(executor: str, package) -> None:
    """Refuse an engine nobody has checked on this machine.

    Installed is not the same as validated. The record says someone ran this exact
    program under this exact policy and watched it work; without one, Neo would be
    guessing that an engine authenticates, reads its prompt from stdin and keeps its
    writes where it was told -- and guessing wrong looks like a bug, not a refusal.
    """

    from app.services.external_agents import compat
    from app.services.isolation import policy as isolation_policy

    try:
        compat.require(
            executor,
            package_hash=package.content_hash,
            source_path=package.source_path,
            policy_hash=isolation_policy.policy_version(),
        )
    except compat.NotValidated as exc:
        raise ExternalAgentError(str(exc)) from exc


def _artifact_labels():
    """This profile's artifact label record, in its own private storage."""

    from app.core.config import active_profile_storage_dir
    from app.services.disclosure.artifacts import ArtifactLabels
    from app.services.isolation.roots import roots

    storage = active_profile_storage_dir.get()
    base = Path(storage) if storage else roots().data_root
    return ArtifactLabels(base / "artifact-labels.json")


def _thread_state_dir(profile_id: str, executor: str, thread_id: str) -> Path:
    """Where one engine keeps its own state for one thread of one profile.

    Scoped this narrowly because a CLI's transcripts are a record of everything it
    was told: shared between profiles they would cross the account boundary, and
    shared between threads they would let a resume reach another conversation.
    """

    from app.core.paths import is_reparse_point, private_dir_mode
    from app.services.isolation.roots import roots

    if not profile_id or Path(profile_id).name != profile_id:
        raise ExternalAgentError("A profile-bound CLI home is required.")
    if not executor or Path(executor).name != executor:
        raise ExternalAgentError("Invalid external engine state scope.")
    # A vendor-reported id is untrusted, and dropping punctuation made distinct
    # ids such as "a/b" and "ab" share one transcript directory. Hash the Neo
    # scope key instead: stable across turns, with no lossy normalization.
    safe = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    root = roots().data_root / "agent-state"
    base = root / profile_id / executor / safe
    for directory in (root, root / profile_id, root / profile_id / executor, base):
        if is_reparse_point(directory):
            raise ExternalAgentError("A CLI state directory is a link.")
        directory.mkdir(parents=True, exist_ok=True, mode=private_dir_mode())
        if is_reparse_point(directory):
            raise ExternalAgentError("A CLI state directory is a link.")
        directory.chmod(private_dir_mode())
        if directory.stat().st_mode & 0o007:
            raise ExternalAgentError("A CLI state directory is accessible to other users.")
    return base


def _neo_thread_scope(session: AgentSession) -> str:
    """A stable transcript scope chosen by Neo, never by the external CLI."""

    return f"chat:{session.chat_id}" if session.chat_id is not None else f"session:{session.id}"


def _shared_config_paths() -> list[str]:
    """Shared tool configuration that a run must not be able to rewrite.

    These are the files the CLIs *execute* from -- hooks, MCP server commands,
    credential helpers. A run that could write one would have arranged to run its own
    code the next time the user started that tool by hand, outside any sandbox.
    """

    home = Path.home()
    return [
        str(home / ".claude" / "settings.json"),
        str(home / ".claude" / "settings.local.json"),
        str(home / ".claude.json"),
        str(home / ".codex" / "config.toml"),
        str(home / ".cursor" / "cli-config.json"),
        str(home / ".zshrc"),
        str(home / ".bashrc"),
        str(home / ".profile"),
        str(home / ".zprofile"),
    ]


def _neo_ports() -> list[int]:
    """Local services whose APIs can expose Neo data or runtime state."""

    from app.services.isolation.network import sensitive_ports

    return list(sensitive_ports())


def _active_profile_id() -> str:
    """The profile this run belongs to, for scoping grants.

    Read from the active profile storage directory the session middleware bound,
    so a run can never be authorized against another profile's grants.
    """

    from app.core.config import active_profile_storage_dir

    storage = active_profile_storage_dir.get()
    return Path(storage).name if storage else ""


def _instructions_segment(
    skills_preamble: str, role: str, step_instructions: str
) -> Labeled | None:
    """The role and skill text, labelled by where it came from.

    A handoff step's own instructions are Neo's shipped words, so they are public.
    A chat-level agent role and installed skill bodies are the user's own material:
    private, and withheld until granted.
    """

    builtin = (step_instructions or "").strip()
    private_parts = [part for part in (skills_preamble, role) if part and part.strip()]
    if not private_parts:
        return Labeled(builtin, public("builtin:handoff_step")) if builtin else None
    tags: list[str] = []
    if skills_preamble and skills_preamble.strip():
        tags.append("skill:enabled")
    if role:
        tags.append("agent_def:chat")
    text = "\n\n".join([*private_parts, builtin]) if builtin else "\n\n".join(private_parts)
    return Labeled(text, Label.private(*tags))


def _granted_segments(session: AgentSession, executor: str) -> list[Labeled]:
    """Material the user explicitly approved for this engine on this run.

    Empty until the approval surface lands; the builder already accepts it, so the
    path is one list away rather than a redesign.
    """

    del session, executor
    return []


def _objective_label(session) -> Label:
    """The objective's provenance, as the server recorded it when the run began.

    Typed this turn, it is the user's own words. Supplied by the server -- a task's
    text -- it keeps that text's label, which an external engine must not receive
    as though the user had typed it.
    """

    stored = getattr(session, "objective_label_json", None)
    if stored:
        return from_json(stored)
    if getattr(session, "task_id", None):
        # Created before objective provenance was recorded, and linked to a task:
        # the objective may have been the task's text, so it is not assumed typed.
        return Label.unknown("legacy_task_session_objective")
    return public(f"user_input:{session.id}")


def _withheld_notice(withheld) -> str:
    kinds = sorted({item.kind for item in withheld})
    return (
        "Some context was not sent to this engine because it has not been approved: "
        + ", ".join(kinds)
        + ". To share a specific detail, put only that detail in a new request to this engine."
    )


def run_step(
    session_id: str,
    *,
    executor: str,
    objective: str,
    mode: str | None = None,
    instructions: str = "",
    previous_answer: str | None = None,
    previous_executor_name: str | None = None,
    pre_state: snapshot.RepoState | None = None,
) -> RunOutcome:
    """Run one external executor turn against the session's repository."""

    settings = get_settings()
    if not chat_prefs.external_agents_enabled():
        raise ExternalAgentError(
            "External engines are off for this profile. Turn them on from the engine "
            "picker in Agent mode."
        )

    session = _session(session_id)
    spec = detect.spec(executor)
    if spec is None:
        raise ExternalAgentError(f"Unknown executor '{executor}'.")

    # Availability is checked here, not at submit time, because a CLI can be
    # signed out between the two. A refusal is an error, never a quiet fallback
    # to Neo's own loop: a turn the user gave to Claude Code must not come back
    # answered by something else without saying so.
    detect.require_available(executor)
    binary = detect.resolve_binary(executor)
    if not binary:  # pragma: no cover - require_available would have raised
        raise ExternalAgentError(f"{spec.name} is not installed.")
    # What runs is a verified copy in Neo's own store, never the path on PATH. The
    # install there is writable by anything running as this user, and metadata is no
    # proof of content, so the copy is what the run is bound to.
    package = _verified_package(executor, binary)
    _require_validated(executor, package)
    binary = package.entrypoint

    root = repo_root(session.repo_id)  # re-validates the live root on every call
    snapshot.ensure_git_worktree(root)

    # A live folder can be renamed and attached again under a new repo id. Its
    # inode identity carries the restriction across that new registration.
    root_stat = root.stat()
    identity_label = _artifact_labels().get(
        f"folder:{root_stat.st_dev}:{root_stat.st_ino}"
    )
    if identity_label.is_private():
        _artifact_labels().raise_label(f"repo:{session.repo_id or root}", identity_label)
    repo_record = repos_store.get_repo(session.repo_id) if session.repo_id else None
    if repo_record and repo_record.get("original_path"):
        try:
            original_stat = Path(repo_record["original_path"]).stat()
        except OSError:
            original_stat = None
        if original_stat is not None:
            origin_label = _artifact_labels().get(
                f"folder:{original_stat.st_dev}:{original_stat.st_ino}"
            )
            if origin_label.is_private():
                _artifact_labels().raise_label(
                    f"repo:{session.repo_id or root}", origin_label
                )

    try:
        approved = disclosure_approvals.consume_for_run(
            profile_id=_active_profile_id(),
            chat_id=session.chat_id,
            request_id=session.client_request_id,
            session_id=session.id,
            engine=executor,
            objective=objective,
        )
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise ExternalAgentError(
            f"The private-context approval is no longer valid ({exc}). Preview it again."
        ) from exc
    if approved is not None:
        # Private text can be written into files or Git objects by the engine.
        # Give it a full copy, with no shared inodes or Git object store, and
        # preserve the original workspace untouched for later review/delivery.
        original_label = _artifact_labels().get(f"repo:{session.repo_id or root}")
        if original_label.is_private():
            raise ExternalAgentError(
                "The source workspace already contains restricted material. "
                "Start from a clean repository and preview again."
            )
        try:
            root = scoped_copy.create(
                root, profile_id=_active_profile_id(), session_id=session.id
            )
        except (OSError, ValueError, scoped_copy.ScopedCopyError) as exc:
            raise ExternalAgentError(f"Could not isolate the approved run: {exc}") from exc
        _merge_meta(
            session_id,
            executor,
            {
                "scoped_workspace": str(root),
                "source_manifest_hash": scoped_copy.manifest_digest(root),
                "disclosure_id": approved.approval_id,
                "disclosure_tags": sorted(approved.payload.label.tags),
            },
        )

    resolved_mode = mode or session.mode or "normal"
    if resolved_mode == "plan" and not spec.capabilities.plan_mode:
        # Refused rather than silently downgraded: running an agent that can
        # write, when the user asked for one that cannot, is the single worst
        # way to be wrong here.
        raise ExternalAgentError(
            f"{spec.name} has no read-only planning mode, so this turn was not started."
        )
    # Unsafe execution escalates the *most permissive* mode; it never overrides
    # a restrictive one. A user who asked for plan mode asked for something that
    # cannot write, and a repository-level opt-in made months ago must not
    # silently turn that into an unsandboxed agent. So both gates AND an
    # explicit `auto` are required -- three conditions, of which the third is
    # the user's choice on this very turn.
    unsafe = resolved_mode == "auto" and _unsafe_allowed(session.repo_id)
    if unsafe:
        _LOG.warning(
            "external_agent_unsafe_mode session=%s executor=%s repo=%s",
            session_id,
            executor,
            session.repo_id,
        )

    resume_id = None if approved is not None else _resume_id(session, executor)
    before = pre_state if (pre_state is not None and approved is None) else snapshot.capture(root)

    history = []
    if session.chat_id and session.anchor_message_id:
        try:
            history = store.chat_history_before(session.chat_id, session.anchor_message_id)
        except Exception:  # pragma: no cover - context, never the run
            history = []

    change_summary = snapshot.summarize(root, before) if (resume_id or previous_answer) else None
    if change_summary == "No files were changed.":
        change_summary = None

    # The agent definition chosen in this chat is a role, and until now it was
    # a role only Neo's own loop ever heard: the session carried the snapshot
    # and this function never read it, so picking an agent changed nothing about
    # an external turn. It does now. A handoff step's own instructions are a
    # *second*, narrower role ("plan only", "review this"), so both apply and
    # the chat-level one goes first -- the same order Neo's own system prompt
    # uses, and the order in which the narrower instruction wins by being last.
    role = ((session.agent_definition_snapshot or {}).get("system_prompt") or "").strip()
    # Skills and a user-written agent role are the user's own private material, so
    # they are labelled and go through the gate like anything else. A shipped
    # built-in role is public and flows; a role the user wrote is withheld until
    # they approve it, and the run says so rather than quietly sending it.
    # Skills go ahead of both roles, in full. These CLIs take one prompt string
    # and hand nothing back until they are done, so there is no `load_skill`
    # round trip to make the way Neo's own loop does -- the choice is between
    # sending the instructions up front and not sending them at all. Recorded
    # here rather than hidden because it is a real difference between engines.
    # What does not differ: a skill the chat turned off contributes nothing.
    skills_preamble = skills_resolver.prepended_text(session.skills or [])

    payload = approved.payload if approved is not None else ext_disclosure.build_payload(
        objective=Labeled(objective, _objective_label(session)),
        history=[] if resume_id else history,
        engine=executor,
        thread_id=resume_id or session.id,
        profile_id=_active_profile_id(),
        instructions=_instructions_segment(skills_preamble, role, instructions),
        change_summary=(
            Labeled(
                "Files changed since you last worked in this repository"
                if resume_id
                else "Files already changed in this turn"
                " (read them from disk -- they are the source of truth):\n"
                + change_summary,
                public(f"workspace:{session.repo_id or 'repo'}"),
            )
            if change_summary
            else None
        ),
        granted=_granted_segments(session, executor),
    )
    if any(item.kind == "objective" for item in payload.withheld):
        # Withholding the objective would launch the engine with nothing to do. The
        # objective is server-supplied private text (a task's), and only an approval
        # of that exact text could send it; stop with the reason instead.
        raise ExternalAgentError(
            f"This run's objective is private Neo data, so {spec.name} cannot receive "
            "it without an approval of that exact text. Type the objective you want "
            "the engine to receive instead."
        )
    # The workspace carries its own restriction. A run is authorized over what it is
    # being sent *and* what the folder already holds: an earlier run may have written
    # granted material into it, and reading that back is the same disclosure again.
    labels = _artifact_labels()
    workspace_key = (
        f"scoped:{_active_profile_id()}:{session.id}"
        if approved is not None else f"repo:{session.repo_id or root}"
    )
    workspace_label = labels.get(workspace_key)
    if workspace_label.is_unknown():
        # First time Neo has seen this folder. It has received nothing from Neo, so
        # its contents are the user's own code -- recorded explicitly rather than
        # inferred from the absence of a record.
        workspace_label = labels.raise_label(
            workspace_key, public(f"workspace:{session.repo_id or 'repo'}")
        )
    if not workspace_label.is_public():
        decision = authorize(
            "external_agent",
            Labeled(payload.text, workspace_label),
            Destination(
                profile_id=_active_profile_id(),
                engine=executor,
                thread_id=resume_id or session.id,
            ),
            grants=None,
        )
        if not decision.allowed:
            raise ExternalAgentError(
                f"This folder holds material that was approved for another engine or "
                f"thread, so {spec.name} cannot be run in it without a new approval. "
                f"({decision.reason})"
            )
    if not payload.label.is_public() and not payload.label.is_unknown():
        # Committed before the run starts: a crash between disclosing and recording
        # would otherwise leave private material in a folder marked public.
        labels.raise_label(workspace_key, payload.label)

    prompt = payload.text
    if payload.withheld:
        store.append_event(
            session_id,
            events.STATUS,
            {
                "content": _withheld_notice(payload.withheld),
                "executor": executor,
                "withheld": [
                    {"kind": item.kind, "reason": item.reason, "tags": list(item.tags)}
                    for item in payload.withheld
                ],
            },
        )

    # The per-chat tool toggles are forwarded only where the CLI can genuinely
    # enforce them. For an executor without a denylist there is nothing honest
    # to send, and quietly dropping them is the point: Neo must not behave as
    # though a restriction were applied.
    capabilities = spec.capabilities
    disabled = list(session.disabled_tools or []) if capabilities.tool_denylist else []

    adapter = _ADAPTERS.get(executor)
    if adapter is None:  # pragma: no cover - detect.spec would have refused first
        raise ExternalAgentError(f"No adapter for executor '{executor}'.")

    plan = adapter.invocation(
        InvocationContext(
            binary=binary,
            prompt=prompt,
            mode=resolved_mode,
            cwd=str(root),
            resume_id=resume_id,
            new_session_id=store.new_id(),
            disabled_tools=disabled,
            # None means "leave it alone", and each CLI then uses the model its
            # own configuration names. A value here is the chat's Model chip,
            # which is per engine because the ids are not interchangeable. The
            # environment override is last and exists for the end-to-end
            # harness, which has no chat to set a chip on and has to tell "the
            # integration is broken" from "this machine's config pins a model
            # the account cannot use".
            model=(
                models.chosen(session.external_models, executor) or models.env_override(executor)
            ),
            effort=models.chosen(session.external_efforts, executor),
            unsafe=unsafe,
        )
    )
    argv, translate = plan.argv, plan.translate
    if plan.session_id:
        # Recorded before the process starts, so a run that dies before saying
        # anything can still be resumed.
        _merge_meta(session_id, executor, {spec.session_id_key: plan.session_id})

    store.append_event(
        session_id,
        events.STATUS,
        {
            "content": f"{spec.name} {'resuming' if resume_id else 'starting'} in {root.name}",
            "executor": executor,
            "audit": _audit(executor, argv, root, resolved_mode, unsafe, resume_id),
        },
    )

    collected: dict[str, Any] = {"final": None, "meta": {}, "outcome": None, "error": None}
    source_label = output_label(store.get_session(session_id) or {}, session_id, executor)

    def handle(line: dict[str, Any] | str) -> None:
        if not isinstance(line, dict):
            return
        try:
            produced = translate(line)
        except Exception:  # pragma: no cover - a parse slip must not kill a run
            _LOG.warning("external_agent_translate_failed executor=%s", executor, exc_info=True)
            return
        for event in produced:
            _apply(session_id, executor, event, collected, source_label=source_label)

    # Per-thread state, so an engine keeps its own transcripts and caches where no
    # other profile or thread can read them, and the user's real ~/.claude and
    # ~/.codex stay denied outright.
    thread_home = _thread_state_dir(
        _active_profile_id(), executor,
        f"session:{session.id}" if approved is not None else _neo_thread_scope(session),
    )
    if approved is not None:
        labels.raise_label(
            f"thread:{_active_profile_id()}:{executor}:{session.id}", payload.label
        )
    try:
        credential = credentials.for_executor(
            executor, binary=binary, home=str(thread_home)
        )
    except credentials.CredentialUnavailable as exc:
        raise ExternalAgentError(str(exc)) from exc
    credentials.write_files(credential)

    child_env = {
        **env_module.build_env(
            home_env=spec.home_env,
            home_dir=str(getattr(settings, spec.home_setting, "") or ""),
        ),
        "HOME": str(thread_home),
        **credential.env,
    }
    # A run that cannot finish before its credential expires is not started: being
    # cut off part-way through editing is worse than not starting.
    timeout = float(settings.external_agent_timeout_seconds)
    remaining = credential.remaining()
    if remaining is not None:
        timeout = min(timeout, max(0.0, remaining - 30))
        if timeout < 60:
            raise ExternalAgentError(
                f"{spec.name}'s session expires too soon to start a run. Refresh it "
                "and try again."
            )

    if approved is not None and not disclosure_approvals.still_active(
        _active_profile_id(), approved.approval_id, session.id
    ):
        raise ExternalAgentError("The private-context approval expired or was revoked.")

    outcome = _launch(
        argv,
        root=root,
        env=child_env,
        timeout=timeout,
        thread_state=str(thread_home),
        on_line=handle,
        session_id=session_id,
        profile_id=_active_profile_id(),
        mode=resolved_mode,
        stdin_text=plan.stdin_text,
        exec_paths=(package.root,),
    )

    # The stream is more informative than the exit code: a CLI that reported a
    # result and then exited non-zero still did the work, and one that exited 0
    # having reported a failure did not.
    if collected["outcome"] == "failed":
        outcome.outcome = "failed"
        outcome.error = collected["error"] or outcome.error
    elif collected["outcome"] == "completed" and not outcome.cancelled and not outcome.timed_out:
        outcome.outcome = "completed"
        outcome.error = None

    outcome.final_text = collected["final"] or outcome.final_text or ""
    outcome.meta = collected["meta"]
    outcome.external_session_id = (store.get_session(session_id) or {}).get(
        "external_session_id"
    ) or resume_id

    if approved is None:
        try:
            changed = snapshot.record(session_id, session.repo_id or "", root, before)
            if changed:
                store.append_event(
                    session_id,
                    events.STATUS,
                    {"content": f"{len(changed)} file(s) changed", "executor": executor},
                )
        except Exception:  # pragma: no cover - the run happened; journalling is after
            _LOG.warning("external_agent_journal_failed session=%s", session_id, exc_info=True)

    _merge_meta(
        session_id,
        executor,
        {
            **outcome.meta,
            "exit_code": outcome.exit_code,
            "unsafe": unsafe or None,
            "mode": resolved_mode,
        },
    )
    return outcome


def _apply(
    session_id: str,
    executor: str,
    event: ExternalEvent,
    collected: dict[str, Any],
    *,
    source_label: Label,
) -> None:
    """Persist one translated event and fold its side-channel facts in."""

    if event.type:
        payload = dict(event.payload)
        payload.setdefault("executor", executor)
        # The CLI is untrusted. Replace, rather than trust, any provenance it
        # places in an event payload before the event enters Neo's durable log.
        payload["source_label"] = to_json(source_label)
        store.append_event(session_id, event.type, payload)

    if event.external_session_id:
        store.update_session(session_id, {"external_session_id": event.external_session_id})
        spec = detect.spec(executor)
        key = spec.session_id_key if spec else "session_id"
        _merge_meta(session_id, executor, {key: event.external_session_id})
    if event.final_text:
        collected["final"] = event.final_text
    if event.meta:
        collected["meta"].update(event.meta)
    if event.outcome:
        collected["outcome"] = event.outcome
    if event.error:
        collected["error"] = event.error


def _audit(
    executor: str,
    argv: list[str],
    root: Path,
    mode: str,
    unsafe: bool,
    resume_id: str | None,
) -> dict[str, Any]:
    """Enough to answer "what exactly did Neo run?".

    The whole argv is recorded, because the prompt is no longer in it: every
    adapter passes it on stdin. This used to drop ``argv[-1]`` on the assumption
    that the prompt was the last element, which was true only for Codex -- for the
    other three the prompt sat at ``argv[2]`` and was copied verbatim into this
    record, into the event log, and from there to the browser.
    """

    return {
        "executor": executor,
        "argv": list(argv),
        "cwd": str(root),
        "mode": mode,
        "unsafe": unsafe,
        "resumed": bool(resume_id),
    }


__all__ = ["STEP_STARTED", "run_step"]
