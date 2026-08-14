"""The OpenAI-compatible surface Open WebUI drives.

Only two endpoints are needed for chat to work: the model list it populates its
selector from, and the completion it sends every turn to.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from extractor_proxy.errors import error_envelope
from extractor_proxy.prompt import inject_system_prompt
from extractor_proxy.upstream import (
    JSON_MEDIA_TYPE,
    UpstreamClient,
    UpstreamError,
    UpstreamResponse,
)

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

    declared_length = request.headers.get("content-length")
    limit = state.settings.max_request_bytes
    if declared_length and declared_length.isdigit() and int(declared_length) > limit:
        logger.warning(
            "chat.rejected",
            extra={"reason": "body_too_large", "content_length": int(declared_length)},
        )
        return _error(
            413,
            f"Request body is larger than the {limit} byte limit.",
            "invalid_request_error",
        )

    body = await request.body()
    if len(body) > limit:
        # A chunked request declares no length, so the read is checked too.
        logger.warning("chat.rejected", extra={"reason": "body_too_large", "body_bytes": len(body)})
        return _error(
            413,
            f"Request body is larger than the {limit} byte limit.",
            "invalid_request_error",
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
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
    streaming = bool(injected.get("stream"))

    # One line describing the request as the proxy understood it. Without this, a log
    # stream shows a POST and an upstream status but not which model was asked, whether
    # the prompt was injected, or how much history came with it — the three things that
    # explain most surprising answers.
    logger.info(
        "chat.request",
        extra={
            "model": payload.get("model"),
            "streaming": streaming,
            "request_bytes": len(body),
            "message_count": len(payload.get("messages") or []),
            "prompt_injected": injected is not payload,
        },
    )

    client: UpstreamClient = state.upstream

    if streaming:
        return await _streaming_response(client, injected)

    try:
        result = await client.chat_completion(injected)
    except UpstreamError as exc:
        return JSONResponse(exc.payload, status_code=exc.status_code)

    _log_completion_usage(result)

    # Relayed verbatim rather than re-serialised, so key order and number formatting
    # survive the hop exactly as OpenAI sent them.
    return Response(
        content=result.content, status_code=result.status_code, media_type=JSON_MEDIA_TYPE
    )


def _log_completion_usage(result: UpstreamResponse) -> None:
    """Record token usage and finish reason from a non-streaming completion.

    Read-only: the bytes still go back verbatim. Truncation is the failure that looks
    like a model quirk rather than a bug — `finish_reason: "length"` produces a
    half-written envelope that then poisons every later turn in the conversation — so
    it is worth a log line even though the proxy does not otherwise read the body.
    """
    if result.status_code >= 400:
        return
    try:
        parsed = json.loads(result.content)
        usage = parsed.get("usage") or {}
        finish_reason = (parsed.get("choices") or [{}])[0].get("finish_reason")
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        return

    logger.info(
        "chat.completed",
        extra={
            "finish_reason": finish_reason,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "truncated": finish_reason == "length",
        },
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

    started = time.perf_counter()

    async def body(primed: bytes | None) -> AsyncIterator[bytes]:
        # Taken as a parameter so it can be released after being yielded; a captured
        # closure variable would hold the buffer for the whole life of the stream.
        chunks = 0
        forwarded = 0
        try:
            if primed is not None:
                chunks, forwarded = 1, len(primed)
                yield primed
                primed = None
            async for chunk in stream:
                chunks += 1
                forwarded += len(chunk)
                yield chunk
        finally:
            # In a `finally` so the summary survives a client disconnect, which is the
            # case where knowing how far the stream got actually matters.
            logger.info(
                "chat.stream.finished",
                extra={
                    "chunks": chunks,
                    "forwarded_bytes": forwarded,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    return StreamingResponse(
        body(first_chunk), media_type="text/event-stream", headers=SSE_HEADERS
    )
