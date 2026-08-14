from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from extractor_proxy import __version__
from extractor_proxy.config import Settings
from extractor_proxy.main import create_app


def client_for(**overrides) -> TestClient:
    settings = Settings(**{"openai_api_key": "sk-test", **overrides})
    return TestClient(create_app(settings))


@pytest.fixture
def ready_client() -> TestClient:
    with client_for() as client:
        yield client


def test_liveness_reports_the_service_and_version(ready_client):
    response = ready_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "extractor-proxy",
        "version": __version__,
    }


def test_liveness_stays_up_when_the_service_is_unready(tmp_path):
    # Liveness must not fail on configuration problems: a restart would not fix one,
    # and killing the container removes the endpoint that could explain it.
    settings = Settings(openai_api_key="", system_prompt_path=tmp_path / "absent.md")

    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_readiness_passes_when_the_prompt_and_key_are_both_present(ready_client):
    response = ready_client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert all(check["ok"] for check in body["checks"].values())


def test_readiness_fails_and_names_a_missing_api_key():
    with client_for(openai_api_key="") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["openai_api_key"] == {
        "ok": False,
        "detail": "OPENAI_API_KEY is not set",
    }
    # The unrelated check still reports its own state, so a probe response is a
    # diagnosis rather than a single bit.
    assert body["checks"]["system_prompt"]["ok"] is True


def test_readiness_fails_when_the_prompt_document_is_unloadable(tmp_path):
    with client_for(system_prompt_path=tmp_path / "absent.md") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    prompt_check = response.json()["checks"]["system_prompt"]
    assert prompt_check["ok"] is False
    assert "cannot read prompt document" in prompt_check["detail"]


def test_probes_carry_the_request_id_header(ready_client):
    # The lifecycle middleware wraps the probes too, so a failing readiness check can
    # be tied back to its log lines.
    assert ready_client.get("/readyz").headers["x-request-id"]
