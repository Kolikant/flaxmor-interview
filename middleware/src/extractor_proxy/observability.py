"""Structured JSON logging and per-request lifecycle instrumentation."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"

#: Set by the lifecycle middleware and read by the formatter, so every log line
#: emitted while handling a request carries its id without being passed around.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Attributes the stdlib puts on every LogRecord. Anything else on the record came
#: from an `extra={...}` at the call site and is merged into the JSON payload.
#:
#: Derived from a throwaway record rather than hand-listed, so that an attribute added
#: by a future Python release is excluded automatically instead of leaking into every
#: log line as a spurious field. `asctime` and `message` are added because they are
#: populated during formatting and so are absent from a fresh record.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message"}


#: Anything shaped like an API key is masked on its way out, wherever it came from.
#: No call site passes a credential today, but the formatter merges arbitrary `extra`
#: fields and stringifies anything, so this makes containment a property of the output
#: rather than a convention every future call site has to remember. It also covers
#: libraries logging through the same root handler — httpx logs request URLs, which
#: would carry credentials if a base URL ever embedded them.
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{8,}")


def redact_secrets(text: str) -> str:
    """Mask anything key-shaped, keeping a short prefix so lines stay correlatable."""
    return _SECRET_PATTERN.sub(r"\1...redacted", text)


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

        return redact_secrets(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(level: str = "INFO", service_name: str = "extractor-proxy") -> None:
    """Point the root logger at stdout with JSON formatting.

    Uvicorn's own loggers have their handlers removed so their output is reformatted
    by the root handler instead of arriving as unstructured text alongside ours. That
    loop is the belt to `log_config=None`'s braces in __main__: with no dictConfig
    installed those loggers already propagate, but an invocation that bypasses the
    entrypoint (`uvicorn --reload`) does install one.

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

    # The client logs one INFO line per request ("HTTP Request: POST ... 200 OK") that
    # says strictly less than the upstream.response event beside it. Both names are
    # silenced because the package is httpx2 — targeting only "httpx" silenced nothing,
    # and the check that was supposed to confirm it only ever exercised an endpoint
    # that makes no upstream call. Warnings and above still come through.
    for client_logger in ("httpx", "httpx2"):
        logging.getLogger(client_logger).setLevel(logging.WARNING)


#: An inbound trace id is caller-controlled and gets both echoed into a response
#: header and written to every log line for the request, so it is filtered to
#: characters that are safe in both and truncated. Nothing legitimate needs more.
_REQUEST_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._\-]")
_REQUEST_ID_MAX_LENGTH = 64


def _inbound_request_id(scope: Scope) -> str | None:
    """Reuse a caller's trace id so it survives the hop into this service.

    Sanitised rather than trusted: an arbitrary header value would otherwise reach a
    response header and every log record for the request.
    """
    raw = (Headers(scope=scope).get(REQUEST_ID_HEADER) or "").strip()
    return _REQUEST_ID_ALLOWED.sub("", raw)[:_REQUEST_ID_MAX_LENGTH] or None


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
        # Also on the scope, because Starlette routes an `Exception` handler to
        # ServerErrorMiddleware, which sits *outside* this middleware: by the time it
        # runs, the contextvar below has been reset and the response never passes
        # through send_wrapper. The scope is the only channel that survives.
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()

        method = scope.get("method", "")
        path = scope.get("path", "")
        status: int | None = None
        body_bytes = 0
        terminal_logged = False

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 2)

        def terminal_fields() -> dict[str, object]:
            return {
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": elapsed_ms(),
                "response_bytes": body_bytes,
            }

        self.logger.info(
            "http.request.start",
            extra={"method": method, "path": path, "query": scope.get("query_string", b"").decode()},
        )

        async def send_wrapper(message: Message) -> None:
            nonlocal status, body_bytes, terminal_logged

            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
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
                    terminal_logged = True
                    self.logger.info("http.request.end", extra=terminal_fields())

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Logged here so a crash still produces a lifecycle-closing line with the
            # request id attached; the exception continues up to the ASGI server.
            terminal_logged = True
            self.logger.exception(
                "http.request.failed",
                extra={"method": method, "path": path, "duration_ms": elapsed_ms()},
            )
            raise
        finally:
            if not terminal_logged:
                # Every request gets a terminal line, including the two cases neither
                # arm above can see. A client that disconnects mid-stream never
                # produces a final body message, because Starlette cancels the body
                # writer and returns normally; and asyncio.CancelledError is a
                # BaseException, so `except Exception` cannot catch it. Without this,
                # pressing stop leaves a request that looks open forever.
                self.logger.warning("http.request.cancelled", extra=terminal_fields())
            request_id_var.reset(token)
