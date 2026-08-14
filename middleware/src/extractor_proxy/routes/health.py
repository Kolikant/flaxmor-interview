"""Liveness and readiness probes.

The split follows the usual contract: liveness answers "is this process healthy
enough to keep", readiness answers "should traffic be sent to it right now".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from extractor_proxy import __version__

router = APIRouter(tags=["operations"])


@router.get("/healthz", summary="Liveness — is the process up")
def liveness(request: Request) -> dict[str, Any]:
    """Always 200 while the process can serve a request.

    Deliberately dependency-free. A liveness probe that fails on a bad
    configuration or a sick upstream gets the container killed and restarted, which
    fixes neither and destroys the endpoint you would have asked what went wrong.
    """
    return {
        "status": "ok",
        "service": request.app.state.settings.service_name,
        "version": __version__,
    }


@router.get("/readyz", summary="Readiness — can this instance serve chat traffic")
def readiness(request: Request, response: Response) -> dict[str, Any]:
    """200 when the instance can serve chat traffic, 503 with the reason when not.

    Checks local preconditions only, and does not call OpenAI. Two reasons: a probe
    that spends a chat completion every few seconds costs real money on a real key,
    and letting an upstream blip mark every replica unready withdraws the whole
    service at exactly the moment it should be returning 502s and logging why.
    Upstream trouble belongs on the request path, not in orchestration signals.
    """
    state = request.app.state
    checks = {
        "system_prompt": _check(
            ok=state.system_prompt is not None,
            detail=state.system_prompt_error or f"loaded from {state.settings.system_prompt_path}",
        ),
        "openai_api_key": _check(
            ok=bool(state.settings.openai_api_key),
            detail="configured" if state.settings.openai_api_key else "OPENAI_API_KEY is not set",
        ),
    }

    ready = all(check["ok"] for check in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready else "not_ready", "checks": checks}


def _check(*, ok: bool, detail: str) -> dict[str, Any]:
    return {"ok": ok, "detail": detail}
