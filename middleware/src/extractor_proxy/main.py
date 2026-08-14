"""Application factory and ASGI entrypoint."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from extractor_proxy import __version__
from extractor_proxy.config import Settings, get_settings
from extractor_proxy.errors import error_envelope
from extractor_proxy.observability import REQUEST_ID_HEADER, RequestLifecycleMiddleware
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

    Only a client created here is closed here. An injected one belongs to whoever
    built it, who may well outlive this app or share the client with another.
    """
    logger.info("service.starting", extra=app.state.settings.redacted_summary())

    owns_client = app.state.upstream is None
    if owns_client:
        app.state.upstream = UpstreamClient(app.state.settings)
    try:
        yield
    finally:
        if owns_client:
            await app.state.upstream.aclose()
            app.state.upstream = None
        logger.info("service.stopped")


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
    app.state.models_payload = _models_payload(settings)
    app.include_router(health.router)
    app.include_router(openai_compat.router)
    app.add_middleware(RequestLifecycleMiddleware)
    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Answer framework-level errors in the same shape as everything else.

    Without these, a wrong path gives Starlette's `{"detail": "Not Found"}` and an
    unhandled exception gives bare text, so a client that learned to read
    `error.message` from every other failure suddenly cannot. Open WebUI in particular
    pulls its toast text straight out of `error.message`.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            error_envelope(str(exc.detail), "invalid_request_error", code=str(exc.status_code)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # The lifecycle middleware has already logged this with the request id; the
        # exception type is deliberately not echoed to the client.
        #
        # Starlette routes this handler to ServerErrorMiddleware, outside the lifecycle
        # middleware, so the contextvar is already reset and the response bypasses the
        # header-stamping wrapper. The id is read back off the scope and applied by
        # hand — an unhandled 500 is precisely when a correlation id is most wanted, so
        # it must not be the one response that lacks it.
        request_id = (request.scope.get("state") or {}).get("request_id")
        envelope = error_envelope("The proxy failed to handle this request.", "internal_error")
        if request_id:
            envelope["error"]["request_id"] = request_id
        return JSONResponse(
            envelope,
            status_code=500,
            headers={REQUEST_ID_HEADER: request_id} if request_id else None,
        )


def _models_payload(settings: Settings) -> dict[str, Any]:
    """Build the /v1/models response once, since it derives only from settings.

    `id` is load-bearing: Open WebUI subscripts it directly while merging model lists,
    so a missing one discards the whole list rather than the single entry. A fixed
    `created` is also more honest than a moving one for a list that never changes.
    """
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": created, "owned_by": "openai"}
            for model_id in settings.exposed_model_ids
        ],
    }


def _read_prompt(settings: Settings) -> tuple[str | None, str | None]:
    """Load the prompt once at startup, returning the failure instead of raising.

    Read in the factory rather than the lifespan on purpose: readiness has to be able
    to report the result whether or not the lifespan has run.

    A missing prompt document is a packaging error, but crashing here would leave the
    container in a restart loop with no endpoint able to say why. Starting up unready
    is more diagnosable: /healthz answers, /readyz names the problem, and the chat
    route refuses with a 503 rather than silently proxying without a prompt.
    """
    try:
        prompt = load_system_prompt(settings.system_prompt_path)
    except PromptUnavailableError as exc:
        logger.error("prompt.load.failed", extra={"prompt_path": str(settings.system_prompt_path)})
        return None, str(exc)

    logger.info(
        "prompt.load.ok",
        extra={"prompt_path": str(settings.system_prompt_path), "prompt_chars": len(prompt)},
    )
    return prompt, None


app = create_app()
