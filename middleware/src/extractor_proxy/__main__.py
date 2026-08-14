"""Process entrypoint: install JSON logging, then serve the app.

Logging is configured here rather than in `create_app()` so that importing the
application — in tests, or for a schema dump — never rewrites global handlers.
"""

from __future__ import annotations

import uvicorn

from extractor_proxy.config import get_settings
from extractor_proxy.observability import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, service_name=settings.service_name)

    uvicorn.run(
        "extractor_proxy.main:app",
        host=settings.host,
        port=settings.port,
        # Uvicorn would otherwise install its own dictConfig and undo the JSON
        # handler set above.
        log_config=None,
    )


if __name__ == "__main__":
    main()
