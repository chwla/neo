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

import logging
import os
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services import chat_prefs
from app.services.agent_core import events, store
from app.services.agent_core.types import AgentSession, Budgets
from app.services.agent_core.workspace import repo_root
from app.services.external_agents import context, detect, runner, snapshot
from app.services.external_agents import env as env_module
from app.services.external_agents.adapters import claude_code as claude_adapter
from app.services.external_agents.adapters import codex as codex_adapter
from app.services.external_agents.types import (
    ExternalAgentError,
    ExternalEvent,
    InvocationContext,
    RunOutcome,
)

#: Which adapter drives which CLI. The whole of Neo's engine-specific dispatch,
#: in one table -- everything past this point is uniform.
_ADAPTERS = {"claude_code": claude_adapter, "codex": codex_adapter}

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

    root = repo_root(session.repo_id)  # re-validates the live root on every call
    snapshot.ensure_git_worktree(root)

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

    resume_id = _resume_id(session, executor)
    before = pre_state if pre_state is not None else snapshot.capture(root)

    history = []
    if session.chat_id and session.anchor_message_id:
        try:
            history = store.chat_history_before(session.chat_id, session.anchor_message_id)
        except Exception:  # pragma: no cover - context, never the run
            history = []

    change_summary = snapshot.summarize(root, before) if (resume_id or previous_answer) else None
    if change_summary == "No files were changed.":
        change_summary = None

    prompt = context.build_prompt(
        objective,
        history=history,
        previous_answer=previous_answer,
        previous_executor_name=previous_executor_name,
        change_summary=change_summary,
        instructions=instructions,
        resuming=bool(resume_id),
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
            # Unset so each CLI uses the model in the user's own configuration.
            # The override exists for the end-to-end harness, which has to tell
            # "the integration is broken" from "this machine's config pins a
            # model the account cannot use".
            model=os.environ.get("NEO_CODEX_MODEL") or None,
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

    def handle(line: dict[str, Any] | str) -> None:
        if not isinstance(line, dict):
            return
        try:
            produced = translate(line)
        except Exception:  # pragma: no cover - a parse slip must not kill a run
            _LOG.warning("external_agent_translate_failed executor=%s", executor, exc_info=True)
            return
        for event in produced:
            _apply(session_id, executor, event, collected)

    outcome = runner.run_process(
        argv,
        cwd=str(root),
        env=env_module.build_env(
            home_env=spec.home_env,
            home_dir=str(getattr(settings, spec.home_setting, "") or ""),
        ),
        timeout=float(settings.external_agent_timeout_seconds),
        on_line=handle,
        is_cancelled=lambda: _is_cancelled(session_id),
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
    outcome.external_session_id = (
        (store.get_session(session_id) or {}).get("external_session_id") or resume_id
    )

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
    session_id: str, executor: str, event: ExternalEvent, collected: dict[str, Any]
) -> None:
    """Persist one translated event and fold its side-channel facts in."""

    if event.type:
        payload = dict(event.payload)
        payload.setdefault("executor", executor)
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

    The prompt -- always the last element of argv -- is deliberately dropped. It
    is already the objective and the conversation, both of which are in the
    transcript, and it is the one part of the command line likely to carry the
    user's own sensitive text; recording it again would copy it into a second
    place for no gain.
    """

    return {
        "executor": executor,
        "argv": list(argv[:-1]),
        "cwd": str(root),
        "mode": mode,
        "unsafe": unsafe,
        "resumed": bool(resume_id),
    }


__all__ = ["STEP_STARTED", "run_step"]
