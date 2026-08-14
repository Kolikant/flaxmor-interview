from __future__ import annotations

import asyncio
import json
import logging

import httpx2 as httpx
import pytest

from conftest import DUMMY_API_KEY, log_events, sse
from extractor_proxy.config import Settings
from extractor_proxy.upstream import UpstreamClient, UpstreamError

PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "a receipt"}]}

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}}],
}


def build_client(handler, **overrides) -> UpstreamClient:
    settings = Settings(**{"openai_api_key": DUMMY_API_KEY, **overrides})
    return UpstreamClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --- non-streaming ----------------------------------------------------------


async def test_a_successful_completion_is_relayed_byte_for_byte():
    # Verbatim bytes, not a re-serialised dict: key order and number formatting have
    # to survive the hop, or the proxy is not really a passthrough.
    raw = b'{"id":"chatcmpl-1","zeta":1.50,"alpha":2,"choices":[]}'
    client = build_client(
        lambda request: httpx.Response(200, content=raw, headers={"content-type": "application/json"})
    )

    response = await client.chat_completion(PAYLOAD)

    assert response.status_code == 200
    assert response.content == raw


async def test_the_configured_key_authenticates_the_upstream_call():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=COMPLETION)

    await build_client(handler).chat_completion(PAYLOAD)

    assert seen["authorization"] == f"Bearer {DUMMY_API_KEY}"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"


async def test_the_request_body_is_sent_verbatim():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=COMPLETION)

    await build_client(handler).chat_completion(PAYLOAD)

    assert seen["body"] == PAYLOAD


async def test_a_connection_failure_becomes_a_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(UpstreamError) as caught:
        await build_client(handler).chat_completion(PAYLOAD)

    assert caught.value.status_code == 502
    assert caught.value.payload["error"]["type"] == "upstream_unavailable"


async def test_a_read_timeout_becomes_a_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream too slow", request=request)

    with pytest.raises(UpstreamError) as caught:
        await build_client(handler).chat_completion(PAYLOAD)

    assert caught.value.status_code == 504
    assert caught.value.payload["error"]["type"] == "upstream_timeout"


async def test_an_upstream_error_response_keeps_its_status_and_body():
    # A 429 carrying OpenAI's own error shape is more useful to the client than
    # anything this proxy could rewrite it into.
    upstream_body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
    client = build_client(lambda request: httpx.Response(429, json=upstream_body))

    response = await client.chat_completion(PAYLOAD)

    assert response.status_code == 429
    assert json.loads(response.content) == upstream_body


async def test_a_non_json_upstream_failure_still_yields_an_error_envelope():
    client = build_client(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))

    response = await client.chat_completion(PAYLOAD)

    assert response.status_code == 502
    envelope = json.loads(response.content)
    assert envelope["error"]["type"] == "upstream_error"
    assert "bad gateway" in envelope["error"]["message"]


# --- streaming --------------------------------------------------------------


async def test_streamed_chunks_arrive_byte_for_byte():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"{"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"\\"a\\":1}"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    client = build_client(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(*chunks)
        )
    )

    received = [chunk async for chunk in client.stream_chat_completion(PAYLOAD)]

    assert received == chunks


async def test_a_failure_before_the_first_chunk_raises_so_a_status_can_still_be_set():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    stream = build_client(handler).stream_chat_completion(PAYLOAD)

    with pytest.raises(UpstreamError) as caught:
        await anext(stream)

    assert caught.value.status_code == 502


async def test_an_upstream_error_status_on_a_streaming_request_raises():
    # Still before any body byte, so the route can answer with a real status.
    upstream_body = {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
    client = build_client(lambda request: httpx.Response(401, json=upstream_body))

    with pytest.raises(UpstreamError) as caught:
        await anext(client.stream_chat_completion(PAYLOAD))

    assert caught.value.status_code == 401
    assert caught.value.payload == upstream_body


async def test_a_mid_stream_failure_ends_the_stream_with_an_error_event():
    async def failing():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("connection dropped")

    client = build_client(lambda request: httpx.Response(200, content=failing()))

    received = [chunk async for chunk in client.stream_chat_completion(PAYLOAD)]

    # The status line is already committed at this point, so the failure has to be
    # reported inside the stream rather than as an HTTP status.
    assert received[0].startswith(b'data: {"choices"')
    error_event = json.loads(received[-2].removeprefix(b"data: ").decode())
    assert error_event["error"]["type"] == "upstream_stream_interrupted"
    assert received[-1] == b"data: [DONE]\n\n"


async def test_abandoning_the_stream_logs_the_upstream_abandonment(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.upstream")
    client = build_client(
        lambda request: httpx.Response(200, content=sse(b"data: one\n\n", b"data: two\n\n"))
    )
    stream = client.stream_chat_completion(PAYLOAD)

    assert await anext(stream) == b"data: one\n\n"
    await stream.aclose()

    # Closing the generator makes the `async with` release the upstream response; the
    # log line is the observable that a consumer stopped early, which is what the HTTP
    # middleware cannot see from where it sits.
    assert log_events(caplog)["upstream.stream.abandoned"].reason == "consumer_disconnected"


# --- timeouts ---------------------------------------------------------------


async def test_connect_and_read_timeouts_are_configured_separately():
    # No overall deadline: httpx applies timeouts per operation, so a single total
    # budget would kill a stream that is legitimately still producing tokens. Built
    # without an injected transport so the client constructs its own timeout.
    settings = Settings(
        openai_api_key=DUMMY_API_KEY,
        connect_timeout_seconds=3.5,
        read_timeout_seconds=45.0,
    )
    client = UpstreamClient(settings)

    timeout = client.http_client.timeout

    assert timeout.connect == 3.5
    assert timeout.read == 45.0
    await client.aclose()


# --- terminator guarantees found by the code-review pass ---------------------


async def test_a_stream_ending_without_done_gets_a_terminator_appended():
    # A 2xx SSE body that stops without [DONE] — a connection closed cleanly
    # mid-envelope — would otherwise leave the client waiting on a stream that is
    # never coming back.
    client = build_client(
        lambda request: httpx.Response(200, content=sse(b'data: {"choices":[]}\n\n'))
    )

    received = [chunk async for chunk in client.stream_chat_completion(PAYLOAD)]

    assert received[-1] == b"data: [DONE]\n\n"
    error = json.loads(received[-2].removeprefix(b"data: "))
    assert error["error"]["type"] == "upstream_stream_interrupted"


async def test_a_well_formed_stream_gets_no_extra_terminator():
    chunks = [b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"]
    client = build_client(lambda request: httpx.Response(200, content=sse(*chunks)))

    received = [chunk async for chunk in client.stream_chat_completion(PAYLOAD)]

    assert received == chunks


async def test_an_empty_success_body_raises_before_any_byte_is_sent():
    # Nothing has been sent, so the status line is still ours — a real 502 beats a 200
    # carrying an in-band error.
    client = build_client(lambda request: httpx.Response(200, content=sse()))

    with pytest.raises(UpstreamError) as caught:
        await anext(client.stream_chat_completion(PAYLOAD))

    assert caught.value.status_code == 502
    assert caught.value.payload["error"]["type"] == "upstream_empty_stream"


async def test_a_streaming_error_body_that_is_not_openai_shaped_still_raises_cleanly():
    # _relayable_body relays any JSON object verbatim, so a gateway answering
    # {"detail": ...} used to raise KeyError inside the generator — which is not an
    # UpstreamError, so the route could not catch it and the client got a bare 500.
    client = build_client(lambda request: httpx.Response(502, json={"detail": "bad gateway"}))

    with pytest.raises(UpstreamError) as caught:
        await anext(client.stream_chat_completion(PAYLOAD))

    assert caught.value.status_code == 502
    assert caught.value.payload == {"detail": "bad gateway"}


async def test_an_error_body_that_is_a_json_string_does_not_crash():
    client = build_client(lambda request: httpx.Response(500, json={"error": "plain string"}))

    with pytest.raises(UpstreamError) as caught:
        await anext(client.stream_chat_completion(PAYLOAD))

    assert caught.value.status_code == 500


async def test_a_non_object_body_becomes_a_502_rather_than_relaying_the_status():
    # A corporate proxy's HTML page arriving as 200 must not reach the client as a 200
    # whose body is an error envelope; relaying the status is only honest while the
    # bytes are also the upstream's.
    client = build_client(lambda request: httpx.Response(200, text="<html>captive portal</html>"))

    response = await client.chat_completion(PAYLOAD)

    assert response.status_code == 502
    assert json.loads(response.content)["error"]["type"] == "upstream_error"


async def test_a_cancelled_consumer_is_logged_like_an_abandoned_stream(caplog):
    # Starlette cancels the body-writer task on client disconnect, so a real stop
    # arrives as CancelledError. Catching only GeneratorExit meant this never fired
    # outside tests that called aclose() by hand.
    caplog.set_level(logging.INFO, logger="extractor_proxy.upstream")
    client = build_client(
        lambda request: httpx.Response(200, content=sse(b"data: one\n\n", b"data: two\n\n"))
    )
    stream = client.stream_chat_completion(PAYLOAD)
    assert await anext(stream) == b"data: one\n\n"

    with pytest.raises(asyncio.CancelledError):
        await stream.athrow(asyncio.CancelledError())

    assert log_events(caplog)["upstream.stream.abandoned"].reason == "consumer_disconnected"
