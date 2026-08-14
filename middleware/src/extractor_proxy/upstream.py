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
from extractor_proxy.errors import error_body, error_envelope

logger = logging.getLogger("extractor_proxy.upstream")

#: Terminator every OpenAI-compatible SSE stream ends with. Open WebUI waits for it.
SSE_DONE = b"data: [DONE]\n\n"

JSON_MEDIA_TYPE = "application/json"

_MAX_ERROR_BODY_BYTES = 500


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
    """An upstream response, ready to relay without being re-encoded."""

    status_code: int
    content: bytes


def _transport_failure(exc: httpx.RequestError) -> UpstreamError:
    """Map an httpx transport failure onto a gateway status, and log it.

    Logging happens here rather than at the call sites because both the streaming and
    non-streaming paths need the identical line, and the mapping is what knows the
    status and reason it should carry.
    """
    if isinstance(exc, httpx.TimeoutException):
        error = UpstreamError(
            504,
            error_envelope(f"Upstream request to OpenAI timed out: {exc}", "upstream_timeout"),
        )
    else:
        error = UpstreamError(
            502,
            error_envelope(f"Could not reach OpenAI: {exc}", "upstream_unavailable"),
        )

    logger.warning(
        "upstream.request.failed",
        extra={"status": error.status_code, "reason": error.payload["error"]["type"]},
    )
    return error


def _relayable_body(response: httpx.Response) -> bytes:
    """The bytes to hand back to the client.

    A JSON body — success or error — is relayed verbatim, so key order and number
    formatting survive the hop untouched; a proxy that re-serialises is not really a
    passthrough. Only a non-JSON body gets replaced, because a gateway in front of
    OpenAI can answer with HTML and a client expecting the OpenAI error shape should
    not have to cope with that.
    """
    try:
        parsed = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        return response.content

    # Slice before decoding: `response.text` would decode and cache the whole body to
    # keep a fragment of it, which is the one place a hostile upstream makes this
    # proxy allocate in proportion to its response.
    excerpt = response.content[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace")
    return error_body(
        f"Upstream returned a non-JSON body: {excerpt}",
        "upstream_error",
        code=str(response.status_code),
    )


def _error_payload(response: httpx.Response) -> dict[str, Any]:
    """An upstream error as a dict, for the streaming path's raise-before-first-byte."""
    return json.loads(_relayable_body(response))


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

        # Both are fixed for the client's lifetime, so they are built once here rather
        # than per request. The key is the configured one and nothing else: Open WebUI
        # holds a dummy, so forwarding what the caller sent would hand OpenAI a bogus
        # credential, and trusting it would make the real key client-supplied.
        self._chat_url = f"{settings.openai_base_url}/chat/completions"
        self._headers = {
            "authorization": f"Bearer {settings.openai_api_key}",
            "content-type": JSON_MEDIA_TYPE,
            # Ask for an unencoded body. httpx decodes transparently but leaves the
            # original content-encoding on the response, so an encoded upstream reply
            # invites a header that no longer describes the bytes being relayed.
            "accept-encoding": "identity",
        }

    async def aclose(self) -> None:
        await self.http_client.aclose()

    async def chat_completion(self, payload: dict[str, Any]) -> UpstreamResponse:
        """Send a non-streaming completion and relay whatever came back.

        Non-2xx responses are returned rather than raised: an upstream 429 with
        OpenAI's own error body is more useful to the client than anything this proxy
        would rewrite it into. Only transport failures raise.
        """
        try:
            response = await self.http_client.post(
                self._chat_url, json=payload, headers=self._headers
            )
        except httpx.RequestError as exc:
            raise _transport_failure(exc) from exc

        logger.info("upstream.response", extra={"status": response.status_code, "streamed": False})
        return UpstreamResponse(
            status_code=response.status_code, content=_relayable_body(response)
        )

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
                "POST", self._chat_url, json=payload, headers=self._headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    logger.warning(
                        "upstream.response",
                        extra={"status": response.status_code, "streamed": True},
                    )
                    raise UpstreamError(response.status_code, _error_payload(response))

                logger.info(
                    "upstream.response",
                    extra={"status": response.status_code, "streamed": True},
                )
                async for chunk in response.aiter_bytes():
                    sent_any = True
                    yield chunk
        except GeneratorExit:
            # Purely observational. The `async with` above already releases the
            # upstream response when this propagates; this only names the upstream-side
            # effect of a consumer stopping early — Open WebUI's stop button, or a
            # closed tab — which the HTTP middleware cannot see from where it sits.
            logger.info("upstream.stream.abandoned", extra={"reason": "consumer_disconnected"})
            raise
        except httpx.RequestError as exc:
            if not sent_any:
                raise _transport_failure(exc) from exc

            logger.warning("upstream.stream.interrupted", extra={"reason": type(exc).__name__})
            yield b"data: " + error_body(
                f"The upstream response was interrupted: {exc}",
                "upstream_stream_interrupted",
            ) + b"\n\n"
            yield SSE_DONE
