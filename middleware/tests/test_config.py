from __future__ import annotations

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
