"""The sandbox policy for one run, as a Seatbelt profile.

Shape and reasoning
-------------------
The profile is ``(allow default)`` followed by targeted denies. That is a blocklist,
and a blocklist is fail-open for anything not enumerated -- which is why
:mod:`app.services.isolation.attest` re-checks the result before every launch rather
than trusting this file to be complete.

The alternative was ruled out by measurement, not preference: under ``(deny default)``
the CLIs cannot reach the services they need to run at all, and Neo would be offering
an engine that never works. What that costs is recorded here and in the contract's
residual risks, and the denies below are ordered so the expensive ones come last.

Every rule here was measured on macOS 26.6.2; the evidence, including the rules that
looked right and did not work, is in docs/external-agents/cli-surface.md. The two
that matter most:

* ``(deny signal (target others))`` does **not** work -- a sandboxed process still
  killed an unrelated one. Signals have to be denied wholesale and then re-allowed
  for self, same-sandbox and children.
* Path denies are evaluated against the canonical vnode path, so ``/tmp``, the
  ``/System/Volumes/Data`` firmlink, case variants, doubled slashes, ``..`` and
  ``..namedfork/rsrc`` are all covered by one ``subpath`` rule. ``/.vol`` inode
  paths are covered too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.isolation.roots import Roots, roots


def _literal(path: str | Path) -> str:
    """One path, quoted for SBPL. Backslashes and quotes would end the string."""

    text = str(path)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True)
class RunPolicy:
    """Everything one run is allowed, as data rather than as text."""

    workspace: Path
    thread_state: Path | None = None
    exec_paths: tuple[Path, ...] = ()
    temp_dir: Path | None = None
    marker_denied: Path | None = None
    marker_allowed: Path | None = None
    deny_ports: tuple[int, ...] = ()
    shared_config_paths: tuple[Path, ...] = ()
    read_only_workspace: bool = False
    deny_network: bool = False

    def writable(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        if not self.read_only_workspace:
            paths.append(self.workspace)
        if self.thread_state is not None:
            paths.append(self.thread_state)
        if self.temp_dir is not None:
            paths.append(self.temp_dir)
        return tuple(paths)


#: Bumped whenever a change here alters what a run is allowed to do. It is part of
#: an engine's compatibility record, so loosening or tightening the policy forces a
#: fresh validation rather than silently re-using an observation made under the old
#: rules.
POLICY_VERSION = 1


def policy_version() -> str:
    """Bind compatibility to every rule and launcher module in this package."""

    import hashlib

    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.name.encode("utf-8"))
        digest.update(source.read_bytes())
    return f"v{POLICY_VERSION}:{digest.hexdigest()[:16]}"


def build_profile(policy: RunPolicy, known: Roots | None = None) -> str:
    """Render ``policy`` as a Seatbelt profile."""

    known = known or roots()
    lines: list[str] = [
        "(version 1)",
        ";; Generated per run by app/services/isolation/policy.py. Do not edit by hand.",
        "(allow default)",
    ]

    # --- reads and writes -------------------------------------------------
    protected = [p for p in known.protected() if p not in (policy.workspace,)]
    if protected:
        lines.append(";; Neo's own data, and the user's own tool state.")
        lines.append(
            "(deny file-read* file-write* file-link "
            + " ".join(f"(subpath {_literal(p)})" for p in protected)
            + ")"
        )

    # Writes are an allowlist of their own: a run may write its workspace, its own
    # thread state and its own temp directory, and nothing else anywhere.
    lines.append(";; Writes: nothing outside this run's own directories.")
    lines.append('(deny file-write* (subpath "/"))')
    writable = policy.writable()
    if writable:
        lines.append(
            "(allow file-write* "
            + " ".join(f"(subpath {_literal(p)})" for p in writable)
            + ")"
        )
    # The device files a normal program expects to be able to write.
    lines.append(
        '(allow file-write-data (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/dtracehelper") (regex #"^/dev/tty") (regex #"^/dev/fd/"))'
    )
    lines.append('(allow file-write* (regex #"^/dev/tty") (regex #"^/dev/fd/"))')

    # Re-allow this run's own scoped entries, which the blanket protected deny above
    # would otherwise have caught (workspaces/ and agent-state/ are scoped roots).
    own = [p for p in (policy.workspace, policy.thread_state, policy.temp_dir) if p]
    if own:
        lines.append(";; This run's own scoped directories.")
        lines.append(
            "(allow file-read* "
            + " ".join(f"(subpath {_literal(p)})" for p in own)
            + ")"
        )
        # A CLI can open its HOME directly yet fail while canonicalizing it:
        # realpath/stat inspects every parent, which the scoped-root deny catches.
        # Permit metadata on the exact ancestors only, never listing or reading
        # their contents or any sibling profile/thread entry.
        ancestors = {
            parent
            for path in own
            for parent in path.parents
            if parent == known.data_root or parent.is_relative_to(known.data_root)
        }
        if ancestors:
            lines.append(
                "(allow file-read-metadata "
                + " ".join(f"(literal {_literal(p)})" for p in sorted(ancestors))
                + ")"
            )

    # --- the verified engine package -------------------------------------
    if policy.exec_paths:
        lines.append(";; The verified engine package: read and execute, never write.")
        lines.append(
            "(allow file-read* process-exec "
            + " ".join(f"(subpath {_literal(p)})" for p in policy.exec_paths)
            + ")"
        )
    if known.exec_root:
        lines.append(f"(deny file-write* (subpath {_literal(known.exec_root)}))")

    # --- shared executable configuration ---------------------------------
    if policy.shared_config_paths:
        lines.append(
            ";; Shared configuration the tools *execute* from. Readable where the CLI "
            "needs its own settings, never writable: this is where an escape would "
            "persist itself for the next time the user runs the tool by hand."
        )
        lines.append(
            "(deny file-write* "
            + " ".join(f"(literal {_literal(p)})" for p in policy.shared_config_paths)
            + ")"
        )

    # --- process supervision markers -------------------------------------
    if policy.marker_denied is not None:
        lines.append(
            ";; Run membership. Denying one marker and allowing a sibling is what lets "
            ";; Neo tell this run's processes from every other sandboxed process on the "
            ";; machine -- including Apple's own, which deny unknown paths too."
        )
        lines.append(f"(deny file-read* (literal {_literal(policy.marker_denied)}))")
    if policy.marker_allowed is not None:
        lines.append(f"(allow file-read* (literal {_literal(policy.marker_allowed)}))")

    # --- network ----------------------------------------------------------
    if policy.deny_network:
        lines.append(";; A Neo filesystem helper has no network work to do.")
        lines.append("(deny network-outbound)")
    if policy.deny_ports:
        lines.append(";; Neo's own HTTP API is not a service its agents may call.")
        for port in policy.deny_ports:
            # Only "*" and "localhost" are accepted as the host here; a literal
            # address is a profile syntax error. Denying both spellings covers
            # 127.0.0.1, ::1 and the hostname alike.
            lines.append(f'(deny network-outbound (remote ip "localhost:{port}"))')
            lines.append(f'(deny network-outbound (remote ip "*:{port}"))')

    # --- unix sockets ------------------------------------------------------
    # A unix socket is a door to another process running as the user. ssh-agent
    # signs with the user's SSH keys for anyone who connects, so a run that reaches
    # it can authenticate as the user without reading a file; gpg-agent and password
    # manager agents are the same. Measured: a run connected to the real
    # SSH_AUTH_SOCK. Only name resolution, syslog and the run's own directories stay.
    lines.append(";; Unix sockets: only DNS, syslog and this run's own directories.")
    lines.append("(deny network-outbound (remote unix-socket))")
    # One rule per path. Measured: two path filters inside a single
    # ``(remote unix-socket ...)`` do not combine as alternatives, and syslog was
    # refused; separate rules each match on their own.
    for service in ("/private/var/run/mDNSResponder", "/private/var/run/syslog"):
        lines.append(
            f"(allow network-outbound (remote unix-socket (path-literal {_literal(service)})))"
        )
    for path in own:
        lines.append(
            f"(allow network-outbound (remote unix-socket (subpath {_literal(path)})))"
        )

    # --- processes and OS-brokered state ---------------------------------
    lines.extend(
        [
            ";; Signals: `(deny signal (target others))` was measured not to work, so",
            ";; they are denied wholesale and re-allowed for this run's own tree.",
            "(deny signal)",
            "(allow signal (target self) (target same-sandbox) (target children))",
            ";; Another process's working directory, open files and executable path",
            ";; (proc_pidinfo) would hand over Neo's database paths. This does NOT cover",
            ";; its environment: sysctl(KERN_PROCARGS2) is not mediated by Seatbelt, so",
            ";; Neo refuses agent-influenced runs while its own environment holds",
            ";; credentials instead (isolation/environment.py).",
            "(deny process-info* (target others))",
            ";; Asking another process to act is how a sandbox is escaped: LaunchServices",
            ";; and AppleEvents both spawn work outside this profile, and launchd jobs",
            ";; outlive the run entirely. job-creation is also denied to every sandboxed",
            ";; process by default (measured); it is stated so the intent does not rest",
            ";; on that default.",
            "(deny lsopen)",
            "(deny appleevent-send)",
            "(deny job-creation)",
            ";; OS-brokered user state that no path rule covers: the pasteboard answers",
            ";; with whatever the user last copied, and Spotlight answers with indexed",
            ";; filenames and content from directories this profile denies.",
            '(deny mach-lookup (global-name "com.apple.pasteboard.1")'
            ' (global-name "com.apple.pboard")'
            ' (global-name-prefix "com.apple.metadata.mds"))',
            ";; The screen, by the same reasoning. Capture goes through WindowServer and",
            ";; the screen-recording daemon, and was measured reachable from a run, held",
            ";; back only by TCC's Screen Recording permission -- which is granted to the",
            ";; responsible app, so one grant to a terminal would extend to every run",
            ";; started under it. Coding CLIs are headless and need neither.",
            '(deny mach-lookup (global-name-prefix "com.apple.windowserver")'
            ' (global-name "com.apple.replayd")'
            ' (global-name-prefix "com.apple.screencapture"))',
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = ["RunPolicy", "build_profile"]
