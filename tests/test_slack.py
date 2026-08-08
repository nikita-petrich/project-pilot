"""Tests for Slack Block Kit builders and the SlackClient wrapper."""

from dataclasses import replace
from typing import cast

from project_pilot.application.service import DraftView
from project_pilot.enrichment.schemas import ContactEnrichment, DiscoveryLinks
from project_pilot.evaluation.check import CheckResult
from project_pilot.models import ApplicationStatus, EvaluationStage, Verdict
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.slack import (
    Block,
    PostedMessage,
    SlackClient,
    SlackNotifier,
    SlackResponse,
    SlackWebClient,
    check_fallback_text,
    format_check_blocks,
    format_contact_blocks,
    format_draft_blocks,
    format_match_blocks,
    format_match_detail_blocks,
    match_detail_fallback_text,
    match_fallback_text,
    sent_confirmation_blocks,
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


def _code_texts(blocks: list[Block]) -> list[str]:
    """The native code blocks' contents (rich_text_preformatted = corner copy button)."""
    texts: list[str] = []
    for block in _blocks_of_type(blocks, "rich_text"):
        elements = block["elements"]
        assert isinstance(elements, list)
        for element in elements:
            assert element["type"] == "rich_text_preformatted"
            texts.append("".join(str(part["text"]) for part in element["elements"]))
    return texts


def _context_texts(blocks: list[Block]) -> list[str]:
    texts: list[str] = []
    for block in _blocks_of_type(blocks, "context"):
        elements = block["elements"]
        assert isinstance(elements, list)
        texts.extend(str(cast("dict[str, object]", element)["text"]) for element in elements)
    return texts


def _draft_view(
    *,
    recipient: str | None = "pm@firma.de",
    status: ApplicationStatus = ApplicationStatus.READY,
    body: str = "Sehr geehrte Damen und Herren,\nich passe gut.",
    revision_count: int = 1,
    contact_name: str | None = None,
    company: str | None = None,
) -> DraftView:
    return DraftView(
        application_id=7,
        title="KI-Projekt",
        url="https://www.freelancermap.de/projekt/x",
        contact_name=contact_name,
        recipient=recipient,
        subject="Bewerbung: KI-Projekt",
        body=body,
        linkedin_message="Hallo, kurzes Interesse!",
        status=status,
        revision_count=revision_count,
        company=company,
    )


_MATCH = MatchMessage(
    title="AI Engineer",
    url="https://x/1",
    score=90,
    company="Talent Co",
    contact_name="Anna Meier",
    is_endcustomer=True,
    location="Remote",
    workload_label="100%",
    duration_label="6 mo",
    start="ASAP",
    posted_ago="12 min ago",
    reasons=["LLM/RAG core", "Python-heavy", "Remote"],
    skills=["Python", "RAG"],
    missing_requirements=["Kubernetes"],
)


def test_match_card_is_compact_and_carries_the_buttons() -> None:
    blocks = format_match_blocks(_MATCH, listing_id=42)
    header = _blocks_of_type(blocks, "header")[0]["text"]
    assert isinstance(header, dict)
    assert "AI Engineer" in str(header["text"]) and "90/100" in str(header["text"])
    joined = "\n".join(_section_texts(blocks))
    assert "🏢 *Talent Co*" in joined and "Direct client" in joined
    assert "📍 Remote" in joined and "⏳ 6 mo" in joined and "🕒 12 min ago" in joined
    assert "LLM/RAG core" in joined  # the top reasons decide the yes/no at a glance
    assert "Remote" in joined
    # everything long stays in the thread: no skills, no gaps, no description
    assert "🛠 Skills" not in joined and "⚠️ Gaps" not in joined
    assert not _code_texts(blocks)
    assert any("thread" in text for text in _context_texts(blocks))
    apply_button = _action_elements(blocks)[0]
    assert apply_button["action_id"] == "apply"
    assert apply_button["value"] == "42"
    assert "open_project" in _action_ids(blocks)
    enrich_button = next(e for e in _action_elements(blocks) if e["action_id"] == "enrich")
    assert enrich_button["value"] == "42"  # enrich carries the listing id


def test_match_card_names_only_the_top_reasons() -> None:
    joined = "\n".join(_section_texts(format_match_blocks(_MATCH, listing_id=1)))
    assert "LLM/RAG core, Python-heavy" in joined and "Python-heavy, Remote" not in joined


def test_match_card_drops_lines_it_has_no_data_for() -> None:
    message = MatchMessage(title="t", url="https://x", score=5)
    blocks = format_match_blocks(message, listing_id=1)
    assert not _section_texts(blocks)  # header, hint and buttons only


def test_match_detail_blocks_carry_the_full_listing() -> None:
    blocks = format_match_detail_blocks(_MATCH)
    joined = "\n".join(_section_texts(blocks))
    assert "*🏢 Company:* Talent Co" in joined
    assert "*👤 Contact:* Anna Meier" in joined
    assert "*🛠 Skills:* Python, RAG" in joined
    assert "*⚠️ Gaps:* Kubernetes" in joined
    assert not _blocks_of_type(blocks, "header")  # the card above already names it
    assert not _blocks_of_type(blocks, "actions")  # buttons stay on the card


def test_match_detail_blocks_show_the_whole_description() -> None:
    long_text = "\n".join(f"Zeile {i}: " + "wort " * 30 for i in range(60))
    blocks = format_match_detail_blocks(replace(_MATCH, description=long_text))
    codes = _code_texts(blocks)
    joined = "".join(codes)
    assert all(len(text) <= 3000 for text in codes)  # stay inside Slack's limits
    assert "Zeile 0:" in joined and "Zeile 59:" in joined  # nothing cut off
    assert "Shortened" not in "".join(_context_texts(blocks))


def test_check_blocks_show_the_whole_description_without_a_project_link() -> None:
    """Pasted text has no "View project" button, so the description is never cut."""
    long_text = "\n".join(f"Zeile {i}: " + "wort " * 30 for i in range(60))
    result = CheckResult(
        title="Senior Data Engineer",
        stage=EvaluationStage.LLM,
        verdict=Verdict.MATCH,
        passed=True,
        score=88,
        threshold=60,
        reason={},
        message=MatchMessage(title="Senior Data Engineer", url="", score=88, description=long_text),
        is_llm_error=False,
    )
    blocks = format_check_blocks(result)
    codes = _code_texts(blocks)
    joined = "".join(codes)
    assert all(len(text) <= 3000 for text in codes)  # stay inside Slack's limits
    assert "Shortened" not in "".join(_context_texts(blocks))  # no dead-end pointer
    assert "Zeile 0:" in joined and "Zeile 59:" in joined  # every line survived


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
    codes = _code_texts(blocks)
    assert "Sehr geehrte Damen und Herren,\nich passe gut." in codes
    assert "Hallo, kurzes Interesse!" in codes
    ids = _action_ids(blocks)
    assert ids == ["send", "cancel"]
    # The mail-client action is a mrkdwn mailto link (Slack buttons drop mailto URLs)
    # and carries the letter itself, not just the subject.
    mail_link = (
        "<mailto:pm@firma.de?subject=Bewerbung%3A%20KI-Projekt"
        "&body=Sehr%20geehrte%20Damen%20und%20Herren%2C%0Aich%20passe%20gut."
        "|📧 Open in mail client>"
    )
    assert mail_link in sections
    send_button = next(e for e in _action_elements(blocks) if e["action_id"] == "send")
    assert send_button["value"] == "7"


def test_draft_blocks_mailto_body_is_bounded_and_says_so() -> None:
    """A mailto must fit one Slack section, so a long letter is cut with a note."""
    blocks = format_draft_blocks(_draft_view(body="\n".join(f"Zeile {i}" for i in range(600))))
    mail_section = next(t for t in _section_texts(blocks) if "mailto:" in t)
    assert len(mail_section) <= 2900
    assert "Zeile%200" in mail_section  # the letter starts in the mail client
    contexts = " ".join(
        str(cast("list[dict[str, object]]", block["elements"])[0]["text"])
        for block in _blocks_of_type(blocks, "context")
    )
    assert "beginning of the letter" in contexts and "Send" in contexts


def test_draft_blocks_without_recipient_offer_mail_and_cancel_and_ask_email() -> None:
    blocks = format_draft_blocks(
        _draft_view(recipient=None, status=ApplicationStatus.AWAITING_EMAIL)
    )
    # Send stays visible without a recipient (pressing it answers with the hint),
    # and the mail-client link is available up front.
    assert _action_ids(blocks) == ["send", "cancel"]
    sections = "\n".join(_section_texts(blocks))
    assert "<mailto:?subject=Bewerbung%3A%20KI-Projekt&body=" in sections
    contexts = " ".join(
        str(cast("list[dict[str, object]]", block["elements"])[0]["text"])
        for block in _blocks_of_type(blocks, "context")
    )
    assert "e-mail address" in contexts


def _all_action_elements(blocks: list[Block]) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    for actions in _blocks_of_type(blocks, "actions"):
        block_elements = actions["elements"]
        assert isinstance(block_elements, list)
        elements.extend(cast("dict[str, object]", element) for element in block_elements)
    return elements


def test_draft_without_recipient_offers_enrich_when_listing_known() -> None:
    view = DraftView(
        application_id=7,
        title="KI-Projekt",
        url="https://x/proj",
        contact_name=None,
        recipient=None,
        subject="Bewerbung: KI-Projekt",
        body="Text",
        linkedin_message="Hi",
        status=ApplicationStatus.AWAITING_EMAIL,
        revision_count=0,
        listing_id=42,
    )
    blocks = format_draft_blocks(view)
    # Contact research is offered next to Send (the mail-client action is a link).
    assert _action_ids(blocks) == ["send", "enrich", "cancel"]
    enrich = next(e for e in _action_elements(blocks) if e["action_id"] == "enrich")
    assert enrich["value"] == "42"


def test_draft_blocks_name_the_cvs_a_send_will_attach() -> None:
    view = DraftView(
        application_id=7,
        title="KI-Projekt",
        url=None,
        contact_name=None,
        recipient="pm@firma.de",
        subject="Bewerbung",
        body="Text",
        linkedin_message="Hi",
        status=ApplicationStatus.READY,
        revision_count=0,
        attachments=("CV-DE.pdf", "CV-DE-Word.docx", "CV-EN.pdf"),
        missing_attachments=("CV-EN-Word.docx",),
    )
    contexts = " ".join(
        str(cast("list[dict[str, object]]", block["elements"])[0]["text"])
        for block in _blocks_of_type(format_draft_blocks(view), "context")
    )
    assert "CV-DE.pdf, CV-DE-Word.docx, CV-EN.pdf" in contexts
    assert "Missing in `cv/`: CV-EN-Word.docx" in contexts


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
    codes = "\n".join(_code_texts(blocks))
    assert "bewerbung@muster-gmbh.de" in codes
    assert "+49 30 1234567" in codes
    assert "Muster GmbH" in joined
    assert "Hallo Max," in codes  # the copyable LinkedIn connection message
    assert _action_ids(blocks) == ["open_li_company", "open_li_people", "open_google"]


def test_contact_blocks_note_when_nothing_found() -> None:
    joined = "\n".join(_section_texts(format_contact_blocks(_enrichment(emails=[], phones=[]))))
    assert "No direct e-mail" in joined  # falls back to the links below


def test_draft_blocks_offer_linkedin_search_for_contact() -> None:
    blocks = format_draft_blocks(_draft_view(contact_name="Anna Kleinen"))
    button = next(e for e in _all_action_elements(blocks) if e["action_id"] == "linkedin_search")
    assert button["url"] == (
        "https://www.linkedin.com/search/results/people/?keywords=Anna%20Kleinen"
    )
    with_company = format_draft_blocks(
        _draft_view(contact_name="Anna Kleinen", company="Muster GmbH")
    )
    button = next(
        e for e in _all_action_elements(with_company) if e["action_id"] == "linkedin_search"
    )
    # person AND company narrows the people search to the right hit
    assert button["url"] == (
        "https://www.linkedin.com/search/results/people/"
        "?keywords=Anna%20Kleinen%20AND%20Muster%20GmbH"
    )
    text = button["text"]
    assert isinstance(text, dict)
    assert "Anna Kleinen" in str(text["text"])
    # the search button rides directly under the LinkedIn section, before the review row
    assert _action_ids(blocks) == ["linkedin_search"]
    assert len(_blocks_of_type(blocks, "actions")) == 2


def test_draft_blocks_keep_linkedin_search_after_send() -> None:
    blocks = format_draft_blocks(
        _draft_view(contact_name="Anna Kleinen", status=ApplicationStatus.SENT)
    )
    ids = [str(e["action_id"]) for e in _all_action_elements(blocks)]
    assert ids == ["linkedin_search"]  # review buttons gone, the search stays


def test_draft_blocks_without_contact_have_no_linkedin_search() -> None:
    ids = [str(e["action_id"]) for e in _all_action_elements(format_draft_blocks(_draft_view()))]
    assert "linkedin_search" not in ids


def test_sent_confirmation_carries_linkedin_text_and_search_button() -> None:
    blocks = sent_confirmation_blocks(
        _draft_view(contact_name="Anna Kleinen", status=ApplicationStatus.SENT)
    )
    joined = "\n".join(_section_texts(blocks))
    assert "sent to *pm@firma.de*" in joined
    assert "Hallo, kurzes Interesse!" in _code_texts(blocks)
    button = next(e for e in _all_action_elements(blocks) if e["action_id"] == "linkedin_search")
    assert "keywords=Anna%20Kleinen" in str(button["url"])


def test_sent_confirmation_without_contact_has_no_button() -> None:
    blocks = sent_confirmation_blocks(_draft_view(status=ApplicationStatus.SENT))
    assert _blocks_of_type(blocks, "actions") == []


def test_draft_blocks_split_long_email_without_truncation() -> None:
    body = "\n".join(f"Zeile {i}: " + "wort " * 40 for i in range(300))
    blocks = format_draft_blocks(_draft_view(body=body))
    codes = [t for t in _code_texts(blocks) if "Interesse" not in t]  # the e-mail parts
    assert len(codes) >= 2  # split across several blocks
    assert all(len(t) <= 3000 for t in codes)
    assert all("…" not in t for t in codes)  # nothing cut off
    joined = "".join(codes)
    assert "Zeile 0:" in joined and "Zeile 299:" in joined


def test_draft_blocks_with_body_in_file_point_at_the_text_file() -> None:
    """When the e-mail went out as a .txt file, the message only points there."""
    view = _draft_view(body="Sehr geehrte Damen und Herren,\nich passe gut.")
    blocks = format_draft_blocks(view, body_in_file=True)
    assert "Sehr geehrte Damen und Herren,\nich passe gut." not in _code_texts(blocks)
    assert any("text file below" in text for text in _context_texts(blocks))
    # the LinkedIn message still renders inline, and the buttons are unchanged
    assert "Hallo, kurzes Interesse!" in _code_texts(blocks)
    assert _action_ids(blocks) == ["send", "cancel"]


def _check_result(
    *,
    passed: bool = True,
    stage: EvaluationStage = EvaluationStage.LLM,
    verdict: Verdict = Verdict.MATCH,
    score: int | None = 80,
    reason: dict[str, object] | None = None,
    message: MatchMessage | None = None,
    is_llm_error: bool = False,
) -> CheckResult:
    return CheckResult(
        title="KI-Projekt",
        stage=stage,
        verdict=verdict,
        passed=passed,
        score=score,
        threshold=60,
        reason=reason or {},
        message=message,
        is_llm_error=is_llm_error,
    )


def test_check_blocks_pass_reuse_match_body_with_custom_apply_button() -> None:
    message = MatchMessage(
        title="KI-Projekt", url="https://x/1", score=80, reasons=["RAG Erfahrung"]
    )
    blocks = format_check_blocks(
        _check_result(message=message), apply_action="apply", apply_value="42"
    )
    header = next(b for b in blocks if b.get("type") == "header")["text"]
    assert isinstance(header, dict)
    assert "KI-Projekt" in str(header["text"]) and "80/100" in str(header["text"])
    apply_button = _action_elements(blocks)[0]
    assert apply_button["action_id"] == "apply" and apply_button["value"] == "42"
    assert "open_project" in _action_ids(blocks)
    context = next(b for b in blocks if b.get("type") == "context")["elements"]
    assert isinstance(context, list)
    assert "✅ match" in str(context[0]["text"]) and "threshold 60" in str(context[0]["text"])


def test_check_blocks_pass_without_url_or_apply_ref_has_no_buttons() -> None:
    message = MatchMessage(title="Text-Check", url="", score=70)
    blocks = format_check_blocks(_check_result(message=message))
    assert not [b for b in blocks if b.get("type") == "actions"]


def test_check_blocks_hard_rule_shows_matched_term() -> None:
    result = _check_result(
        passed=False,
        stage=EvaluationStage.HARD_RULE,
        verdict=Verdict.NO_MATCH,
        score=None,
        reason={"rule": "blacklist", "matched_term": "sap"},
    )
    blocks = format_check_blocks(result)
    joined = "\n".join(_section_texts(blocks))
    assert "blacklist" in joined and "`sap`" in joined
    assert "No match" in str(next(b for b in blocks if b.get("type") == "header")["text"])


def test_check_blocks_match_below_threshold_says_so() -> None:
    result = _check_result(
        passed=False,
        score=55,
        reason={"reasons": ["passt teils"], "missing_requirements": ["kubernetes"]},
    )
    joined = "\n".join(_section_texts(format_check_blocks(result)))
    assert "55/100" in joined and "below your threshold" in joined
    assert "passt teils" in joined and "kubernetes" in joined


def test_check_blocks_llm_error_warns() -> None:
    result = _check_result(passed=False, verdict=Verdict.NO_MATCH, score=0, is_llm_error=True)
    joined = "\n".join(_section_texts(format_check_blocks(result)))
    assert "no verdict" in joined


def test_check_fallback_text_states_verdict() -> None:
    assert "match" in check_fallback_text(_check_result(message=None))
    assert "no match" in check_fallback_text(_check_result(passed=False))


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

    async def files_upload_v2(
        self,
        *,
        channel: str,
        content: str,
        filename: str,
        title: str | None = None,
        thread_ts: str | None = None,
    ) -> SlackResponse:
        self.calls.append(
            {"channel": channel, "filename": filename, "title": title, "thread_ts": thread_ts}
        )
        if self.boom:
            raise RuntimeError("network down")
        return _Resp({"ok": self.ok, "error": self.error})


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


async def test_upload_text_sends_the_file_into_the_thread() -> None:
    web = _FakeWeb()
    ok = await _client(web).upload_text(
        content="Sehr geehrte...", filename="E-Mail.txt", title="Bewerbung", thread_ts="1700.42"
    )
    assert ok is True
    call = web.calls[0]
    assert call["channel"] == "C123"
    assert call["filename"] == "E-Mail.txt"
    assert call["thread_ts"] == "1700.42"


async def test_send_match_posts_the_card_then_the_detail_into_its_thread() -> None:
    web = _FakeWeb()
    sent = await SlackNotifier(_client(web)).send_match(_MATCH, listing_id=42)
    assert sent is True
    card, detail = web.calls
    assert card["text"] == match_fallback_text(_MATCH)
    assert card["thread_ts"] is None
    assert detail["text"] == match_detail_fallback_text(_MATCH)
    assert detail["thread_ts"] == "1700.42"  # the card's ts = the thread it opened


async def test_send_match_reports_failure_and_skips_the_detail() -> None:
    web = _FakeWeb(ok=False, error="channel_not_found")
    assert await SlackNotifier(_client(web)).send_match(_MATCH, listing_id=42) is False
    assert len(web.calls) == 1  # no thread to post the detail into


async def test_upload_text_reports_failure_instead_of_raising() -> None:
    assert (
        await _client(_FakeWeb(ok=False, error="missing_scope")).upload_text(
            content="x", filename="f.txt", title="t"
        )
        is False
    )
    assert (
        await _client(_FakeWeb(boom=True)).upload_text(content="x", filename="f.txt", title="t")
        is False
    )
