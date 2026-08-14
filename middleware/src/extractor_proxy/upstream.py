"""The OpenAI hop.

Everything that talks to OpenAI lives here, so the routes stay concerned with HTTP
shape and this module owns the one thing that is genuinely hard: reporting a failure
that happens after the response status has already been sent.
"""

from __future__ import annotations

import asyncio
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
SSE_DONE_MARKER = b"[DONE]"


JSON_MEDIA_TYPE = "application/json"

_MAX_ERROR_BODY_BYTES = 500


def _sse_error(message: str) -> bytes:
    """An error object framed as one SSE event.

    Open WebUI's frontend special-cases a `data:` frame carrying an `error` key, so
    this reaches the user as a message rather than a silent truncation.
    """
    return b"data: " + error_body(message, "upstream_stream_interrupted") + b"\n\n"


class UpstreamError(Exception):
    """A failure the route can still turn into an HTTP status.

    Raised only while the response status is still open — either before the request
    was sent, or before the first streamed byte. Once bytes are on the wire the
    stream reports its own failure instead; see `stream_chat_completion`.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        # Read defensively: an upstream error body is relayed verbatim whenever it is a
        # JSON object, and a gateway can answer `{"detail": ...}` or even
        # `{"error": "a string"}`. Subscripting would raise inside the streaming
        # generator, where it is not an UpstreamError and so escapes the route's
        # handler as a bare 500.
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        super().__init__(str(message) if message else f"upstream returned {status_code}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class UpstreamResponse:
    """An upstream response, ready to relay without being re-encoded.

    `payload` is the same body already parsed. It is carried rather than re-derived
    because deciding what to relay requires parsing it anyway, and every consumer that
    wanted a dict was otherwise parsing the same bytes a second time.
    """

    status_code: int
    content: bytes
    payload: dict[str, Any]


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


def _relay(response: httpx.Response) -> UpstreamResponse:
    """Decide once what the client gets: status, bytes, and the parsed body.

    A JSON object — success or error — is relayed verbatim with its own status, so key
    order and number formatting survive the hop untouched; a proxy that re-serialises
    is not really a passthrough.

    Anything else is replaced with an error envelope, because a gateway in front of
    OpenAI can answer with an HTML page and a client expecting the OpenAI error shape
    should not have to cope with that. The status becomes 502 in that case: relaying
    the upstream status is only honest while the bytes are also the upstream's, and a
    proxy error page arriving as its original status with a rewritten body is the worst
    of both.

    Both response modes go through here, so that decision produces one answer. It
    previously did not: the streaming path took the bytes and discarded the status, so
    an HTML page behind a 403 surfaced as 403 while the same body on the non-streaming
    path surfaced as 502.
    """
    try:
        parsed = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        return UpstreamResponse(response.status_code, response.content, parsed)

    # Slice before decoding: `response.text` would decode and cache the whole body to
    # keep a fragment of it, which is the one place a hostile upstream makes this
    # proxy allocate in proportion to its response.
    excerpt = response.content[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace")
    envelope = error_envelope(
        f"Upstream returned a body that is not a JSON object: {excerpt}",
        "upstream_error",
        code=str(response.status_code),
    )
    return UpstreamResponse(502, json.dumps(envelope).encode(), envelope)


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

        relayed = _relay(response)
        logger.info("upstream.response", extra={"status": relayed.status_code, "streamed": False})
        return relayed

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

        The terminator is guaranteed on every path that sent bytes. A 2xx body that
        ends without `[DONE]` — an empty body, a JSON or HTML page from something that
        is not OpenAI, a connection closed cleanly mid-envelope — would otherwise leave
        the client waiting on a stream that is never coming back.
        """
        sent_any = False
        saw_done = False
        try:
            async with self.http_client.stream(
                "POST", self._chat_url, json=payload, headers=self._headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    # Same relay decision as the non-streaming path, status included.
                    relayed = _relay(response)
                    logger.warning(
                        "upstream.response",
                        extra={"status": relayed.status_code, "streamed": True},
                    )
                    raise UpstreamError(relayed.status_code, relayed.payload)

                logger.info(
                    "upstream.response",
                    extra={"status": response.status_code, "streamed": True},
                )
                async for chunk in response.aiter_bytes():
                    sent_any = True
                    # A substring check on opaque bytes, not SSE parsing: enough to
                    # know the stream terminated itself, without decoding frames.
                    saw_done = saw_done or SSE_DONE_MARKER in chunk
                    yield chunk

                if not sent_any:
                    # Nothing was sent, so the status line is still ours to choose.
                    logger.warning("upstream.stream.empty", extra={"status": response.status_code})
                    raise UpstreamError(
                        502,
                        error_envelope(
                            "Upstream returned a success status with an empty body.",
                            "upstream_empty_stream",
                        ),
                    )
        except (GeneratorExit, asyncio.CancelledError):
            # A consumer stopped early: Open WebUI's stop button, or a closed tab.
            # Starlette cancels the body-writer task on disconnect, which arrives here
            # as CancelledError — GeneratorExit only shows up for an explicit aclose().
            # Catching just the latter meant this never fired for a real disconnect.
            # The `async with` releases the upstream response either way; this names
            # the upstream-side effect, which the HTTP middleware cannot see.
            logger.info("upstream.stream.abandoned", extra={"reason": "consumer_disconnected"})
            raise
        except httpx.RequestError as exc:
            if not sent_any:
                raise _transport_failure(exc) from exc

            logger.warning("upstream.stream.interrupted", extra={"reason": type(exc).__name__})
            yield _sse_error(f"The upstream response was interrupted: {exc}")
            yield SSE_DONE
            return

        if not saw_done:
            logger.warning("upstream.stream.unterminated")
            yield _sse_error("The upstream response ended without a terminator.")
            yield SSE_DONE
