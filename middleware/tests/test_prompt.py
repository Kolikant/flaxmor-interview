from __future__ import annotations

import pytest

from extractor_proxy.prompt import (
    inject_system_prompt,
    is_internal_task_request,
)

PROMPT = "You are a structured data extractor."

# Verbatim openings of the templates open-webui v0.6.5 sends for its own bookkeeping.
TITLE_TEMPLATE = (
    "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history."
)
TAGS_TEMPLATE = (
    "### Task:\nGenerate 1-3 broad tags categorizing the main themes of the chat history"
)
QUERY_TEMPLATE = (
    "### Task:\nAnalyze the chat history to determine the necessity of generating search queries"
)
EMOJI_TEMPLATE = (
    "Your task is to reflect the speaker's likely facial expression through a fitting emoji."
)


def user_turn(text: str = "Invoice 4491, total $82.10") -> dict:
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": text}]}


def test_prompt_is_prepended_as_a_system_message():
    result = inject_system_prompt(user_turn(), PROMPT)

    assert result["messages"][0] == {"role": "system", "content": PROMPT}
    assert result["messages"][1]["content"] == "Invoice 4491, total $82.10"


def test_other_payload_fields_survive_injection():
    payload = user_turn() | {"stream": True, "temperature": 0.2}

    result = inject_system_prompt(payload, PROMPT)

    assert result["stream"] is True
    assert result["temperature"] == 0.2
    assert result["model"] == "gpt-4o-mini"


def test_the_caller_payload_is_not_mutated():
    payload = user_turn()

    inject_system_prompt(payload, PROMPT)

    assert payload["messages"] == [{"role": "user", "content": "Invoice 4491, total $82.10"}]


def test_conversation_history_order_is_preserved():
    payload = {
        "messages": [
            {"role": "user", "content": "a receipt"},
            {"role": "assistant", "content": '{"document_type": "receipt"}'},
            {"role": "user", "content": "what was the total?"},
        ]
    }

    result = inject_system_prompt(payload, PROMPT)

    assert [m["role"] for m in result["messages"]] == ["system", "user", "assistant", "user"]


def test_an_existing_system_message_is_kept_after_ours():
    payload = {
        "messages": [
            {"role": "system", "content": "Always answer in British English."},
            {"role": "user", "content": "a receipt"},
        ]
    }

    result = inject_system_prompt(payload, PROMPT)

    assert result["messages"][0]["content"] == PROMPT
    assert result["messages"][1]["content"] == "Always answer in British English."


def test_injection_is_idempotent():
    once = inject_system_prompt(user_turn(), PROMPT)

    twice = inject_system_prompt(once, PROMPT)

    assert twice["messages"] == once["messages"]


@pytest.mark.parametrize(
    "template",
    [TITLE_TEMPLATE, TAGS_TEMPLATE, QUERY_TEMPLATE, EMOJI_TEMPLATE],
    ids=["title", "tags", "query", "emoji"],
)
def test_open_webui_internal_tasks_are_left_alone(template):
    # These calls expect their own small JSON object back. Injecting the extraction
    # contract here is what turns every chat title into an extraction envelope.
    payload = {"messages": [{"role": "user", "content": template}], "stream": False}

    result = inject_system_prompt(payload, PROMPT)

    assert result is payload
    assert is_internal_task_request(payload["messages"]) is True


def test_a_pasted_document_containing_the_marker_is_still_a_real_turn():
    # The marker must be at the start of a lone user message, so a pasted document
    # that merely mentions "### Task:" is not mistaken for Open WebUI bookkeeping.
    payload = user_turn("Sprint notes\n\n### Task: ship the invoice parser\nOwner: Dana")

    result = inject_system_prompt(payload, PROMPT)

    assert result["messages"][0]["content"] == PROMPT


def test_a_multi_message_conversation_is_never_an_internal_task():
    messages = [
        {"role": "user", "content": TITLE_TEMPLATE},
        {"role": "assistant", "content": '{"title": "Invoice"}'},
    ]

    assert is_internal_task_request(messages) is False


def test_multimodal_content_is_treated_as_a_real_turn():
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "### Task:"}],
        }
    ]

    assert is_internal_task_request(messages) is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"messages": []}, {"messages": "not-a-list"}],
    ids=["absent", "empty", "wrong-type"],
)
def test_malformed_message_lists_pass_straight_through(payload):
    # Validating this here would mean inventing error responses OpenAI itself would
    # phrase differently; the upstream is left to reject it.
    assert inject_system_prompt(payload, PROMPT) is payload
