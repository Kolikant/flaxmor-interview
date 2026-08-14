"""The OpenAI-compatible surface Open WebUI drives.

Only two endpoints are needed for chat to work: the model list it populates its
selector from, and the completion it sends every turn to.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from extractor_proxy.errors import error_envelope
from extractor_proxy.prompt import inject_system_prompt
from extractor_proxy.upstream import JSON_MEDIA_TYPE, UpstreamClient, UpstreamError

logger = logging.getLogger("extractor_proxy.openai")

router = APIRouter(prefix="/v1", tags=["openai"])

#: Headers set on a streamed response, as an allowlist rather than a passthrough.
#: Open WebUI copies the middleware's response headers verbatim onto the response it
#: sends the browser, so forwarding an upstream `content-encoding` or `content-length`
#: would describe a body that no longer matches and the message would never render.
SSE_HEADERS = {"cache-control": "no-store"}


def _error(status_code: int, message: str, error_type: str) -> JSONResponse:
    """An error this proxy originated, as opposed to one relayed from upstream."""
    return JSONResponse(error_envelope(message, error_type), status_code=status_code)


@router.get("/models", summary="Models advertised to Open WebUI")
async def list_models(request: Request) -> dict[str, Any]:
    """Return the configured models without calling OpenAI.

    Serving this from configuration keeps the selector populated when the upstream is
    unreachable, and keeps the endpoint deterministic under test. The tradeoff is that
    the list does not reflect the account's real entitlements, so a model the key
    cannot reach fails at the first chat rather than at selection.

    The payload is built once at startup, since it derives entirely from settings that
    cannot change at runtime.
    """
    return request.app.state.models_payload


@router.post("/chat/completions", summary="Chat completion with the extraction prompt injected")
async def chat_completions(request: Request) -> Response:
    """Inject the extraction prompt, then relay OpenAI's answer.

    Malformed bodies are rejected here rather than forwarded: an OpenAI-compatible
    proxy that answers a bad body with a 500 and a stack trace is worse than one that
    answers with the error shape its clients already parse.
    """
    state = request.app.state

    try:
        payload = await request.json()
    except ValueError:
        return _error(400, "Request body is not valid JSON.", "invalid_request_error")

    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    if state.system_prompt is None:
        # Relaying without the prompt would quietly turn the product into a plain
        # GPT proxy, which is a worse failure than refusing.
        logger.error("chat.refused", extra={"reason": "prompt_unavailable"})
        return _error(
            503,
            f"The extraction prompt is unavailable: {state.system_prompt_error}",
            "prompt_unavailable",
        )

    injected = inject_system_prompt(payload, state.system_prompt)
    client: UpstreamClient = state.upstream

    if injected.get("stream"):
        return await _streaming_response(client, injected)

    try:
        result = await client.chat_completion(injected)
    except UpstreamError as exc:
        return JSONResponse(exc.payload, status_code=exc.status_code)

    # Relayed verbatim rather than re-serialised, so key order and number formatting
    # survive the hop exactly as OpenAI sent them.
    return Response(
        content=result.content, status_code=result.status_code, media_type=JSON_MEDIA_TYPE
    )


async def _streaming_response(client: UpstreamClient, payload: dict[str, Any]) -> Response:
    """Start the upstream stream, then decide the status before committing headers.

    The first chunk is pulled *before* the StreamingResponse is constructed. That is
    the whole trick: once a 200 and the SSE content type have been sent, the status
    line cannot be revised, so an upstream 401 or a dead connection would be
    invisible to the client. Pulling one chunk first keeps a real status code
    available for every failure that happens before the body starts, and costs
    nothing in latency because the chunk is forwarded as soon as it arrives.
    """
    stream = client.stream_chat_completion(payload)

    try:
        first_chunk: bytes | None = await anext(stream)
    except StopAsyncIteration:
        first_chunk = None
    except UpstreamError as exc:
        await stream.aclose()
        return JSONResponse(exc.payload, status_code=exc.status_code)

    async def body(primed: bytes | None) -> AsyncIterator[bytes]:
        # Taken as a parameter so it can be released after being yielded; a captured
        # closure variable would hold the buffer for the whole life of the stream.
        if primed is not None:
            yield primed
            primed = None
        async for chunk in stream:
            yield chunk

    return StreamingResponse(
        body(first_chunk), media_type="text/event-stream", headers=SSE_HEADERS
    )
