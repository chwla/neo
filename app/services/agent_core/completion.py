"""Deciding whether the objective was actually met.

The loop calls this when the model produces a turn with no tool calls. That
signal means "the model stopped asking for things", and treating it as success
is how the previous design could report a finished run that had done nothing.

Two independent inputs decide the outcome: the model's own judgement about the
objective, and an evidence ledger built from tools that produce checkable
results. Only agreement between them yields ``verified_complete``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.services.agent_core.types import AgentSession, Evidence, StopVerdict
from app.services.llm import LLMMessage

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

_SYSTEM = (
    "You judge whether an objective has been completed. Answer with strict JSON only: "
    '{"complete": bool, "blocked": bool, "unmet_criteria": [string], "summary": string}. '
    "Judge only from the recorded actions and evidence supplied. Do not assume an action "
    "succeeded because it was attempted.\n"
    "complete=true only when the recorded actions actually accomplished the objective.\n"
    "blocked=true ONLY when the agent cannot make further progress without something it "
    "cannot get by itself: user input, credentials, a missing repository, or a capability "
    "it does not have. Work that is merely unfinished, described but not performed, or not "
    "yet verified is NOT blocked -- it is incomplete, so return complete=false, blocked=false "
    "and list what still needs doing in unmet_criteria."
)

#: How many times a run may be told "you are not finished" before the loop gives
#: up on nudging it. One is help; more is a loop the user pays for.
MAX_ADJUDICATIONS = 1


def evidence_verdict(evidence: list[Evidence]) -> bool:
    """Whether the ledger supports a claim of success.

    The last record of each kind wins, so a test run that failed, was fixed and
    then passed verifies -- while a later regression correctly un-verifies an
    earlier pass. An empty ledger can never verify.
    """

    if not evidence:
        return False
    latest: dict[str, Evidence] = {}
    for item in evidence:
        latest[item.kind] = item
    if any(not item.passed for item in latest.values()):
        return False
    return any(item.passed for item in latest.values())


def _decode(content: str) -> dict[str, Any]:
    match = _FENCE.search(content)
    payload = match.group(1) if match else content
    start, end = payload.find("{"), payload.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in adjudication")
    decoded = json.loads(payload[start : end + 1])
    if not isinstance(decoded, dict):
        raise ValueError("adjudication was not an object")
    return decoded


class CompletionAdjudicator:
    def __init__(self, llm_factory: Callable[[], Any]) -> None:
        self.llm_factory = llm_factory

    def _prompt(self, session: AgentSession, transcript: list[LLMMessage]) -> str:
        ledger = (
            "\n".join(
                f"- {item.kind}: {'passed' if item.passed else 'FAILED'} — {item.detail}"
                for item in session.evidence
            )
            or "(no verification evidence was recorded)"
        )
        actions = (
            "\n".join(
                f"- {message.role}: {message.content[:300]}"
                for message in transcript[-12:]
                if message.role in {"assistant", "tool"} and message.content
            )
            or "(no actions recorded)"
        )
        todo = (
            "\n".join(f"- [{item.status}] {item.title}" for item in session.todo)
            or "(no checklist was used)"
        )
        return (
            f"Objective:\n{session.objective}\n\n"
            f"Checklist:\n{todo}\n\n"
            f"Recent actions:\n{actions}\n\n"
            f"Verification evidence:\n{ledger}"
        )

    def adjudicate(self, session: AgentSession, transcript: list[LLMMessage]) -> StopVerdict:
        verified = evidence_verdict(session.evidence)
        try:
            result = self.llm_factory().chat_with_metadata(
                [
                    LLMMessage(role="system", content=_SYSTEM),
                    LLMMessage(role="user", content=self._prompt(session, transcript)),
                ],
                temperature=0.0,
                num_predict=400,
            )
            decoded = _decode(result.content)
        except Exception:
            # The work already happened, so it is not thrown away -- but
            # "verified" means the evidence *and* the objective agree, and with
            # no adjudication there is nothing to agree with. Passing tests prove
            # tests pass, not that the objective was met, so this degrades rather
            # than letting an outage upgrade the outcome.
            return StopVerdict(
                stop_reason="unverified_complete",
                summary=session.summary or "The run finished; completion could not be adjudicated.",
            )

        summary = str(decoded.get("summary") or "").strip() or "The run finished."
        blocked = bool(decoded.get("blocked"))
        complete = bool(decoded.get("complete"))
        unmet = [str(item) for item in (decoded.get("unmet_criteria") or []) if str(item).strip()]

        if blocked:
            return StopVerdict(stop_reason="blocked", summary=summary, unmet_criteria=unmet)
        if complete:
            return StopVerdict(
                stop_reason="verified_complete" if verified else "unverified_complete",
                summary=summary,
            )
        if session.adjudications < MAX_ADJUDICATIONS:
            return StopVerdict(
                stop_reason="unverified_complete",
                summary=summary,
                continue_working=True,
                unmet_criteria=unmet or ["The objective does not appear to be complete yet."],
            )
        return StopVerdict(stop_reason="unverified_complete", summary=summary, unmet_criteria=unmet)
