"""Shared fixtures.

Settings are environment-driven, so every test runs with the service's own
variables cleared and from a scratch working directory. Without this a developer's
local .env or exported OPENAI_API_KEY would silently change what the suite asserts.
"""

from __future__ import annotations

import pytest

from extractor_proxy.config import get_settings

SERVICE_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "EXPOSED_MODELS",
    "CONNECT_TIMEOUT_SECONDS",
    "READ_TIMEOUT_SECONDS",
    "LOG_LEVEL",
    "SERVICE_NAME",
    "SYSTEM_PROMPT_PATH",
)


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch, tmp_path):
    for name in SERVICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
