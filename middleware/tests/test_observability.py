from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from conftest import log_events as events
from extractor_proxy.observability import (
    JsonFormatter,
    RequestLifecycleMiddleware,
    redact_secrets,
    request_id_var,
)


@pytest.fixture
def json_logger():
    """A logger wired to the JSON formatter, plus a reader for the emitted lines."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(service_name="test-service"))

    logger = logging.getLogger("extractor_proxy.test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line]

    yield logger, lines
    logger.handlers = []


def test_every_line_is_a_json_object_with_the_core_fields(json_logger):
    logger, lines = json_logger

    logger.info("upstream.call")

    (entry,) = lines()
    assert entry["event"] == "upstream.call"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "extractor_proxy.test"
    assert entry["service"] == "test-service"
    assert entry["ts"].endswith("Z")


def test_extra_fields_are_merged_into_the_payload(json_logger):
    logger, lines = json_logger

    logger.info("upstream.response", extra={"status": 200, "duration_ms": 12.5})

    (entry,) = lines()
    assert entry["status"] == 200
    assert entry["duration_ms"] == 12.5


def test_request_id_is_attached_from_the_context(json_logger):
    logger, lines = json_logger

    token = request_id_var.set("abc123")
    try:
        logger.info("in.request")
    finally:
        request_id_var.reset(token)
    logger.info("outside.request")

    inside, outside = lines()
    assert inside["request_id"] == "abc123"
    assert "request_id" not in outside


def test_exceptions_are_rendered_as_a_structured_error(json_logger):
    logger, lines = json_logger

    try:
        raise ValueError("upstream exploded")
    except ValueError:
        logger.exception("upstream.failed")

    (entry,) = lines()
    assert entry["error"]["type"] == "ValueError"
    assert entry["error"]["message"] == "upstream exploded"
    assert "ValueError: upstream exploded" in entry["error"]["stack"]


def test_non_serialisable_extras_do_not_break_the_line(json_logger):
    logger, lines = json_logger

    logger.info("odd.payload", extra={"path_obj": object()})

    (entry,) = lines()
    assert isinstance(entry["path_obj"], str)


def probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLifecycleMiddleware)

    @app.get("/ok")
    def ok() -> dict:
        return {"ok": True}

    @app.get("/stream")
    def stream() -> StreamingResponse:
        def chunks():
            yield b"one"
            yield b"two"
            yield b"three"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("handler blew up")

    return app


def test_response_carries_a_generated_request_id():
    with TestClient(probe_app()) as client:
        response = client.get("/ok")

    assert response.headers["x-request-id"]


def test_inbound_request_id_is_reused_rather_than_replaced():
    with TestClient(probe_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "trace-from-caller"})

    assert response.headers["x-request-id"] == "trace-from-caller"


def test_lifecycle_logs_cover_start_first_byte_and_completion(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    with TestClient(probe_app()) as client:
        client.get("/ok")

    logged = events(caplog)
    assert set(logged) == {"http.request.start", "http.response.start", "http.request.end"}
    assert logged["http.request.start"].path == "/ok"
    assert logged["http.response.start"].status == 200
    assert logged["http.response.start"].first_byte_ms >= 0
    assert logged["http.request.end"].status == 200
    assert logged["http.request.end"].duration_ms >= 0


def test_streamed_responses_are_measured_to_the_last_chunk(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    with TestClient(probe_app()) as client:
        response = client.get("/stream")

    assert response.text == "onetwothree"
    end = events(caplog)["http.request.end"]
    # The whole streamed body is accounted for, not just the first chunk — the
    # reason this middleware wraps `send` instead of using BaseHTTPMiddleware.
    assert end.response_bytes == len("onetwothree")


async def drive_middleware(inner) -> list[dict]:
    """Run the lifecycle middleware around `inner` with a bare ASGI harness.

    A cancelled request cannot be provoked through TestClient, so the middleware is
    exercised directly at the ASGI boundary.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/stream",
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await RequestLifecycleMiddleware(inner)(scope, receive, send)
    return sent


async def test_a_cancelled_request_still_closes_the_lifecycle(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    async def cancelled_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise asyncio.CancelledError

    # CancelledError is a BaseException on 3.11, so an `except Exception` arm cannot
    # see it. Without a terminal event here the request looks open forever.
    with pytest.raises(asyncio.CancelledError):
        await drive_middleware(cancelled_app)

    logged = events(caplog)
    assert "http.request.cancelled" in logged
    assert logged["http.request.cancelled"].status == 200
    assert logged["http.request.cancelled"].response_bytes == len(b"partial")


async def test_a_stream_abandoned_without_a_final_chunk_is_still_closed(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    async def disconnected_app(scope, receive, send):
        # Starlette cancels the body writer on client disconnect and returns
        # normally, so the final more_body=False message never arrives.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"chunk", "more_body": True})

    await drive_middleware(disconnected_app)

    logged = events(caplog)
    assert "http.request.end" not in logged
    assert logged["http.request.cancelled"].response_bytes == len(b"chunk")


async def test_a_completed_request_is_not_reported_as_cancelled(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    async def complete_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    await drive_middleware(complete_app)

    logged = events(caplog)
    assert "http.request.end" in logged
    assert "http.request.cancelled" not in logged


def test_a_failing_handler_still_closes_the_lifecycle(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    with TestClient(probe_app()) as client:
        with pytest.raises(RuntimeError, match="handler blew up"):
            client.get("/boom")

    failure = events(caplog)["http.request.failed"]
    assert failure.path == "/boom"
    assert failure.exc_info is not None


async def test_a_non_http_scope_is_passed_through_untouched(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")
    seen = {}

    async def inner(scope, receive, send):
        seen["scope"] = scope

    scope = {"type": "lifespan"}
    await RequestLifecycleMiddleware(inner)(scope, None, None)

    assert seen["scope"] is scope
    assert caplog.records == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A short prefix survives so two log lines about the same key still correlate;
        # four characters of a 164-character key is not a meaningful disclosure.
        ("sk-proj-AbCdEfGhIjKlMnOpQrSt", "sk-proj...redacted"),
        ("Bearer sk-1234567890abcdef", "Bearer sk-1234...redacted"),
        ("nothing secret here", "nothing secret here"),
    ],
    ids=["project-key", "bearer", "innocuous"],
)
def test_key_shaped_values_are_masked(raw, expected):
    assert redact_secrets(raw) == expected


def test_a_credential_reaching_a_log_line_is_masked_in_the_output(json_logger):
    # Containment is a property of the output, not a convention each call site has to
    # remember: the formatter merges arbitrary `extra` fields and stringifies anything.
    logger, lines = json_logger

    logger.info("upstream.call", extra={"url": "https://x@api/v1?k=sk-proj-AbCdEfGhIjKlMnOp"})

    (entry,) = lines()
    assert "sk-proj-AbCdEfGhIjKlMnOp" not in json.dumps(entry)
    assert "redacted" in entry["url"]


@pytest.mark.parametrize(
    ("inbound", "expected"),
    [
        ("trace-from-caller", "trace-from-caller"),
        ("bad\r\nX-Injected: yes", "badX-Injected:yes".replace(":", "")),
        ("x" * 200, "x" * 64),
        ("!!!", None),
    ],
    ids=["clean", "crlf-stripped", "truncated", "nothing-usable"],
)
def test_an_inbound_request_id_is_sanitised(inbound, expected):
    # Caller-controlled, and it lands in both a response header and every log record
    # for the request, so it is filtered rather than trusted.
    with TestClient(probe_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": inbound})

    returned = response.headers["x-request-id"]
    if expected is None:
        # Nothing usable survived, so a fresh id was generated instead.
        assert returned and returned != inbound
    else:
        assert returned == expected


def test_a_key_inside_a_traceback_is_masked(json_logger):
    # The formatter renders exc_info into an `error.stack` string, which redaction has
    # to cover as much as the fields beside it.
    logger, lines = json_logger

    try:
        raise ValueError("upstream rejected sk-proj-AbCdEfGhIjKlMnOpQrSt")
    except ValueError:
        logger.exception("upstream.failed")

    (entry,) = lines()
    blob = json.dumps(entry)
    assert "sk-proj-AbCdEfGhIjKlMnOpQrSt" not in blob
    assert "redacted" in blob


def test_the_http_client_logger_is_silenced_by_name():
    # The package is httpx2, so silencing only "httpx" silenced nothing — and the check
    # that was meant to confirm it exercised an endpoint that makes no upstream call.
    import logging as stdlib_logging

    from extractor_proxy.observability import configure_logging

    previous = {
        name: stdlib_logging.getLogger(name).level for name in ("httpx", "httpx2")
    }
    try:
        configure_logging(level="INFO", service_name="test")
        assert stdlib_logging.getLogger("httpx2").level == stdlib_logging.WARNING
        assert stdlib_logging.getLogger("httpx").level == stdlib_logging.WARNING
    finally:
        for name, level in previous.items():
            stdlib_logging.getLogger(name).setLevel(level)
        stdlib_logging.getLogger().handlers = []
