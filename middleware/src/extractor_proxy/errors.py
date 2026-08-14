"""The proxy's error vocabulary.

Kept out of `upstream.py` because this shape is the service's *public* contract, not
part of the OpenAI hop: a malformed request body and an unloadable prompt document both
answer in it without any upstream call being involved.
"""

from __future__ import annotations

import json
from typing import Any


def error_envelope(message: str, error_type: str, code: str | None = None) -> dict[str, Any]:
    """Build an OpenAI-shaped error body.

    Clients of an OpenAI-compatible API already know how to read this shape, so
    failures originating in the proxy are dressed the same way as upstream ones.
    Open WebUI in particular surfaces `error.message` directly as its toast text, so
    that field always has to be a plain string.
    """
    return {"error": {"message": message, "type": error_type, "code": code}}


def error_body(message: str, error_type: str, code: str | None = None) -> bytes:
    """The same envelope, serialised — for paths that relay bytes rather than dicts."""
    return json.dumps(error_envelope(message, error_type, code)).encode()
