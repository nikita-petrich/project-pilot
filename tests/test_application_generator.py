"""Tests for the application draft generator (retry, failure surfacing, prompts)."""

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from project_pilot.application.documents import ImageAttachment
from project_pilot.application.generator import (
    ApplicationGenerator,
    DraftResponse,
    load_application_prompt,
)
from project_pilot.application.schemas import ApplicationDraft
from project_pilot.errors import LlmSchemaError


def _draft() -> ApplicationDraft:
    return ApplicationDraft(
        project_title="X-Projekt", subject="Bewerbung: X", body="Text", linkedin_message="Hi"
    )


class _FakeClient:
    def __init__(self, responses: list[DraftResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.images: list[list[str]] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        images: Sequence[ImageAttachment] = (),
    ) -> DraftResponse:
        self.calls.append(user)
        self.images.append([image.name for image in images])
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
        current=ApplicationDraft(
            project_title="Alt-Projekt", subject="Alt", body="Alter Text", linkedin_message="Li"
        ),
        instruction="Bitte kürzer und auf Englisch",
    )
    prompt = client.calls[0]
    assert "Alt" in prompt
    assert "Alter Text" in prompt
    assert "Bitte kürzer und auf Englisch" in prompt


async def test_generate_and_revise_forward_images_to_the_client() -> None:
    image = ImageAttachment(name="listing.png", mime_type="image/png", data=b"\x89PNG")
    response = DraftResponse(draft=_draft(), tokens_in=1, tokens_out=1)
    client = _FakeClient([response, response])
    generator = _generator(client)
    await generator.generate(profile_text="p", listing_text="l", images=[image])
    await generator.revise(
        profile_text="p",
        listing_text="l",
        current=_draft(),
        instruction="Bitte kürzer",
        images=[image],
    )
    assert client.images == [["listing.png"], ["listing.png"]]


def test_load_application_prompt_reads_the_single_file() -> None:
    text = load_application_prompt()
    assert "linkedin_message" in text
    assert "BID-WRITING" in text


def test_prompt_carries_the_signature_template_in_both_languages() -> None:
    """Guards the signature block, incl. the ``-- `` separator's trailing space.

    Editors and formatters that trim trailing whitespace would silently turn the
    RFC 3676 separator into a bare ``--``, so it is asserted literally.
    """
    lines = load_application_prompt().splitlines()

    assert lines.count("-- ") == 2  # one per language, trailing space intact
    assert "--" not in lines

    # The output labels stay language-specific; the <...> placeholders name the keys
    # the prompt looks up in the profile's "Contact & Signature" block.
    for greeting, phone, booking, vat in (
        ("Viele Grüße", "Tel.: <Phone>", "Erstgespräch buchen (30 Min.):", "USt-IdNr.: "),
        ("Best regards", "Phone: <Phone>", "Book an intro call (30 min):", "VAT ID: "),
    ):
        # The greeting sits inside the signature block, right under the separator.
        assert lines[lines.index(greeting) - 1] == "-- "
        for expected in (phone, booking):
            assert expected in lines
        assert any(line.startswith(vat) for line in lines)


def _signature_keys(profile_path: Path) -> set[str]:
    """The ``Label:`` keys offered by a profile's "Contact & Signature" block."""
    _, _, block = profile_path.read_text(encoding="utf-8").partition("## Contact & Signature")
    assert block, f"{profile_path} has no 'Contact & Signature' section"
    return set(re.findall(r"^([A-Za-z][A-Za-z .\-]*?):\s", block, re.MULTILINE))


# The contract between the prompt and every profile file: the prompt fills these by
# looking the label up in the profile, and only prose connects the two ends.
SIGNATURE_KEYS = ("Phone", "Email", "Web", "LinkedIn", "GitHub", "VAT ID")
LANGUAGE_KEYS = ("Location German", "Location English", "CTA German", "CTA English")


@pytest.mark.parametrize(
    "profile", [Path("profile/profile.md"), Path("profile/profile.example.md")]
)
def test_every_profile_defines_the_keys_the_signature_needs(profile: Path) -> None:
    """A renamed label would otherwise drop its signature line without any error."""
    assert set(SIGNATURE_KEYS + LANGUAGE_KEYS) <= _signature_keys(profile)


def test_prompt_looks_up_the_signature_keys_by_name() -> None:
    """The other half of the contract: the prompt must ask for those same keys."""
    prompt = load_application_prompt()
    for key in ("Phone", "Email", "VAT ID", "Location German", "Location English"):
        assert f"<{key}>" in prompt, f"prompt no longer fills <{key}>"
    for key in ("CTA German", "CTA English"):
        assert f"`{key}`" in prompt, f"prompt no longer selects `{key}`"


async def test_generate_hands_the_contact_over_explicitly() -> None:
    client = _FakeClient([DraftResponse(draft=_draft(), tokens_in=1, tokens_out=1)])
    generator = ApplicationGenerator(client, model="m", prompt_template="sys")
    await generator.generate(profile_text="P", listing_text="L", contact_name="Nina Musterfrau")
    assert "## Ansprechpartner\nNina Musterfrau" in client.calls[0]


async def test_generate_without_contact_adds_no_section() -> None:
    client = _FakeClient([DraftResponse(draft=_draft(), tokens_in=1, tokens_out=1)])
    generator = ApplicationGenerator(client, model="m", prompt_template="sys")
    await generator.generate(profile_text="P", listing_text="L")
    assert "Ansprechpartner" not in client.calls[0]


async def test_revise_keeps_the_contact_in_the_prompt() -> None:
    client = _FakeClient([DraftResponse(draft=_draft(), tokens_in=1, tokens_out=1)])
    generator = ApplicationGenerator(client, model="m", prompt_template="sys")
    await generator.revise(
        profile_text="P",
        listing_text="L",
        current=_draft(),
        instruction="kürzer",
        contact_name="Nina Musterfrau",
    )
    assert "## Ansprechpartner\nNina Musterfrau" in client.calls[0]
