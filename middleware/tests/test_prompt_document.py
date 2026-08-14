"""Tests for loading the prompt out of SYSTEM_PROMPT.md, and for the document itself.

The repository document is the runtime source of truth, so a few assertions here
guard the contract the prompt promises rather than only the parser around it.
"""

from __future__ import annotations

import json
import re

import pytest

from extractor_proxy.config import discover_system_prompt_path
from extractor_proxy.prompt import (
    PROMPT_BEGIN_MARKER,
    PROMPT_END_MARKER,
    PromptUnavailableError,
    load_system_prompt,
)

ENVELOPE_KEYS = [
    "document_type",
    "confidence",
    "language",
    "summary",
    "fields",
    "uncertain_fields",
    "unextracted",
    "warnings",
]


def write_document(tmp_path, body: str):
    path = tmp_path / "SYSTEM_PROMPT.md"
    path.write_text(
        f"Commentary above.\n{PROMPT_BEGIN_MARKER}\n{body}\n{PROMPT_END_MARKER}\nNotes below.\n",
        encoding="utf-8",
    )
    return path


def test_only_the_delimited_block_is_loaded(tmp_path):
    path = write_document(tmp_path, "You are an extractor.")

    assert load_system_prompt(path) == "You are an extractor."


def test_surrounding_commentary_never_reaches_the_model(tmp_path):
    path = write_document(tmp_path, "You are an extractor.")

    prompt = load_system_prompt(path)

    assert "Commentary above" not in prompt
    assert "Notes below" not in prompt


def test_a_missing_file_is_reported_as_unavailable(tmp_path):
    with pytest.raises(PromptUnavailableError, match="cannot read prompt document"):
        load_system_prompt(tmp_path / "absent.md")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("no markers at all", "missing <!-- SYSTEM_PROMPT:BEGIN -->"),
        (f"{PROMPT_BEGIN_MARKER}\nunterminated", "missing <!-- SYSTEM_PROMPT:END -->"),
        (f"{PROMPT_BEGIN_MARKER}\n   \n{PROMPT_END_MARKER}", "empty prompt block"),
    ],
    ids=["no-begin", "no-end", "empty"],
)
def test_malformed_documents_are_rejected_with_a_reason(tmp_path, document, expected):
    path = tmp_path / "SYSTEM_PROMPT.md"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(PromptUnavailableError, match=re.escape(expected)):
        load_system_prompt(path)


def repository_prompt() -> str:
    path = discover_system_prompt_path()
    if not path.is_file():
        pytest.skip("SYSTEM_PROMPT.md is not present next to the package")
    return load_system_prompt(path)


def test_the_repository_prompt_loads():
    assert len(repository_prompt()) > 500


def test_the_repository_prompt_defines_both_modes():
    prompt = repository_prompt()

    assert "EXTRACTION MODE" in prompt
    assert "ANSWER MODE" in prompt


def test_the_repository_prompt_names_every_envelope_key():
    prompt = repository_prompt()

    for key in ENVELOPE_KEYS:
        assert f'"{key}"' in prompt, f"envelope key {key} is not in the prompt"


def prompt_json_blocks() -> list[dict]:
    prompt = repository_prompt()
    blocks = re.findall(r"```json\n(.*?)```", prompt, flags=re.DOTALL)
    assert blocks, "the prompt should contain at least one json example block"
    # A typo in any example would actively teach the model to emit broken JSON, so
    # every block in the prompt has to parse, not just the worked example.
    return [json.loads(block) for block in blocks]


def test_every_json_example_in_the_prompt_parses():
    assert len(prompt_json_blocks()) >= 2


def test_the_envelope_examples_carry_every_key_in_the_documented_order():
    envelopes = [block for block in prompt_json_blocks() if "document_type" in block]

    # The bare template and the worked example both demonstrate the envelope; if
    # either drifts from the documented key order the contract has two versions.
    assert len(envelopes) == 2
    for envelope in envelopes:
        assert list(envelope) == ENVELOPE_KEYS


def test_the_uncertain_field_example_shows_the_documented_shape():
    entries = [block for block in prompt_json_blocks() if "path" in block]

    assert len(entries) == 1
    assert list(entries[0]) == ["path", "confidence", "reason"]
