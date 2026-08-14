"""Structured JSON logging and per-request lifecycle instrumentation."""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"

#: Set by the lifecycle middleware and read by the formatter, so every log line
#: emitted while handling a request carries its id without being passed around.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Attributes the stdlib puts on every LogRecord. Anything else on the record came
#: from an `extra={...}` at the call site and is merged into the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "message",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders each record as a single-line JSON object.

    The log message is treated as an *event name* (`http.request.start`) rather than
    a sentence, so lines group and filter cleanly in a log aggregator.
    """

    def __init__(self, service_name: str = "extractor-proxy") -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "service": self.service_name,
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload["error"] = {
                "type": exc_type.__name__ if exc_type else None,
                "message": str(exc_value) if exc_value else None,
                "stack": self.formatException(record.exc_info),
            }

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", service_name: str = "extractor-proxy") -> None:
    """Point the root logger at stdout with JSON formatting.

    Uvicorn's own loggers have their handlers removed so their output is reformatted
    by the root handler instead of arriving as unstructured text alongside ours.
    Called from the process entrypoint rather than the app factory, so importing the
    app in tests never mutates global logging state.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name=service_name))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(noisy)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def _inbound_request_id(scope: Scope) -> str | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.decode("latin-1").lower() == REQUEST_ID_HEADER:
            return raw_value.decode("latin-1").strip() or None
    return None


class RequestLifecycleMiddleware:
    """Logs the start, first byte and completion of every HTTP request.

    Implemented as raw ASGI rather than BaseHTTPMiddleware on purpose: this service
    mainly returns streamed SSE, and BaseHTTPMiddleware hands control back as soon
    as the response *starts*. Wrapping `send` instead lets us record both
    time-to-first-byte and the total duration once the last chunk has gone out,
    which is what makes a streaming proxy's latency legible.
    """

    def __init__(self, app: ASGIApp, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or logging.getLogger("extractor_proxy.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        method = scope.get("method", "")
        path = scope.get("path", "")
        status: int | None = None
        body_bytes = 0
        closed = False

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 2)

        self.logger.info(
            "http.request.start",
            extra={"method": method, "path": path, "query": scope.get("query_string", b"").decode()},
        )

        async def send_wrapper(message: Message) -> None:
            nonlocal status, body_bytes, closed

            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("x-request-id", request_id)
                self.logger.info(
                    "http.response.start",
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "first_byte_ms": elapsed_ms(),
                    },
                )
            elif message["type"] == "http.response.body":
                body_bytes += len(message.get("body", b"") or b"")
                if not message.get("more_body", False):
                    closed = True
                    self.logger.info(
                        "http.request.end",
                        extra={
                            "method": method,
                            "path": path,
                            "status": status,
                            "duration_ms": elapsed_ms(),
                            "response_bytes": body_bytes,
                        },
                    )

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Logged here so a crash still produces a lifecycle-closing line with the
            # request id attached; the exception continues up to the ASGI server.
            closed = True
            self.logger.exception(
                "http.request.failed",
                extra={"method": method, "path": path, "duration_ms": elapsed_ms()},
            )
            raise
        finally:
            if not closed:
                # Every request gets a terminal line, including the two cases neither
                # arm above can see. A client that disconnects mid-stream never
                # produces a final body message, because Starlette cancels the body
                # writer and returns normally; and asyncio.CancelledError is a
                # BaseException, so `except Exception` cannot catch it. Without this,
                # pressing stop leaves a request that looks open forever.
                self.logger.warning(
                    "http.request.cancelled",
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": elapsed_ms(),
                        "response_bytes": body_bytes,
                    },
                )
            request_id_var.reset(token)
