"""Installing Neo's built-in SearXNG, on any platform, without touching Neo's own packages.

SearXNG has no PyPI package and cuts no releases, so "installing" it is two things: the source
tree of one pinned commit, and the packages that tree imports. The Docker image bakes both in.
Everywhere else Neo installs them itself -- on first start, in the background, or ahead of time
with ``python scripts/setup_searxng.py`` -- so a plain ``pip install -r requirements.txt``
install of Neo has SearXNG without anyone remembering a second step.

SearXNG's packages go into ``<source>/.deps`` with ``pip install --target``, not into Neo's
environment. That keeps Neo's tested versions exactly as they are (SearXNG pins some packages
Neo shares to other versions), and it is what makes installing while Neo is running safe: on
Windows a package whose DLL a running process has loaded cannot be replaced. The directory is
appended to ``sys.path`` after Neo's own, so where both have a package, Neo's version is used --
the same rule the Docker image follows by reinstalling Neo's pins last.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

#: The SearXNG commit Neo is tested with. The Dockerfile's SEARXNG_COMMIT must match; a CI
#: check holds the two together.
SEARXNG_COMMIT = "54613defc7d4cbbc1d8ec3ba269b90717eab0958"
REPOSITORY = "https://github.com/searxng/searxng"
#: Build- and development-time only; dropping them takes the tree from ~90 MB to ~19 MB.
DEV_ONLY = frozenset({"docs", "tests", "client", "searxng_extra", "utils", ".git", ".github"})
#: Written into the source tree once it is complete, naming the commit it holds.
MARKER = ".neo-searxng"
#: What SearXNG imports at startup. Present means its packages are installed, wherever.
REQUIRED_MODULES = (
    "flask", "flask_babel", "babel", "lxml", "httpx", "httpx_socks", "valkey", "msgspec",
    "isodate", "whitenoise", "markdown_it", "pygments", "typer",
)
VERSION_FROZEN = '''\
# Written at install time -- searx/version.py prefers this over shelling
# out to git, which is absent from the fetched tree.
VERSION_STRING = "neo"
VERSION_TAG = "neo"
DOCKER_TAG = "neo"
GIT_URL = "https://github.com/searxng/searxng"
GIT_BRANCH = "master"
'''

_lock = threading.Lock()
#: "idle", "installing", "installed" or "failed", for status reporting.
state = {"status": "idle", "error": ""}


def source_dir() -> Path:
    from app.services.search.searxng_embedded import _source_dir

    return _source_dir()


def deps_dir() -> Path:
    return source_dir() / ".deps"


def add_deps_to_path() -> None:
    """Make SearXNG's own packages importable, after Neo's so Neo's versions win."""
    deps = str(deps_dir())
    if Path(deps).is_dir() and deps not in sys.path:
        sys.path.append(deps)


def source_present() -> bool:
    return (source_dir() / "searx" / "webapp.py").is_file()


def packages_present() -> bool:
    add_deps_to_path()
    return all(importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES)


def needs_install() -> bool:
    return not (source_present() and packages_present())


def _skip(relative: str) -> bool:
    parts = relative.split("/")
    if parts[0] in DEV_ONLY:
        return True
    # SearXNG's tree has file names Windows cannot hold (``...searxng.conf:socket``); they are
    # all under utils/, which is dropped anyway, but a name like that anywhere is skipped.
    return any(":" in part or part.endswith((" ", ".")) for part in parts)


def _remove(path: Path) -> None:
    def writable(function, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if path.exists():
        shutil.rmtree(path, onexc=writable)


def fetch_source(commit: str = SEARXNG_COMMIT, *, log: Callable[[str], None] = logger.info) -> None:
    """Download the pinned commit's tree into the source directory, replacing it whole."""
    target = source_dir()
    staging = target.with_name(target.name + ".download")
    _remove(staging)
    url = f"{REPOSITORY}/archive/{commit}.zip"
    log(f"Downloading SearXNG {commit[:12]} from {url}")
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310 - fixed https URL
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    names = archive.namelist()
    prefix = names[0].split("/", 1)[0] + "/" if names else ""
    staging.mkdir(parents=True)
    for member in archive.infolist():
        relative = member.filename[len(prefix):]
        if not relative or member.is_dir() or _skip(relative):
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(member))
    if not (staging / "searx" / "webapp.py").is_file():
        _remove(staging)
        raise RuntimeError("The SearXNG download did not contain searx/webapp.py.")
    (staging / "searx" / "version_frozen.py").write_text(VERSION_FROZEN, encoding="utf-8")
    (staging / MARKER).write_text(commit, encoding="utf-8")
    # Swapped in only once complete, so an interrupted download never leaves a half tree that
    # would make the built-in provider look installed. Packages already installed are kept.
    deps = target / ".deps"
    if deps.is_dir():
        deps.rename(staging / ".deps")
    _remove(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(target)


def install_packages(*, log: Callable[[str], None] = logger.info) -> None:
    """Install what SearXNG imports into its own directory, never into Neo's environment."""
    target = source_dir()
    log(f"Installing SearXNG's packages into {target / '.deps'}")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-warn-script-location", "--upgrade", "--target", str(target / ".deps"),
            "-r", str(target / "requirements.txt"),
        ],
        check=True,
    )
    importlib.invalidate_caches()


def install(*, force: bool = False, log: Callable[[str], None] = logger.info) -> None:
    """Install whatever part of SearXNG is missing (or all of it, with ``force``)."""
    with _lock:
        state.update(status="installing", error="")
        try:
            if force or not source_present():
                fetch_source(log=log)
            if force or not packages_present():
                install_packages(log=log)
        except Exception as exc:
            state.update(status="failed", error=str(exc)[:300])
            raise
        state.update(status="installed", error="")


def install_in_background() -> threading.Thread:
    """Install on a daemon thread, then boot it. Search falls back to other providers meanwhile."""

    def work() -> None:
        from app.services.search import searxng_embedded

        try:
            install()
        except Exception:
            logger.warning("Built-in SearXNG could not be installed; other providers will serve "
                           "searches. Run scripts/setup_searxng.py to retry.", exc_info=True)
            return
        # A search made during the install found SearXNG missing and remembered that; forget
        # it so the next search boots the tree that is now there.
        searxng_embedded.reset_for_tests()
        searxng_embedded.warm_up()

    thread = threading.Thread(target=work, name="searxng-install", daemon=True)
    thread.start()
    return thread
