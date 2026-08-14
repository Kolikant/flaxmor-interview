"""Application factory and ASGI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from extractor_proxy import __version__
from extractor_proxy.config import Settings, get_settings
from extractor_proxy.observability import RequestLifecycleMiddleware
from extractor_proxy.prompt import PromptUnavailableError, load_system_prompt
from extractor_proxy.routes import health, openai_compat
from extractor_proxy.upstream import UpstreamClient

logger = logging.getLogger("extractor_proxy")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one upstream client for the app's lifetime.

    A client per request would mean a TLS handshake per request, and each user turn
    produces several concurrent upstream calls — the completion plus Open WebUI's
    title and tag generation — so the connection pool earns its keep immediately.
    """
    if app.state.upstream is None:
        app.state.upstream = UpstreamClient(app.state.settings)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


def create_app(settings: Settings | None = None, upstream: UpstreamClient | None = None) -> FastAPI:
    """Build the ASGI application.

    Settings and the upstream client are injectable so tests can build an app against
    a fake OpenAI without reaching for environment variables or the network.
    """
    settings = settings or get_settings()

    app = FastAPI(
        lifespan=_lifespan,
        title=settings.service_name,
        version=__version__,
        description=(
            "Sits between Open WebUI and OpenAI, injecting a structured-extraction "
            "system prompt into every chat completion request."
        ),
    )
    app.state.settings = settings
    app.state.upstream = upstream
    app.state.system_prompt, app.state.system_prompt_error = _read_prompt(settings)
    app.include_router(health.router)
    app.include_router(openai_compat.router)
    app.add_middleware(RequestLifecycleMiddleware)
    return app


def _read_prompt(settings: Settings) -> tuple[str | None, str | None]:
    """Load the prompt once at startup, returning the failure instead of raising.

    A missing prompt document is a packaging error, but crashing here would leave the
    container in a restart loop with no endpoint able to say why. Starting up unready
    is more diagnosable: /healthz answers, /readyz names the problem, and the chat
    route refuses with a 503 rather than silently proxying without a prompt.
    """
    try:
        prompt = load_system_prompt(settings.system_prompt_path)
    except PromptUnavailableError as exc:
        logger.error("prompt.load.failed", extra={"path": str(settings.system_prompt_path)})
        return None, str(exc)

    logger.info(
        "prompt.load.ok",
        extra={"path": str(settings.system_prompt_path), "prompt_chars": len(prompt)},
    )
    return prompt, None


app = create_app()
