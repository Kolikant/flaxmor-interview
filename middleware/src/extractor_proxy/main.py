"""Application factory and ASGI entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from extractor_proxy import __version__
from extractor_proxy.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Settings are injectable so tests can build an app against a fake upstream
    without reaching for environment variables or the module-level cache.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.service_name,
        version=__version__,
        description=(
            "Sits between Open WebUI and OpenAI, injecting a structured-extraction "
            "system prompt into every chat completion request."
        ),
    )
    app.state.settings = settings
    return app


app = create_app()
