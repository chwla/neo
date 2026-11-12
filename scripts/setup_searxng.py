"""Install Neo's built-in SearXNG ahead of time.

Neo installs it by itself, in the background, the first time it starts without it (see
``app/services/search/searxng_install.py``). Run this instead to do it up front -- before going
offline, on a machine with NEO_SEARXNG_AUTO_INSTALL=false, or to repair an install:

    python scripts/setup_searxng.py            # install whatever part is missing
    python scripts/setup_searxng.py --force    # download and reinstall all of it

SearXNG's packages go into its own folder (``data/searxng/.deps`` by default), never into Neo's
environment, so Neo's tested versions are left exactly as they are.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.search import searxng_embedded, searxng_install  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Neo's built-in SearXNG.")
    parser.add_argument("--force", action="store_true", help="download and reinstall all of it")
    args = parser.parse_args()

    if searxng_install.needs_install() or args.force:
        searxng_install.install(force=args.force, log=print)
    else:
        print(f"SearXNG is already installed at {searxng_install.source_dir()}.")

    searxng_embedded.reset_for_tests()
    if searxng_embedded._boot() is None:
        print(f"SearXNG did not start: {searxng_embedded._BOOT_ERROR}")
        return 1
    print("SearXNG is installed and starts inside Neo. Restart Neo if it is running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
