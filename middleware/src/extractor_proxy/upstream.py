"""The OpenAI hop.

Everything that talks to OpenAI lives here, so the routes stay concerned with HTTP
shape and this module owns the one thing that is genuinely hard: reporting a failure
that happens after the response status has already been sent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx2 as httpx

from extractor_proxy.config import Settings

logger = logging.getLogger("extractor_proxy.upstream")

#: Terminator every OpenAI-compatible SSE stream ends with. Open WebUI waits for it.
SSE_DONE = b"data: [DONE]\n\n"

_MAX_ERROR_BODY_CHARS = 500


def error_envelope(message: str, error_type: str, code: str | None = None) -> dict[str, Any]:
    """Build an OpenAI-shaped error body.

    Clients of an OpenAI-compatible API already know how to read this shape, so
    failures originating in the proxy are dressed the same way as upstream ones.
    """
    return {"error": {"message": message, "type": error_type, "code": code}}


class UpstreamError(Exception):
    """A failure the route can still turn into an HTTP status.

    Raised only while the response status is still open — either before the request
    was sent, or before the first streamed byte. Once bytes are on the wire the
    stream reports its own failure instead; see `stream_chat_completion`.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(payload["error"]["message"])
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class UpstreamResponse:
    """An upstream HTTP response, successful or not, ready to relay verbatim."""

    status_code: int
    payload: dict[str, Any]


def _transport_failure(exc: httpx.RequestError) -> UpstreamError:
    """Map an httpx transport failure onto a gateway status."""
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(
            504,
            error_envelope(
                f"Upstream request to OpenAI timed out: {exc}",
                "upstream_timeout",
            ),
        )
    return UpstreamError(
        502,
        error_envelope(
            f"Could not reach OpenAI: {exc}",
            "upstream_unavailable",
        ),
    )


def _decode_body(response: httpx.Response) -> dict[str, Any]:
    """Parse an upstream body, synthesising an envelope when it is not JSON.

    A gateway in front of OpenAI can answer with HTML, and a client expecting the
    OpenAI error shape should not have to cope with that.
    """
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        return payload

    return error_envelope(
        f"Upstream returned a non-JSON body: {response.text[:_MAX_ERROR_BODY_CHARS]}",
        "upstream_error",
        code=str(response.status_code),
    )


class UpstreamClient:
    """Async client for OpenAI's chat completions, in both response modes."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self.http_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            )
        )

    async def aclose(self) -> None:
        await self.http_client.aclose()

    @property
    def _chat_url(self) -> str:
        return f"{self._settings.openai_base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        """Authenticate with the configured key only.

        Whatever the caller sent is ignored: Open WebUI is configured with a dummy
        key, so forwarding it would hand OpenAI a bogus credential, and trusting it
        would make the real key client-supplied.
        """
        return {
            "authorization": f"Bearer {self._settings.openai_api_key}",
            "content-type": "application/json",
        }

    async def chat_completion(self, payload: dict[str, Any]) -> UpstreamResponse:
        """Send a non-streaming completion and relay whatever came back.

        Non-2xx responses are returned rather than raised: an upstream 429 with
        OpenAI's own error body is more useful to the client than anything this proxy
        would rewrite it into. Only transport failures raise.
        """
        try:
            response = await self.http_client.post(
                self._chat_url, json=payload, headers=self._headers()
            )
        except httpx.RequestError as exc:
            error = _transport_failure(exc)
            logger.warning(
                "upstream.request.failed",
                extra={"status": error.status_code, "reason": error.payload["error"]["type"]},
            )
            raise error from exc

        logger.info(
            "upstream.response",
            extra={"status": response.status_code, "streamed": False},
        )
        return UpstreamResponse(status_code=response.status_code, payload=_decode_body(response))

    async def stream_chat_completion(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """Relay an SSE stream from OpenAI, chunk for chunk.

        Chunks are forwarded as opaque bytes rather than decoded and re-encoded. The
        proxy has no need to read them, and a re-encoding bug would corrupt every
        response.

        Failure handling splits on whether anything has been sent yet. Before the
        first chunk, raise `UpstreamError` so the route can still answer with a
        status. After it, the status is already committed, so emit one error event
        and the `[DONE]` terminator — Open WebUI then shows a message instead of a
        silently truncated one.
        """
        sent_any = False
        try:
            async with self.http_client.stream(
                "POST", self._chat_url, json=payload, headers=self._headers()
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    logger.warning(
                        "upstream.response",
                        extra={"status": response.status_code, "streamed": True},
                    )
                    raise UpstreamError(response.status_code, _decode_body(response))

                logger.info(
                    "upstream.response",
                    extra={"status": response.status_code, "streamed": True},
                )
                try:
                    async for chunk in response.aiter_bytes():
                        sent_any = True
                        yield chunk
                except GeneratorExit:
                    # The consumer stopped early — Open WebUI's stop button, or a
                    # closed browser tab. Exiting the context manager releases the
                    # upstream response instead of leaving the call in flight.
                    logger.info(
                        "upstream.stream.abandoned",
                        extra={"reason": "consumer_disconnected"},
                    )
                    raise
        except httpx.RequestError as exc:
            if not sent_any:
                error = _transport_failure(exc)
                logger.warning(
                    "upstream.request.failed",
                    extra={"status": error.status_code, "reason": error.payload["error"]["type"]},
                )
                raise error from exc

            logger.warning("upstream.stream.interrupted", extra={"reason": type(exc).__name__})
            interrupted = error_envelope(
                f"The upstream response was interrupted: {exc}",
                "upstream_stream_interrupted",
            )
            yield f"data: {json.dumps(interrupted)}\n\n".encode()
            yield SSE_DONE
