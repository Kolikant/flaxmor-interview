from __future__ import annotations

import json
import logging

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from extractor_proxy.config import Settings
from extractor_proxy.main import create_app
from extractor_proxy.upstream import UpstreamClient

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"a": 1}'}}],
}

TITLE_TEMPLATE = (
    "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history."
)


def build_app(handler, **overrides):
    settings = Settings(**{"openai_api_key": "sk-configured", **overrides})
    upstream = UpstreamClient(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return create_app(settings, upstream=upstream)


def recording_handler(response_factory):
    """A MockTransport handler that records the bodies it was sent."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response_factory(request)

    return handler, seen


async def sse(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def user_turn(text="Invoice A-4491 total 82.10", **extra) -> dict:
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": text}], **extra}


# --- GET /v1/models ---------------------------------------------------------


def test_models_are_listed_in_the_openai_envelope():
    app = build_app(lambda r: httpx.Response(200, json=COMPLETION), exposed_models="gpt-4o,gpt-5")

    with TestClient(app) as client:
        body = client.get("/v1/models").json()

    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == ["gpt-4o", "gpt-5"]
    assert {entry["object"] for entry in body["data"]} == {"model"}


def test_each_model_entry_carries_the_fields_open_webui_reads():
    app = build_app(lambda r: httpx.Response(200, json=COMPLETION))

    with TestClient(app) as client:
        entry = client.get("/v1/models").json()["data"][0]

    # Open WebUI subscripts model["id"] directly while merging lists, so a missing id
    # discards the whole list rather than the single entry.
    assert entry["id"] == "gpt-4o-mini"
    assert entry["object"] == "model"
    assert isinstance(entry["created"], int)
    assert entry["owned_by"] == "openai"


def test_listing_models_never_calls_the_upstream():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))
    app = build_app(handler)

    with TestClient(app) as client:
        assert client.get("/v1/models").status_code == 200

    # Serving from configuration keeps the selector populated when OpenAI is down.
    assert seen == []


# --- POST /v1/chat/completions, non-streaming -------------------------------


def test_the_extraction_prompt_reaches_the_upstream_first():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))
    app = build_app(handler)

    with TestClient(app) as client:
        client.post("/v1/chat/completions", json=user_turn())

    sent = seen[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "structured data extraction engine" in sent[0]["content"]
    assert sent[1]["content"] == "Invoice A-4491 total 82.10"


def test_an_open_webui_task_request_passes_through_untouched():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))
    app = build_app(handler)

    with TestClient(app) as client:
        client.post("/v1/chat/completions", json=user_turn(TITLE_TEMPLATE, stream=False))

    # Injecting here would turn every chat title into an extraction envelope.
    assert seen[0]["messages"] == [{"role": "user", "content": TITLE_TEMPLATE}]


def test_a_non_streaming_response_is_relayed_unchanged():
    app = build_app(lambda r: httpx.Response(200, json=COMPLETION))

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn())

    assert response.status_code == 200
    assert response.json() == COMPLETION


def test_an_inbound_authorization_header_is_not_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=COMPLETION)

    with TestClient(build_app(handler)) as client:
        client.post(
            "/v1/chat/completions",
            json=user_turn(),
            headers={"Authorization": "Bearer sk-dummy-from-open-webui"},
        )

    assert seen["authorization"] == "Bearer sk-configured"


def test_an_upstream_error_status_is_relayed_with_its_body():
    body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
    app = build_app(lambda r: httpx.Response(429, json=body))

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn())

    assert response.status_code == 429
    assert response.json() == body


def test_an_unreachable_upstream_becomes_a_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with TestClient(build_app(handler)) as client:
        response = client.post("/v1/chat/completions", json=user_turn())

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"


def test_a_malformed_request_body_is_rejected_before_the_upstream_is_called():
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))

    with TestClient(build_app(handler)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert seen == []


@pytest.mark.parametrize("body", [b"[]", b'"a string"', b"42"], ids=["array", "string", "number"])
def test_a_json_body_that_is_not_an_object_is_rejected(body):
    # Valid JSON but not a dict: reaching prompt injection with this would raise an
    # AttributeError and surface as a 500 with a stack trace.
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))

    with TestClient(build_app(handler)) as client:
        response = client.post(
            "/v1/chat/completions", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == 400
    assert seen == []


# --- POST /v1/chat/completions, streaming -----------------------------------

STREAM_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"{"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"}"}}]}\n\n',
    b"data: [DONE]\n\n",
]


def test_a_streaming_response_is_served_as_an_event_stream():
    app = build_app(
        lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(*STREAM_CHUNKS)
        )
    )

    with TestClient(app) as client:
        with client.stream("POST", "/v1/chat/completions", json=user_turn(stream=True)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            # No content-length means the body was streamed rather than buffered whole.
            assert "content-length" not in response.headers
            body = b"".join(response.iter_bytes())

    assert body == b"".join(STREAM_CHUNKS)


def test_a_usage_chunk_with_no_choices_passes_through_untouched():
    # Open WebUI sends stream_options.include_usage, whose final chunk carries
    # "choices": []. Forwarding bytes means this cannot crash the proxy — a parser
    # doing chunk["choices"][0] would. The test pins the property.
    usage_chunk = b'data: {"choices":[],"usage":{"total_tokens":31}}\n\n'
    app = build_app(
        lambda r: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(STREAM_CHUNKS[0], usage_chunk, b"data: [DONE]\n\n"),
        )
    )

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn(stream=True))

    assert response.status_code == 200
    assert usage_chunk in response.content


def test_a_streaming_failure_before_the_first_chunk_still_gets_a_real_status():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with TestClient(build_app(handler)) as client:
        response = client.post("/v1/chat/completions", json=user_turn(stream=True))

    # The first chunk is pulled before the response starts, which is what keeps a
    # status code available here instead of a 200 carrying an error event.
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"


def test_a_streaming_upstream_rejection_is_relayed_with_its_status():
    body = {"error": {"message": "invalid api key", "type": "invalid_request_error"}}
    app = build_app(lambda r: httpx.Response(401, json=body))

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn(stream=True))

    assert response.status_code == 401
    assert response.json() == body


def test_a_mid_stream_failure_is_reported_inside_the_stream():
    async def failing():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("connection dropped")

    app = build_app(lambda r: httpx.Response(200, content=failing()))

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn(stream=True))

    # Status was committed with the first chunk, so the failure can only be carried
    # in-band. Open WebUI shows a message instead of a truncated one.
    assert response.status_code == 200
    events = [line for line in response.content.split(b"\n\n") if line]
    assert b"partial" in events[0]
    assert json.loads(events[-2].removeprefix(b"data: "))["error"]["type"] == (
        "upstream_stream_interrupted"
    )
    assert events[-1] == b"data: [DONE]"


# --- degraded startup -------------------------------------------------------


def test_chat_is_refused_when_the_prompt_document_is_unloadable(tmp_path):
    handler, seen = recording_handler(lambda r: httpx.Response(200, json=COMPLETION))
    app = build_app(handler, system_prompt_path=tmp_path / "absent.md")

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn())

    # Proxying promptless would quietly turn the product into a plain GPT relay.
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "prompt_unavailable"
    assert seen == []


def test_models_still_list_when_the_prompt_is_unloadable(tmp_path):
    app = build_app(
        lambda r: httpx.Response(200, json=COMPLETION), system_prompt_path=tmp_path / "absent.md"
    )

    with TestClient(app) as client:
        assert client.get("/v1/models").status_code == 200


# --- observability ----------------------------------------------------------


def test_the_upstream_outcome_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.upstream")
    app = build_app(lambda r: httpx.Response(200, json=COMPLETION))

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=user_turn())

    assert response.headers["x-request-id"]
    logged = {record.getMessage(): record for record in caplog.records}
    assert logged["upstream.response"].status == 200
    assert logged["upstream.response"].streamed is False
