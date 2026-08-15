from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from extractor_proxy import config
from extractor_proxy.config import (
    PROMPT_FILENAME,
    Settings,
    discover_system_prompt_path,
    get_settings,
)


def test_exposed_models_is_split_and_trimmed():
    settings = Settings(exposed_models=" gpt-4o-mini , gpt-4o ,, ")

    assert settings.exposed_model_ids == ["gpt-4o-mini", "gpt-4o"]


def test_exposed_models_defaults_to_one_chat_model():
    assert Settings().exposed_model_ids == ["gpt-4o-mini"]


def test_base_url_loses_its_trailing_slash():
    # Upstream paths are built as f"{base_url}/chat/completions", so a trailing
    # slash would produce a double slash and a 404 from some gateways.
    settings = Settings(openai_base_url="https://api.openai.com/v1/")

    assert settings.openai_base_url == "https://api.openai.com/v1"


def test_log_level_is_normalised_to_upper_case():
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_api_key_defaults_to_empty_so_the_process_can_still_boot():
    # Readiness reports the missing key; startup itself must not crash, otherwise a
    # misconfigured deployment gives no endpoint to ask what is wrong.
    assert Settings().openai_api_key == ""


def test_settings_read_values_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("EXPOSED_MODELS", "gpt-4o")

    settings = Settings()

    assert settings.openai_api_key == "sk-from-env"
    assert settings.exposed_model_ids == ["gpt-4o"]


def test_system_prompt_path_points_at_the_repo_document():
    assert discover_system_prompt_path().name == PROMPT_FILENAME


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_the_walk_stops_at_the_repository_root(tmp_path, monkeypatch):
    # An unrelated SYSTEM_PROMPT.md above the checkout must not become the prompt this
    # service runs, so the search stops at the repo boundary rather than at "/".
    #
    # The decoy goes in a directory that is NOT the working directory: the fixture
    # chdirs to tmp_path, and the not-found fallback is cwd-relative, so a decoy placed
    # in tmp_path itself would be returned by the fallback and the assertion would pass
    # or fail for the wrong reason.
    above = tmp_path / "above"
    repo = above / "repo"
    package = repo / "middleware" / "src" / "extractor_proxy"
    package.mkdir(parents=True)
    (repo / ".git").mkdir()
    decoy = above / "SYSTEM_PROMPT.md"
    decoy.write_text("not ours", encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))

    assert config.discover_system_prompt_path() != decoy


def test_the_document_is_found_at_the_repository_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    package = repo / "middleware" / "src" / "extractor_proxy"
    package.mkdir(parents=True)
    (repo / ".git").mkdir()
    document = repo / "SYSTEM_PROMPT.md"
    document.write_text("ours", encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))

    assert config.discover_system_prompt_path() == document


def test_the_fallback_is_absolute_so_the_error_names_a_real_path(tmp_path, monkeypatch):
    # A cwd-relative fallback produced "cannot read prompt document at
    # SYSTEM_PROMPT.md", which gives an operator nothing to act on.
    package = tmp_path / "nowhere" / "extractor_proxy"
    package.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))

    fallback = config.discover_system_prompt_path()

    assert fallback.is_absolute()
    assert fallback.name == PROMPT_FILENAME


def test_a_base_url_embedding_credentials_is_refused():
    # httpx logs request URLs without masking userinfo, so this would put a credential
    # into the log stream of a service that is otherwise careful never to log one.
    with pytest.raises(ValidationError):
        Settings(openai_api_key="sk-test", openai_base_url="https://u:pw@api.openai.com/v1")


def test_the_rejection_message_does_not_contain_the_credential():
    # pydantic embeds the offending input in every ValidationError, so the validator
    # written to keep a credential out of the logs was printing one — and settings are
    # read before logging is configured, so it reached stderr unredacted.
    try:
        Settings(openai_api_key="sk-test", openai_base_url="https://u:hunter2@api.openai.com/v1")
    except ValidationError as exc:
        assert "hunter2" not in str(exc)
    else:
        pytest.fail("a credential-bearing base URL should be refused")


def test_a_clean_base_url_still_passes():
    assert Settings(openai_base_url="https://api.openai.com/v1/").openai_base_url.endswith("/v1")


def test_the_startup_summary_never_carries_the_key():
    # This is what gets logged at startup, so it is the one place a key would leak in
    # plain sight if the summary ever grew a field.
    key = "sk-proj-DistinctiveValueForThisTest"
    summary = Settings(openai_api_key=key).redacted_summary()

    assert key not in json.dumps(summary)
    assert summary["openai_api_key_present"] is True
    assert summary["openai_api_key_length"] == len(key)
