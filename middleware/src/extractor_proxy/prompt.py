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

#: Openings of the prompt templates Open WebUI sends for its own bookkeeping calls
#: (chat title, tag, search-query and emoji generation). Taken from
#: `DEFAULT_*_PROMPT_TEMPLATE` in open-webui v0.6.5's config.py.
#:
#: Matching on template text is not the detection I would have picked. Open WebUI
#: labels these calls in the request body as `metadata.task`, but its OpenAI router
#: does `metadata = payload.pop("metadata", None)` before forwarding, so that label
#: never survives to a proxy sitting where this one sits. The prompt text is the only
#: signal left at this boundary.
OPEN_WEBUI_TASK_MARKERS: tuple[str, ...] = (
    "### Task:",
    "Your task is to reflect the speaker's likely facial expression",
)


def is_internal_task_request(messages: list[dict[str, Any]]) -> bool:
    """True when this looks like Open WebUI talking to itself rather than a user turn.

    Open WebUI asks the model to name the chat, tag it, propose search queries and
    pick an emoji. Those calls expect a small JSON object of their own
    (`{"title": ...}`, `{"tags": [...]}`), so forcing the extraction contract onto
    them turns every chat title into an extraction envelope.

    The test is deliberately narrow — a single user message whose content *starts*
    with a known template opening — so that a person pasting a document that happens
    to contain "### Task:" further down is still treated as a real turn.
    """
    if len(messages) != 1:
        return False

    (message,) = messages
    if message.get("role") != "user":
        return False

    content = message.get("content")
    if not isinstance(content, str):
        # Multimodal content arrives as a list of parts. Open WebUI never sends its
        # task templates that way, so anything non-string is a real user turn.
        return False

    stripped = content.lstrip()
    return any(stripped.startswith(marker) for marker in OPEN_WEBUI_TASK_MARKERS)


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
