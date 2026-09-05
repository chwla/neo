"""The environment an external CLI is handed.

This is an allowlist, and it is the deliberate *inverse* of the one in
``command_sandbox.runner``. That module points ``HOME`` into the workspace so a
sandboxed command cannot reach the user's real configuration. Here we must do
the opposite: the entire point of driving Claude Code or Codex is that they
authenticate as the user, from the user's own credential directory, against the
user's own subscription. Redirecting ``HOME`` would break exactly the thing the
feature exists to provide.

What does not change is that the child gets an allowlist rather than Neo's
environment. Neo's own secrets -- the connector master key, database URLs, API
keys -- have no business inside a process running someone else's agent loop, and
an inherited environment leaks them by default. So the rule is: pass what the CLI
needs to work and to reach the network, and nothing else.

Neo never reads, parses or copies the credential files themselves. It names the
directory and lets the CLI do its own authentication.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Variables the child needs to run at all, resolve a toolchain, and reach the
#: network through a corporate proxy. ``HOME`` is here on purpose (see above).
_PASSTHROUGH = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    # The CLIs' own configuration-directory variables. Inherited when the *user*
    # has set one, because that is their deliberate choice and Neo running the
    # CLI should not behave differently from them running it in their terminal.
    # Neo never sets these itself unless explicitly configured -- see below.
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    # Windows needs these for a process to start at all.
    "SYSTEMROOT",
    "SystemRoot",
    "windir",
    "SystemDrive",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
)

#: Extra variables a *sign-in* may see, and an agent run may not. A login opens
#: the user's browser and talks to a terminal; a coding run does neither, so
#: these stay out of the run environment, where they would only be surface.
_INTERACTIVE_EXTRA = (
    "BROWSER",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_SESSION_TYPE",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)

#: Prefixes that must never reach the child, checked even though the allowlist
#: above would already exclude them. Belt and braces: the allowlist is the
#: mechanism, and this is the assertion that survives someone extending it
#: carelessly. ``_forbidden`` is also exported so a test can state the rule.
_FORBIDDEN_PREFIXES = ("NEO_",)
_FORBIDDEN_EXACT = frozenset(
    {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_API_KEY",
        "DATA_BRAVE_API_KEY",
        "SERPER_API_KEY",
        "WEB_SEARCH_API_KEY",
    }
)


def is_forbidden(name: str) -> bool:
    """Whether a variable must never be passed to an external agent."""

    upper = name.upper()
    return upper in _FORBIDDEN_EXACT or any(
        upper.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES
    )


def expand_home(raw: str) -> str:
    """Resolve a configured credential directory to an absolute path."""

    return str(Path(os.path.expanduser(raw or "")).expanduser())


def build_env(
    *,
    home_env: str | None = None,
    home_dir: str | None = None,
    interactive: bool = False,
) -> dict[str, str]:
    """The child's environment.

    ``home_env``/``home_dir`` name the CLI's own configuration directory --
    ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME`` -- and are applied **only when a
    deployment has explicitly configured one**.

    That conditional is load-bearing, and was found the hard way. Setting
    ``CLAUDE_CONFIG_DIR`` at all switches Claude Code from Keychain-backed
    credentials to file-based ones in the named directory. Passing even its own
    documented default, ``~/.claude``, therefore turns a signed-in user into a
    signed-out one:

        $ claude auth status --json
        {"loggedIn": true,  "authMethod": "claude.ai"}
        $ CLAUDE_CONFIG_DIR=~/.claude claude auth status --json
        {"loggedIn": false, "authMethod": "none"}

    So the rule is: Neo never *forces* these variables. It inherits one the user
    set themselves (they are in the passthrough list above), and overrides only
    on an explicit instruction. Authentication belongs to the CLI, and that
    includes deciding where to look for it.

    ``interactive`` is for the one job that is not a coding run: signing the CLI
    in. A login is a terminal conversation that ends in a browser, so it needs
    the opposite of the run environment -- a terminal type that is not ``dumb``,
    and no ``CI=true``, which is precisely the flag a CLI reads to decide it is
    unattended and must not prompt. Setting it for a login would turn every
    in-app sign-in into an immediate, unexplained failure.
    """

    env: dict[str, str] = {}
    passthrough = _PASSTHROUGH + (_INTERACTIVE_EXTRA if interactive else ())
    for name in passthrough:
        value = os.environ.get(name)
        if value is not None and not is_forbidden(name):
            env[name] = value

    if interactive:
        # A real terminal type: both CLIs draw a prompt and expect to be able to
        # move the cursor. Colour stays off, because Neo reads this output back
        # to the user as text rather than rendering it as a terminal would.
        env["TERM"] = "xterm-256color"
        env["NO_COLOR"] = "1"
    else:
        # Deterministic, machine-readable output. TERM=dumb keeps a CLI that
        # probes the terminal from emitting cursor control sequences into the
        # JSONL we are about to parse.
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["CI"] = "true"

    if home_env and home_dir and home_dir.strip():
        resolved = expand_home(home_dir)
        # `expand_home("")` resolves to ".", which would be a silent and very
        # wrong credential directory, so an empty configuration must never
        # reach this branch.
        if resolved and resolved != ".":
            env[home_env] = resolved
    return env


__all__ = ["build_env", "expand_home", "is_forbidden"]
