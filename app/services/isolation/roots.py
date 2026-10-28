"""What is private, what is scoped to one run, and what may merely be executed.

One function answers "which paths must a sandboxed process not reach", and
everything else -- the policy generator, the overlap guard, the attestation probe and
the contract cross-check -- reads it. A second list would eventually disagree with the
first, and the way it would disagree is by leaving something out.

The classes come from docs/security/data-isolation.md §6:

``PRIVATE``
    Never readable or writable by a run. No carve-outs. Everything under the data
    root that is not named below is private *by default*, so a feature that adds a
    new directory is protected before anyone remembers to add it here.
``SCOPED``
    One entry per profile/run. A run gets its own and nothing else, so two profiles
    -- and two threads of one profile -- cannot read each other.
``EXEC``
    Verified engine packages: read, map and execute, never write. Separate from
    PRIVATE because a run has to be able to execute its own CLI, and separate from
    PUBLIC because nothing may modify it.
``MARKER``
    Empty files used to identify which processes belong to a run. They hold no data.
``PUBLIC``
    No user data: model weights, the search engine's own source tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_base_settings
from app.core.paths import install_root

#: Children of the data root that are *not* private, with their class.
SCOPED_DIRS = ("workspaces", "agent-state")
EXEC_DIRS = ("engines",)
MARKER_DIRS = ("run-markers",)
PUBLIC_DIRS = ("voice-models", "searxng")

#: Files and directories outside the data root that a run must never read. These are
#: the user's own shared tool state: credentials, transcripts of other sessions, and
#: the configuration files those tools *execute* from.
HOME_PRIVATE = (
    ".claude",
    ".claude.json",
    ".claude.json.lock",
    ".codex",
    ".cursor",
    ".gemini",
    ".config/gcloud",
    ".aws",
    ".ssh",
    ".netrc",
    ".npmrc",
    ".gitconfig",
    ".zsh_history",
    ".bash_history",
    "Library/Keychains",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Safari",
    "Library/Cookies",
    ".mozilla",
    ".config/google-chrome",
    ".config/BraveSoftware",
)


@dataclass(frozen=True)
class Roots:
    data_root: Path
    private: tuple[Path, ...]
    scoped: tuple[Path, ...]
    exec_root: Path
    marker_root: Path
    public: tuple[Path, ...]

    def protected(self) -> tuple[Path, ...]:
        """Everything a run must not read unless something re-allows it."""

        return (*self.private, *self.scoped)


def data_root() -> Path:
    """Neo's data directory, resolved the same way every store resolves it."""

    settings = get_base_settings()
    if settings.data_dir:
        return Path(settings.data_dir).expanduser().resolve()
    url = settings.database_url
    if url.startswith("sqlite:///"):
        candidate = Path(url.removeprefix("sqlite:///")).expanduser()
        if not candidate.is_absolute():
            # Anchored to the install, never to the working directory: the cwd is
            # how a second set of databases came to exist in `app/` and `frontend/`.
            candidate = install_root() / candidate
        return candidate.resolve().parent
    return (install_root() / "data").resolve()


def _home() -> Path:
    return Path.home()


def roots() -> Roots:
    base = data_root()
    reserved = {*SCOPED_DIRS, *EXEC_DIRS, *MARKER_DIRS, *PUBLIC_DIRS}

    private: list[Path] = []
    # Deny-by-default: every child of the data root that is not explicitly another
    # class is private. A directory that does not exist yet is still listed, so it
    # is protected from the moment something creates it mid-run.
    if base.exists():
        for child in sorted(base.iterdir()):
            if child.name not in reserved:
                private.append(child)
    # Named explicitly as well, so they are protected before anything creates them:
    # the walk above can only see what already exists.
    for name in ("profiles", "isolation", "tmp", "backend.log", ".env"):
        candidate = base / name
        if candidate not in private:
            private.append(candidate)

    home = _home()
    for name in HOME_PRIVATE:
        private.append(home / name)

    # The install directory holds .env, the stray databases earlier builds left in
    # the launch folder, and Neo's own source. None of it is a run's business.
    install = install_root()
    for name in (".env", "profiles", "data"):
        private.append(install / name)
    # Older launches resolved storage relative to their cwd. These copies hold
    # real accounts and memories even though the current server no longer reads
    # them, so reporting them as "stray" is not enough: agents must not read them.
    for relative in ("app/profiles", "frontend/profiles"):
        private.append(install / relative)
    for relative in ("neo_memory.db", "app/neo_memory.db", "frontend/neo_memory.db"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            private.append(install / f"{relative}{suffix}")

    scoped = tuple((base / name) for name in SCOPED_DIRS)
    public = tuple((base / name) for name in PUBLIC_DIRS)
    return Roots(
        data_root=base,
        private=tuple(dict.fromkeys(private)),
        scoped=scoped,
        exec_root=base / EXEC_DIRS[0],
        marker_root=base / MARKER_DIRS[0],
        public=public,
    )


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def overlaps_protected(
    candidate: Path, known: Roots | None = None, *, profile_id: str | None = None
) -> str | None:
    """Why ``candidate`` may not be used as a workspace, or ``None`` if it may.

    Refuses a path that *is*, is *inside*, or *contains* a protected root. The last
    case matters most: attaching a parent directory would otherwise hand a run
    everything below it, which is how the Neo install folder -- and a home directory
    -- became reachable.
    """

    known = known or roots()
    try:
        resolved = candidate.expanduser().resolve()
    except OSError as exc:
        return f"the workspace path could not be resolved: {exc}"

    if resolved == known.data_root:
        return "that folder is Neo's own data directory"
    if _is_within(known.data_root, resolved):
        # Inside the data root, only a SCOPED entry is allowed, and only below the
        # class directory itself (``workspaces/<profile>/<repo>``, never
        # ``workspaces`` or ``workspaces/<profile>``).
        for scoped in known.scoped:
            if _is_within(scoped, resolved):
                parts = resolved.relative_to(scoped).parts
                if scoped.name != "workspaces":
                    return "that folder is agent state, not a code workspace"
                if len(parts) < 2:
                    return "select one repository inside your own workspace area"
                if profile_id is not None and parts[0] != profile_id:
                    return "that workspace belongs to another profile"
                return None
        return "that folder is inside Neo's private storage"

    for protected in known.protected():
        if resolved == protected:
            return f"that folder is {protected.name}, which holds Neo's own data"
        if _is_within(protected, resolved):
            return f"that folder is inside {protected.name}, which holds Neo's own data"
        if _is_within(resolved, protected) and protected.exists():
            return (
                f"that folder contains {protected}, so opening it would hand an agent "
                "Neo's own data"
            )
    return None


def thread_state_refusal(
    candidate: Path, *, profile_id: str, known: Roots | None = None
) -> str | None:
    """Require an external engine's writable HOME to be its own scoped entry."""

    known = known or roots()
    if not profile_id or Path(profile_id).name != profile_id:
        return "the run is not bound to a valid profile"
    try:
        resolved = candidate.expanduser().resolve()
        parts = resolved.relative_to(known.data_root / "agent-state").parts
    except (OSError, ValueError):
        return "CLI state is outside Neo's scoped agent-state directory"
    if len(parts) < 3 or parts[0] != profile_id:
        return "CLI state belongs to another profile or is not one thread"
    return None


__all__ = [
    "EXEC_DIRS",
    "HOME_PRIVATE",
    "MARKER_DIRS",
    "PUBLIC_DIRS",
    "SCOPED_DIRS",
    "Roots",
    "data_root",
    "overlaps_protected",
    "roots",
    "thread_state_refusal",
]
