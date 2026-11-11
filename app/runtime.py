from __future__ import annotations

import uvicorn

from app.core.config import get_settings

GRACEFUL_SHUTDOWN_SECONDS = 5


def main() -> None:
    settings = get_settings()
    # Bounded, so a client that never closes its connection cannot hold the
    # process open after it has been asked to stop.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
