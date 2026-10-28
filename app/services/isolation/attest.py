"""Prove the policy actually holds, immediately before every launch.

The macOS profile is ``(allow default)`` plus denies, so it is only as good as its
list. A rule that stops working -- an OS change, a path that moved, a mistake in the
generator -- would otherwise be invisible: runs would keep succeeding, with the
boundary quietly gone.

So before each launch Neo runs a few bytes of Python under the *identical* profile
and checks that it cannot read a freshly written sentinel in each protected class,
cannot reach Neo's port, and *can* read its own workspace. The last one is as
important as the others: a profile that denies everything would pass a test that only
looked for refusals, and would then break every run.

Cheap enough to do every time (a few tens of milliseconds), and the whole point is
that it is not cached.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from app.services.isolation.policy import RunPolicy

SENTINEL_PREFIX = "NEO-ATTEST-"

_PROBE = textwrap.dedent(
    """
    import json, os, socket, sys
    plan = json.loads(sys.argv[1])
    out = {"reads": {}, "writes": {}, "connect": {}}
    for name, path in plan["must_not_read"].items():
        try:
            with open(path, "rb") as fh:
                out["reads"][name] = "read:" + fh.read(64).decode("utf-8", "replace")
        except OSError as exc:
            out["reads"][name] = "denied"
    for name, path in plan["must_read"].items():
        try:
            if plan["dirs"].get(name):
                os.listdir(path)
            else:
                with open(path, "rb") as fh:
                    fh.read(1)
            out["reads"][name] = "ok"
        except OSError as exc:
            out["reads"][name] = "blocked:" + type(exc).__name__
    for name, path in plan["must_not_write"].items():
        try:
            with open(path, "ab") as fh:
                fh.write(b"x")
            out["writes"][name] = "wrote"
        except OSError:
            out["writes"][name] = "denied"
    for name, port in plan["must_not_connect"].items():
        try:
            socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()
            out["connect"][name] = "connected"
        except OSError:
            out["connect"][name] = "denied"
    print(json.dumps(out))
    """
)


class AttestationFailed(RuntimeError):
    """The policy did not hold. Nothing is launched."""


@dataclass(frozen=True)
class Attestation:
    checks: dict
    sentinels: tuple[Path, ...]


def _write_sentinel(path: Path) -> str | None:
    value = SENTINEL_PREFIX + secrets.token_hex(8)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return value
    except OSError:
        return None


def attest(
    policy: RunPolicy,
    profile_text: str,
    *,
    protected: tuple[Path, ...],
    sandbox_exec: str,
    timeout: float = 30.0,
) -> Attestation:
    """Run the probe under ``profile_text`` and raise unless every check holds."""

    sentinels: list[Path] = []
    must_not_read: dict[str, str] = {}
    # One sentinel per protected class, in a directory that class owns. A sentinel
    # that could not be written is skipped rather than silently counted as a pass.
    for index, root in enumerate(protected[:12]):
        candidate = root / f".neo-attest-{secrets.token_hex(4)}" if root.is_dir() else root
        if root.is_dir():
            if _write_sentinel(candidate) is None:
                continue
            sentinels.append(candidate)
            must_not_read[f"protected_{index}"] = str(candidate)
        elif root.exists():
            must_not_read[f"protected_{index}"] = str(root)

    must_read = {"workspace": str(policy.workspace)}
    read_dirs = {"workspace": True}
    probe_file = policy.workspace / f".neo-attest-{secrets.token_hex(4)}"
    wrote_probe = _write_sentinel(probe_file) is not None
    if wrote_probe:
        must_read["workspace_file"] = str(probe_file)
        read_dirs["workspace_file"] = False

    must_not_write = {}
    for index, root in enumerate(protected[:6]):
        if root.is_file():
            must_not_write[f"protected_{index}"] = str(root)

    must_not_connect = {f"port_{port}": port for port in policy.deny_ports}

    plan = {
        "must_not_read": must_not_read,
        "must_read": must_read,
        "must_not_write": must_not_write,
        "must_not_connect": must_not_connect,
        "dirs": read_dirs,
    }

    profile_dir = Path(policy.temp_dir or policy.workspace)
    with_profile = profile_dir / f".neo-attest-{secrets.token_hex(4)}.sb"
    try:
        with_profile.write_text(profile_text, encoding="utf-8")
        completed = subprocess.run(
            [
                sandbox_exec,
                "-f",
                str(with_profile),
                sys.executable,
                "-I",
                "-c",
                _PROBE,
                json.dumps(plan),
            ],
            cwd=str(policy.workspace),
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationFailed(f"the isolation self-check could not run: {exc}") from exc
    finally:
        try:
            with_profile.unlink()
        except OSError:
            pass
        for path in sentinels:
            try:
                path.unlink()
            except OSError:
                pass
        if wrote_probe:
            try:
                probe_file.unlink()
            except OSError:
                pass

    if completed.returncode != 0:
        raise AttestationFailed(
            f"the isolation self-check exited {completed.returncode}: "
            f"{completed.stderr.strip()[:400]}"
        )
    try:
        observed = json.loads(completed.stdout)
    except ValueError as exc:
        raise AttestationFailed("the isolation self-check produced no verdict") from exc

    failures: list[str] = []
    for name, value in observed["reads"].items():
        if name.startswith("protected_") and str(value).startswith("read:"):
            failures.append(f"{name} was readable")
        if name.startswith("workspace") and str(value).startswith("blocked"):
            failures.append(f"{name} was not readable, so no run could work")
    for name, value in observed["writes"].items():
        if value == "wrote":
            failures.append(f"{name} was writable")
    for name, value in observed["connect"].items():
        if value == "connected":
            failures.append(f"{name} was reachable")
    if failures:
        raise AttestationFailed(
            "the sandbox did not hold: " + "; ".join(failures)
        )
    return Attestation(checks=observed, sentinels=tuple(sentinels))


def free_port_is_denied(port: int) -> bool:
    """Whether ``port`` currently has a listener, for the deny list's own sake."""

    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


__all__ = ["Attestation", "AttestationFailed", "attest"]
