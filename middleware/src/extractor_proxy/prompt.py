"""System prompt injection — the core behaviour of this proxy.

Every chat completion that represents a real user turn gets the extraction system
prompt prepended. Open WebUI's own internal calls are deliberately left alone; see
`is_internal_task_request` for why that detection looks the way it does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("extractor_proxy.prompt")

#: Delimiters around the prompt inside SYSTEM_PROMPT.md. HTML comments are invisible
#: when the document is rendered, so the file stays readable as documentation while
#: remaining unambiguous to parse.
PROMPT_BEGIN_MARKER = "<!-- SYSTEM_PROMPT:BEGIN -->"
PROMPT_END_MARKER = "<!-- SYSTEM_PROMPT:END -->"


class PromptUnavailableError(RuntimeError):
    """Raised when SYSTEM_PROMPT.md is missing, unreadable, or has no prompt block."""


def load_system_prompt(path: Path) -> str:
    """Read the prompt out of SYSTEM_PROMPT.md.

    The document is the single source of truth rather than a Python constant, so the
    artefact reviewed and the bytes sent upstream are the same thing. The cost is a
    startup-time file read and this parser; the benefit is that editing the prompt
    cannot leave the documentation stale.
    """
    try:
        document = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptUnavailableError(f"cannot read prompt document at {path}: {exc}") from exc

    # Both markers are checked the same way — on whether the separator was found, not
    # on whether text follows it — so a document ending exactly at the begin marker
    # reports the honest error rather than a missing-marker one.
    _, begin, after_begin = document.partition(PROMPT_BEGIN_MARKER)
    if not begin:
        raise PromptUnavailableError(f"{path} is missing {PROMPT_BEGIN_MARKER}")

    body, end, _ = after_begin.partition(PROMPT_END_MARKER)
    if not end:
        raise PromptUnavailableError(f"{path} is missing {PROMPT_END_MARKER}")

    prompt = body.strip()
    if not prompt:
        raise PromptUnavailableError(f"{path} contains an empty prompt block")

    return prompt

#: Matching on template text is not the detection I would have picked. Open WebUI
#: labels these calls in the request body as `metadata.task`, but its OpenAI router
#: does `metadata = payload.pop("metadata", None)` before forwarding, so that label
#: never survives to a proxy sitting where this one sits. The prompt text is the only
#: signal left at this boundary.
#:
#: The two groups below are all eight `DEFAULT_*_PROMPT_TEMPLATE` values in
#: open-webui v0.6.5, read out of the pinned image rather than transcribed.

#: Five templates open with this header — title, tags, query, autocomplete and image
#: prompt. Every one of them also contains the output marker below, so both are
#: required: a person pasting a to-do list that happens to start "### Task:" would
#: otherwise silently get no extraction at all.
TASK_HEADER_MARKER = "### Task:"
TASK_OUTPUT_MARKER = "### Output:"

#: The remaining three templates have no common header, so each is matched on an
#: opening distinctive enough to stand alone.
DISTINCTIVE_TASK_OPENINGS: tuple[str, ...] = (
    "Your task is to reflect the speaker's likely facial expression",
    "You have been provided with a set of responses from various models",
    "Available Tools:",
)


def is_internal_task_request(messages: list[dict[str, Any]]) -> bool:
    """True when this looks like Open WebUI talking to itself rather than a user turn.

    Open WebUI asks the model to name the chat, tag it, propose search queries and
    pick an emoji. Those calls expect a small JSON object of their own
    (`{"title": ...}`, `{"tags": [...]}`), so forcing the extraction contract onto
    them turns every chat title into an extraction envelope.

    The shape matched is a final user message carrying a template, optionally preceded
    only by system messages. The system-message allowance is not hypothetical: giving
    an Open WebUI workspace model a system prompt makes
    `apply_model_system_prompt_to_body` prepend it to *every* request including these,
    so requiring exactly one message would silently stop recognising task calls the
    moment a user configures a model — and every chat title would become an envelope.

    Matching stays narrow in the other direction too. The `### Task:` header alone is
    not enough, because a person pasting a to-do list that opens with it would get no
    extraction at all; the templates that use that header also carry an output marker,
    so both are required.
    """
    if not messages:
        return False

    *leading, last = messages
    if not all(isinstance(message, dict) for message in messages):
        return False
    if any(message.get("role") != "system" for message in leading):
        return False
    if last.get("role") != "user":
        return False

    content = last.get("content")
    if not isinstance(content, str):
        # Multimodal content arrives as a list of parts. Open WebUI never sends its
        # task templates that way, so anything non-string is a real user turn.
        return False

    stripped = content.lstrip()
    if stripped.startswith(TASK_HEADER_MARKER):
        return TASK_OUTPUT_MARKER in content
    return any(stripped.startswith(opening) for opening in DISTINCTIVE_TASK_OPENINGS)


def inject_system_prompt(payload: dict[str, Any], system_prompt: str) -> dict[str, Any]:
    """Return a copy of `payload` with the extraction prompt prepended to messages.

    A system message that Open WebUI already supplied (from the model's own settings)
    is kept, immediately after ours, rather than being overwritten — dropping a
    user's configuration silently would be worse than the tension it creates. Where
    the two disagree about output shape, the injected prompt states its contract is
    non-negotiable, which is what holds the format in practice.

    Returns the payload untouched when there is nothing to do: an internal Open WebUI
    task, a malformed `messages` field, or a prompt already in place.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        # Not this proxy's job to invent validation errors — pass it through and let
        # OpenAI answer with its own, which is what an OpenAI-compatible client expects.
        logger.warning("prompt.injection.skipped", extra={"reason": "no_messages"})
        return payload

    if not all(isinstance(message, dict) for message in messages):
        # `{"messages": ["hello"]}` is valid JSON and a plausible client mistake.
        # Without this the `.get` calls below raise AttributeError and the caller gets
        # a 500 with a stack trace — the opposite of what this function promises.
        logger.warning("prompt.injection.skipped", extra={"reason": "non_object_message"})
        return payload

    if is_internal_task_request(messages):
        logger.info("prompt.injection.skipped", extra={"reason": "open_webui_task"})
        return payload

    first = messages[0]
    if first.get("role") == "system" and first.get("content") == system_prompt:
        # Idempotent: never stack the same prompt twice on a retried request.
        return payload

    injected = dict(payload)
    injected["messages"] = [{"role": "system", "content": system_prompt}, *messages]
    logger.info(
        "prompt.injection.applied",
        extra={"message_count": len(injected["messages"])},
    )
    return injected
