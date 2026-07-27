"""Tests for Slack Block Kit builders and the SlackClient wrapper."""

from typing import cast

from project_pilot.application.service import DraftView
from project_pilot.enrichment.schemas import ContactEnrichment, DiscoveryLinks
from project_pilot.models import ApplicationStatus
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.slack import (
    Block,
    PostedMessage,
    SlackClient,
    SlackResponse,
    SlackWebClient,
    format_contact_blocks,
    format_draft_blocks,
    format_match_blocks,
)


def _blocks_of_type(blocks: list[Block], kind: str) -> list[Block]:
    return [b for b in blocks if b.get("type") == kind]


def _action_elements(blocks: list[Block]) -> list[dict[str, object]]:
    actions = _blocks_of_type(blocks, "actions")
    assert actions, "no actions block"
    elements = actions[0]["elements"]
    assert isinstance(elements, list)
    return [cast("dict[str, object]", element) for element in elements]


def _action_ids(blocks: list[Block]) -> list[str]:
    return [str(element["action_id"]) for element in _action_elements(blocks)]


def _section_texts(blocks: list[Block]) -> list[str]:
    texts: list[str] = []
    for block in _blocks_of_type(blocks, "section"):
        text = block["text"]
        assert isinstance(text, dict)
        texts.append(str(text["text"]))
    return texts


def _draft_view(
    *,
    recipient: str | None = "pm@firma.de",
    status: ApplicationStatus = ApplicationStatus.READY,
    body: str = "Sehr geehrte Damen und Herren,\nich passe gut.",
    revision_count: int = 1,
) -> DraftView:
    return DraftView(
        application_id=7,
        title="KI-Projekt",
        url="https://www.freelancermap.de/projekt/x",
        recipient=recipient,
        subject="Bewerbung: KI-Projekt",
        body=body,
        linkedin_message="Hallo, kurzes Interesse!",
        status=status,
        revision_count=revision_count,
    )


def test_match_blocks_have_apply_button_and_facts() -> None:
    message = MatchMessage(
        title="AI Engineer",
        url="https://x/1",
        score=90,
        company="Talent Co",
        location="Remote",
        reasons=["LLM/RAG core"],
        skills=["Python", "RAG"],
    )
    blocks = format_match_blocks(message, listing_id=42)
    header = _blocks_of_type(blocks, "header")[0]["text"]
    assert isinstance(header, dict)
    assert "AI Engineer" in str(header["text"]) and "90/100" in str(header["text"])
    joined = "\n".join(_section_texts(blocks))
    assert "*🏢 Company:* Talent Co" in joined
    assert "LLM/RAG core" in joined
    apply_button = _action_elements(blocks)[0]
    assert apply_button["action_id"] == "apply"
    assert apply_button["value"] == "42"
    assert "open_project" in _action_ids(blocks)
    enrich_button = next(e for e in _action_elements(blocks) if e["action_id"] == "enrich")
    assert enrich_button["value"] == "42"  # enrich carries the listing id


def test_match_blocks_show_short_description_in_full() -> None:
    message = MatchMessage(title="t", url="https://x", score=1, description="Kurze Beschreibung.")
    joined = "\n".join(_section_texts(format_match_blocks(message, listing_id=1)))
    assert "```Kurze Beschreibung.```" in joined
    assert "Gekürzt" not in joined


def test_match_blocks_preview_long_description_and_keep_link() -> None:
    long_text = "wort " * 400  # ~2000 chars, far over the preview budget
    message = MatchMessage(title="t", url="https://x/proj", score=1, description=long_text)
    blocks = format_match_blocks(message, listing_id=1)
    desc = next(t for t in _section_texts(blocks) if "Description" in t)
    assert len(desc) < 900  # compact preview, not the whole description
    assert "Shortened" in desc and "…" in desc
    assert "open_project" in _action_ids(blocks)  # full text stays behind the link


def test_match_blocks_escape_slack_specials() -> None:
    message = MatchMessage(title="A & B <x>", url="https://x", score=50)
    header = format_match_blocks(message, listing_id=1)[0]["text"]
    assert isinstance(header, dict)
    # header is plain_text (no escaping); the facts/section escaping is covered below
    section = MatchMessage(title="t", url="https://x", score=1, company="A & B <x>")
    joined = "\n".join(_section_texts(format_match_blocks(section, listing_id=1)))
    assert "A &amp; B &lt;x&gt;" in joined


def test_draft_blocks_full_email_subject_linkedin_and_buttons() -> None:
    blocks = format_draft_blocks(_draft_view())
    sections = "\n".join(_section_texts(blocks))
    assert "*To:* pm@firma.de" in sections
    assert "*Subject:* `Bewerbung: KI-Projekt`" in sections
    assert "```Sehr geehrte Damen und Herren,\nich passe gut.```" in sections
    assert "```Hallo, kurzes Interesse!```" in sections
    ids = _action_ids(blocks)
    assert ids == ["send", "open_mail", "cancel"]
    mail_button = next(e for e in _action_elements(blocks) if e["action_id"] == "open_mail")
    assert str(mail_button["url"]).startswith("mailto:pm@firma.de?subject=Bewerbung")
    send_button = next(e for e in _action_elements(blocks) if e["action_id"] == "send")
    assert send_button["value"] == "7"


def test_draft_blocks_without_recipient_offer_mail_and_cancel_and_ask_email() -> None:
    blocks = format_draft_blocks(
        _draft_view(recipient=None, status=ApplicationStatus.AWAITING_EMAIL)
    )
    # No Senden (no recipient yet), but the mail-client button is available up front.
    assert _action_ids(blocks) == ["open_mail", "cancel"]
    mail_button = next(e for e in _action_elements(blocks) if e["action_id"] == "open_mail")
    assert str(mail_button["url"]).startswith("mailto:?subject=Bewerbung")
    context = _blocks_of_type(blocks, "context")[0]["elements"]
    assert isinstance(context, list)
    assert "e-mail address" in str(context[0]["text"])


def test_draft_without_recipient_offers_enrich_when_listing_known() -> None:
    view = DraftView(
        application_id=7,
        title="KI-Projekt",
        url="https://x/proj",
        recipient=None,
        subject="Bewerbung: KI-Projekt",
        body="Text",
        linkedin_message="Hi",
        status=ApplicationStatus.AWAITING_EMAIL,
        revision_count=0,
        listing_id=42,
    )
    blocks = format_draft_blocks(view)
    assert _action_ids(blocks) == ["enrich", "open_mail", "cancel"]  # no Send yet; research offered
    enrich = next(e for e in _action_elements(blocks) if e["action_id"] == "enrich")
    assert enrich["value"] == "42"


def _enrichment(*, emails: list[str], phones: list[str]) -> ContactEnrichment:
    return ContactEnrichment(
        company="Muster GmbH",
        person="Max Mustermann",
        website="https://muster-gmbh.de/",
        links=DiscoveryLinks(
            linkedin_company="https://www.linkedin.com/search/results/companies/?keywords=Muster",
            linkedin_people="https://www.linkedin.com/search/results/people/?keywords=Max",
            google_company="https://www.google.com/search?q=Muster",
            google_contact="https://www.google.com/search?q=Muster+Impressum",
        ),
        linkedin_message="Hallo Max, ich würde mich gerne vernetzen. Beste Grüße!",
        emails=emails,
        phones=phones,
        persons=["Max Mustermann"],
        sources=["https://muster-gmbh.de/impressum"],
    )


def test_contact_blocks_render_emails_phones_message_and_links() -> None:
    blocks = format_contact_blocks(
        _enrichment(emails=["bewerbung@muster-gmbh.de"], phones=["+49 30 1234567"])
    )
    joined = "\n".join(_section_texts(blocks))
    assert "bewerbung@muster-gmbh.de" in joined
    assert "+49 30 1234567" in joined
    assert "Muster GmbH" in joined
    assert "Hallo Max," in joined  # the copyable LinkedIn connection message
    ids = _action_ids(blocks)
    assert ids == ["open_li_company", "open_li_people", "open_google"]


def test_contact_blocks_note_when_nothing_found() -> None:
    joined = "\n".join(_section_texts(format_contact_blocks(_enrichment(emails=[], phones=[]))))
    assert "No direct e-mail" in joined  # falls back to the links below


def test_draft_blocks_split_long_email_without_truncation() -> None:
    body = "\n".join(f"Zeile {i}: " + "wort " * 40 for i in range(300))
    blocks = format_draft_blocks(_draft_view(body=body))
    for text in _section_texts(blocks):
        assert len(text) <= 3000  # Slack's per-section limit
    email_sections = [t for t in _section_texts(blocks) if "```" in t and "LinkedIn" not in t]
    assert len(email_sections) >= 2  # split across several sections
    assert all("…" not in t for t in email_sections)  # nothing cut off
    joined = "".join(email_sections)
    assert "Zeile 0:" in joined and "Zeile 299:" in joined


class _Resp:
    """Minimal ``SlackResponse`` stand-in wrapping a plain dict."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def get(self, key: str, default: object = None, /) -> object:
        return self._data.get(key, default)


class _FakeWeb:
    def __init__(self, *, ok: bool = True, error: str | None = None, boom: bool = False) -> None:
        self.ok = ok
        self.error = error
        self.boom = boom
        self.calls: list[dict[str, object]] = []

    async def chat_postMessage(  # noqa: N802 - mirrors slack_sdk's method name
        self,
        *,
        channel: str,
        text: str,
        blocks: list[Block] | None = None,
        thread_ts: str | None = None,
        unfurl_links: bool = True,
        unfurl_media: bool = True,
    ) -> SlackResponse:
        self.calls.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts, "unfurl_links": unfurl_links}
        )
        if self.boom:
            raise RuntimeError("network down")
        return _Resp({"ok": self.ok, "ts": "1700.42", "channel": channel, "error": self.error})

    async def chat_update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[Block] | None = None,
    ) -> SlackResponse:
        return _Resp({"ok": True, "ts": ts, "channel": channel})


def _client(web: _FakeWeb) -> SlackClient:
    return SlackClient(channel="C123", web_client=cast("SlackWebClient", web))


async def test_post_blocks_returns_channel_and_ts() -> None:
    web = _FakeWeb()
    posted = await _client(web).post_blocks([], "fallback")
    assert posted == PostedMessage(channel="C123", ts="1700.42")
    assert posted.ref == "C123:1700.42"
    assert web.calls[0]["channel"] == "C123"


async def test_post_text_in_thread_passes_thread_ts() -> None:
    web = _FakeWeb()
    await _client(web).post_text("hi", thread_ts="1700.42")
    assert web.calls[0]["thread_ts"] == "1700.42"


async def test_post_returns_none_on_api_error() -> None:
    assert await _client(_FakeWeb(ok=False, error="channel_not_found")).post_blocks([], "x") is None


async def test_post_returns_none_on_transport_error() -> None:
    assert await _client(_FakeWeb(boom=True)).post_text("x") is None
