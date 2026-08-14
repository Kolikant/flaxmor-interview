"""Runtime configuration, read once from the environment at startup."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROMPT_FILENAME = "SYSTEM_PROMPT.md"


def discover_system_prompt_path() -> Path:
    """Locate SYSTEM_PROMPT.md by walking up from this module.

    The assignment requires the prompt to live in the repo as SYSTEM_PROMPT.md, so
    that file is treated as the single source of truth and loaded at startup rather
    than duplicated into a Python constant that could drift from the document. The
    container image copies it alongside the package root, which this walk also finds.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / PROMPT_FILENAME
        if candidate.is_file():
            return candidate
    return Path(PROMPT_FILENAME)


class Settings(BaseSettings):
    """Settings are read from the process environment, or a local .env when present."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Upstream OpenAI credentials. The key is never defaulted to a usable value: the
    # service starts without it so /healthz still answers, but /readyz reports unready.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # Models advertised to Open WebUI on GET /v1/models, as a comma-separated list.
    exposed_models: str = "gpt-4o-mini"

    # Total budget for an upstream call, and the slice of it allowed for connecting.
    request_timeout_seconds: float = 90.0
    connect_timeout_seconds: float = 10.0

    log_level: str = "INFO"
    service_name: str = "extractor-proxy"
    system_prompt_path: Path = Field(default_factory=discover_system_prompt_path)

    @field_validator("openai_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def exposed_model_ids(self) -> list[str]:
        """`exposed_models` split into ids.

        Kept as a plain string field rather than a `list[str]`, because
        pydantic-settings decodes complex-typed fields as JSON, which would force
        operators to write `["gpt-4o-mini"]` in a .env file instead of a bare list.
        """
        return [model.strip() for model in self.exposed_models.split(",") if model.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton; cache cleared in tests via `cache_clear()`."""
    return Settings()
