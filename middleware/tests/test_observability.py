from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from extractor_proxy.observability import (
    JsonFormatter,
    RequestLifecycleMiddleware,
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


def build_app() -> FastAPI:
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


def events(caplog) -> dict[str, logging.LogRecord]:
    return {record.getMessage(): record for record in caplog.records}


def test_response_carries_a_generated_request_id():
    with TestClient(build_app()) as client:
        response = client.get("/ok")

    assert response.headers["x-request-id"]


def test_inbound_request_id_is_reused_rather_than_replaced():
    with TestClient(build_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "trace-from-caller"})

    assert response.headers["x-request-id"] == "trace-from-caller"


def test_lifecycle_logs_cover_start_first_byte_and_completion(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    with TestClient(build_app()) as client:
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

    with TestClient(build_app()) as client:
        response = client.get("/stream")

    assert response.text == "onetwothree"
    end = events(caplog)["http.request.end"]
    # The whole streamed body is accounted for, not just the first chunk — the
    # reason this middleware wraps `send` instead of using BaseHTTPMiddleware.
    assert end.response_bytes == len("onetwothree")


def test_a_failing_handler_still_closes_the_lifecycle(caplog):
    caplog.set_level(logging.INFO, logger="extractor_proxy.http")

    with TestClient(build_app()) as client:
        with pytest.raises(RuntimeError, match="handler blew up"):
            client.get("/boom")

    failure = events(caplog)["http.request.failed"]
    assert failure.path == "/boom"
    assert failure.exc_info is not None
