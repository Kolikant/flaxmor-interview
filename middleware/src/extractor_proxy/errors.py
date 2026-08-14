"""The proxy's error vocabulary.

Kept out of `upstream.py` because this shape is the service's *public* contract, not
part of the OpenAI hop: a malformed request body and an unloadable prompt document both
answer in it without any upstream call being involved.
"""

from __future__ import annotations

import json
from typing import Any

from extractor_proxy.observability import request_id_var


def error_envelope(
    message: str,
    error_type: str,
    code: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-shaped error body.

    Clients of an OpenAI-compatible API already know how to read this shape, so
    failures originating in the proxy are dressed the same way as upstream ones.
    Open WebUI in particular surfaces `error.message` directly as its toast text, so
    that field always has to be a plain string.

    The request id is included when there is one. It is an addition to OpenAI's shape
    — clients ignore fields they do not know — and it is what turns "the browser showed
    me an error" into a single log line: the same id is in the `x-request-id` response
    header and on every log record emitted while handling the request.

    `request_id` is normally read from the contextvar. The override exists for one
    caller: the unhandled-exception handler runs inside ServerErrorMiddleware, outside
    the lifecycle middleware, by which point the contextvar has been reset — so it
    sources the id from the ASGI scope and passes it in. Attaching it stays this
    function's job either way.
    """
    error: dict[str, Any] = {"message": message, "type": error_type, "code": code}

    resolved = request_id or request_id_var.get()
    if resolved:
        error["request_id"] = resolved

    return {"error": error}


def error_body(message: str, error_type: str, code: str | None = None) -> bytes:
    """The same envelope, serialised — for paths that relay bytes rather than dicts."""
    return json.dumps(error_envelope(message, error_type, code)).encode()
