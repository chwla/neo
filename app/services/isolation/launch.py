"""The one rule for starting a command or a test process.

Neo starts processes that run code it did not write: the allowlisted commands an
agent or a person asks for, and test suites, which are the repository's own code
and on Neo's paths often code an agent has just edited. Where a validated sandbox
exists (macOS, Seatbelt) each of those runs inside it. Where none does -- Windows,
Linux, Docker, or a Mac whose sandbox probe failed -- the process would run with
the user's whole account: every file they can read, every key, every other Neo
profile's data, and the network.

This module is where that choice is made, and every launch path calls it
immediately before starting a process, rather than trusting the permission
resolver or the interface to have made it already:

* **isolated** whenever the sandbox is available, and only that: a sandbox that
  fails part way into a launch is a refusal, never a cue to run without one;
* **unsandboxed** only with a person's approval of *this* launch, given after they
  were shown :data:`UNSANDBOXED_EXPOSURE`;
* **refused** otherwise. Nothing unattended counts as that approval -- not AUTO
  mode, not a session grant, and not a model approving its own call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.services import isolation

_LOG = logging.getLogger(__name__)

#: Shown to the person before they may approve an unsandboxed launch, and the text
#: an agent approval must have carried for that approval to count.
UNSANDBOXED_EXPOSURE = (
    "Neo cannot sandbox commands on this computer, so this would run with your full "
    "user account. It, and any code it runs (a test suite runs the repository's own "
    "code), could read or change any file you can, including your keys and other Neo "
    "profiles' data, and could reach the network."
)

LaunchMode = Literal["isolated", "unsandboxed"]


class UnsandboxedLaunchRefused(isolation.IsolationRefused):
    """No sandbox is available, and no person approved this launch without one."""


@dataclass(frozen=True)
class HumanApproval:
    """A person's approval of one specific launch without a sandbox.

    ``subject`` names the launch it covers: a command run's id or a test run's id.
    ``approver`` records who gave it -- ``user`` for the signed-in person acting in
    the interface, ``agent-approval:<id>`` for an approval card they answered.
    """

    subject: str
    approver: str
    acknowledged_exposure: bool


def sandbox_available() -> bool:
    return bool(isolation.status().get("supported"))


def authorize(purpose: str, *, subject: str, approval: HumanApproval | None) -> LaunchMode:
    """Decide how this launch may run, or raise :class:`UnsandboxedLaunchRefused`."""

    state = isolation.status()
    if state.get("supported"):
        return "isolated"
    if (
        approval is not None
        and approval.acknowledged_exposure
        and approval.approver
        and subject
        and approval.subject == subject
    ):
        _LOG.warning(
            "unsandboxed_launch purpose=%s subject=%s approver=%s",
            purpose,
            subject,
            approval.approver,
        )
        return "unsandboxed"
    reason = state.get("reason") or "no sandbox is available"
    raise UnsandboxedLaunchRefused(
        f"Not run: Neo cannot sandbox this here ({reason}), and running it without a "
        f"sandbox needs your explicit approval of this exact command. {UNSANDBOXED_EXPOSURE}"
    )
