"""Application factory and ASGI entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from extractor_proxy import __version__
from extractor_proxy.config import Settings, get_settings
from extractor_proxy.observability import RequestLifecycleMiddleware
from extractor_proxy.prompt import PromptUnavailableError, load_system_prompt

logger = logging.getLogger("extractor_proxy")


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
    app.state.system_prompt, app.state.system_prompt_error = _read_prompt(settings)
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
