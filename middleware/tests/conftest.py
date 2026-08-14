"""Shared fixtures and helpers.

Settings are environment-driven, so every test runs with the service's own variables
cleared and from a scratch working directory. Without this a developer's local .env or
exported OPENAI_API_KEY would silently change what the suite asserts.
"""

from __future__ import annotations

import logging

import pytest

from extractor_proxy.config import Settings, get_settings

#: A stand-in for the upstream credential. Shared so the suite cannot end up asserting
#: against two different dummy keys.
DUMMY_API_KEY = "sk-configured"


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch, tmp_path):
    # Derived from the model rather than hand-listed: a hand-maintained list silently
    # stops covering the next setting added, and the failure mode is a test that passes
    # in CI and fails on a machine that happens to export it.
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def sse(*chunks: bytes):
    """An async byte stream, which is what httpx requires of an async response body."""
    for chunk in chunks:
        yield chunk


def log_events(caplog) -> dict[str, logging.LogRecord]:
    """Captured records keyed by event name, since the message *is* the event name."""
    return {record.getMessage(): record for record in caplog.records}
