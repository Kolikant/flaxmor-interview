"""Runtime configuration, read once from the environment at startup."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROMPT_FILENAME = "SYSTEM_PROMPT.md"


def discover_system_prompt_path() -> Path:
    """Locate SYSTEM_PROMPT.md by walking up from this module, for a source checkout.

    The assignment requires the prompt to live in the repo as SYSTEM_PROMPT.md, so
    that file is treated as the single source of truth and loaded at startup rather
    than duplicated into a Python constant that could drift from the document.

    This walk serves local runs and the test suite only. It cannot work in the
    container, where the package is installed into site-packages and no parent of it
    is the repository — which is exactly why docker-compose.yml sets
    SYSTEM_PROMPT_PATH explicitly.

    The search stops at the repository root rather than continuing to the filesystem
    root, so an unrelated SYSTEM_PROMPT.md sitting somewhere above the checkout cannot
    silently become the prompt this service runs. Note the boundary is the repo, not
    the package: pyproject.toml sits in middleware/, one level *below* the document.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / PROMPT_FILENAME
        if candidate.is_file():
            return candidate
        if (parent / ".git").exists():
            break
    return Path(PROMPT_FILENAME).resolve()


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

    # httpx applies timeouts per operation rather than per request, so there is
    # deliberately no overall deadline: a single total budget would abort a stream that
    # is still legitimately producing tokens. Connect stays short to fail fast on a
    # dead upstream; read bounds the gap *between* streamed chunks.
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 90.0

    # Ceiling on a request body, checked before it is read. Generous, because a pasted
    # document is legitimately large — the point is that "large" has a limit at all, so
    # one caller cannot make the process buffer an arbitrary amount of memory.
    max_request_bytes: int = 8 * 1024 * 1024

    log_level: str = "INFO"
    service_name: str = "extractor-proxy"
    system_prompt_path: Path = Field(default_factory=discover_system_prompt_path)

    # Bind address for the entrypoint in __main__.py. Defaults to all interfaces
    # because the process is expected to run inside a container.
    host: str = "0.0.0.0"
    port: int = 8000

    @field_validator("openai_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("openai_base_url")
    @classmethod
    def _reject_credentials_in_url(cls, value: str) -> str:
        """Refuse a base URL carrying userinfo.

        httpx logs request URLs without masking userinfo, so `https://user:pass@host`
        would put a credential into the log stream of a service that is otherwise
        careful never to log one. Failing at startup is better than leaking quietly.
        """
        if "@" in urlsplit(value).netloc:
            raise ValueError(
                "OPENAI_BASE_URL must not embed credentials; pass the key via OPENAI_API_KEY"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        return value.upper()

    def redacted_summary(self) -> dict[str, Any]:
        """Every effective setting, safe to log, with the key reduced to a fingerprint.

        Logged once at startup because "which configuration is this container actually
        running?" is the first question of most debugging sessions, and the answer
        otherwise lives across a .env file, compose defaults and the process
        environment. The key is reported as present-or-not plus a length, which is
        enough to tell "unset" from "set to the wrong thing" without printing it.
        """
        return {
            "openai_base_url": self.openai_base_url,
            "openai_api_key_present": bool(self.openai_api_key),
            "openai_api_key_length": len(self.openai_api_key),
            "exposed_models": self.exposed_model_ids,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "read_timeout_seconds": self.read_timeout_seconds,
            "max_request_bytes": self.max_request_bytes,
            "system_prompt_path": str(self.system_prompt_path),
            "log_level": self.log_level,
            "host": self.host,
            "port": self.port,
        }

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
