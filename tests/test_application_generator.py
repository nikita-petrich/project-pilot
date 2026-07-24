"""Tests for the application draft generator (retry, failure surfacing, prompts)."""

import pytest

from project_pilot.application.generator import (
    ApplicationGenerator,
    DraftResponse,
    load_application_prompt,
)
from project_pilot.application.schemas import ApplicationDraft
from project_pilot.errors import LlmSchemaError


def _draft() -> ApplicationDraft:
    return ApplicationDraft(subject="Bewerbung: X", body="Text", linkedin_message="Hi")


class _FakeClient:
    def __init__(self, responses: list[DraftResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, *, model: str, system: str, user: str) -> DraftResponse:
        self.calls.append(user)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _generator(client: _FakeClient) -> ApplicationGenerator:
    return ApplicationGenerator(client, model="m", prompt_template="sys")


async def test_generate_returns_draft_and_metadata() -> None:
    client = _FakeClient([DraftResponse(draft=_draft(), tokens_in=10, tokens_out=5)])
    generated = await _generator(client).generate(
        profile_text="profile text", listing_text="listing text"
    )
    assert generated.draft.subject == "Bewerbung: X"
    assert generated.prompt_version == "application"
    assert generated.tokens_in == 10
    assert generated.tokens_out == 5
    prompt = client.calls[0]
    assert "profile text" in prompt
    assert "listing text" in prompt


async def test_generate_retries_once_then_succeeds() -> None:
    client = _FakeClient(
        [RuntimeError("boom"), DraftResponse(draft=_draft(), tokens_in=None, tokens_out=None)]
    )
    generated = await _generator(client).generate(profile_text="p", listing_text="l")
    assert generated.draft.body == "Text"
    assert len(client.calls) == 2


async def test_generate_raises_after_two_schema_failures() -> None:
    empty = DraftResponse(draft=None, tokens_in=None, tokens_out=None)
    client = _FakeClient([empty, empty])
    with pytest.raises(LlmSchemaError):
        await _generator(client).generate(profile_text="p", listing_text="l")


async def test_revise_prompt_includes_current_draft_and_instruction() -> None:
    client = _FakeClient([DraftResponse(draft=_draft(), tokens_in=1, tokens_out=1)])
    await _generator(client).revise(
        profile_text="p",
        listing_text="l",
        current=ApplicationDraft(subject="Alt", body="Alter Text", linkedin_message="Li"),
        instruction="Bitte kürzer und auf Englisch",
    )
    prompt = client.calls[0]
    assert "Alt" in prompt
    assert "Alter Text" in prompt
    assert "Bitte kürzer und auf Englisch" in prompt


def test_load_application_prompt_reads_the_single_file() -> None:
    text = load_application_prompt()
    assert "linkedin_message" in text
    assert "BID-WRITING" in text
