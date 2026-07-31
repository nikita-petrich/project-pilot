"""Tests for the application service: draft flow, guards, sending (Postgres-backed)."""

from collections.abc import Sequence
from email.message import EmailMessage

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.application.documents import ImageAttachment
from project_pilot.application.generator import ApplicationGenerator, DraftResponse
from project_pilot.application.mailer import SmtpMailer
from project_pilot.application.schemas import ApplicationDraft
from project_pilot.application.service import ApplicationService, extract_email, is_email
from project_pilot.config import CvAttachments, SmtpConfig
from project_pilot.errors import ApplicationStateError, EmailSendError
from project_pilot.evaluation.llm import render_listing_entity
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.normalize import canonicalize_url, compute_url_hash
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import Application, ApplicationStatus, Listing, PostedPrecision
from project_pilot.models import RemoteStatus as Remote
from project_pilot.profile_loader import Profile, ProfileConstraints


def test_is_email_accepts_only_bare_addresses() -> None:
    assert is_email(" jobs@firma.de ")
    assert not is_email("schreib an jobs@firma.de bitte")
    assert not is_email("kein-at-zeichen")


def test_extract_email_finds_first_plausible_address() -> None:
    text = "Kontakt: noreply@portal.de oder direkt bewerbung@firma-mueller.de senden."
    assert extract_email(text) == "bewerbung@firma-mueller.de"


def test_extract_email_skips_platform_addresses() -> None:
    assert extract_email("info@freelancermap.de") is None
    assert extract_email("no-reply@agentur.de") is None
    assert extract_email("gar keine adresse") is None


class _FakeClient:
    def __init__(self, responses: list[DraftResponse]) -> None:
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
        return self.responses.pop(0)


class _FakeSend:
    def __init__(self, *, err: Exception | None = None) -> None:
        self.err = err
        self.sent: list[EmailMessage] = []

    async def __call__(
        self,
        message: EmailMessage,
        /,
        *,
        hostname: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        start_tls: bool,
        timeout: float,  # noqa: ASYNC109 - mirrors aiosmtplib.send's keyword
    ) -> object:
        if self.err is not None:
            raise self.err
        self.sent.append(message)
        return {}


def _draft(subject: str = "Bewerbung: KI-Projekt", title: str = "KI-Projekt") -> ApplicationDraft:
    return ApplicationDraft(
        project_title=title,
        subject=subject,
        body="Sehr geehrte Damen",
        linkedin_message="Hi!",
    )


def _generator(drafts: list[ApplicationDraft]) -> tuple[ApplicationGenerator, _FakeClient]:
    client = _FakeClient([DraftResponse(draft=d, tokens_in=7, tokens_out=3) for d in drafts])
    return ApplicationGenerator(client, model="m", prompt_template="sys"), client


def _profile() -> Profile:
    return Profile(
        text="Senior AI engineer, remote.",
        constraints=ProfileConstraints(blacklist=[], must_have=[], languages=["de", "en"]),
        profile_hash="hash123",
    )


def _mailer(send: _FakeSend) -> SmtpMailer:
    config = SmtpConfig(
        host="mail.example.com",
        port=587,
        username="u",
        password="p",
        sender="nik@example.com",
        use_starttls=True,
    )
    return SmtpMailer(config, send_fn=send)


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    drafts: list[ApplicationDraft] | None = None,
    *,
    mailer: SmtpMailer | None = None,
    cv_attachments: CvAttachments | None = None,
) -> ApplicationService:
    generator, _ = _generator(drafts if drafts is not None else [_draft(), _draft()])
    return ApplicationService(
        session_factory=session_factory,
        generator=generator,
        profile=_profile(),
        mailer=mailer,
        cv_attachments=cv_attachments,
    )


def _listing(
    description: str = "Projektbeschreibung",
    *,
    raw: dict[str, object] | None = None,
    location: str | None = None,
) -> Listing:
    url = "https://www.freelancermap.de/projekt/ki-projekt"
    canonical = canonicalize_url(url, BASE_URL)
    return Listing(
        source="freelancermap",
        external_url=canonical,
        url_hash=compute_url_hash(canonical),
        title="KI-Projekt",
        description=description,
        skills=["Python"],
        raw=raw or {},
        location=location,
    )


async def _store(session_factory: async_sessionmaker[AsyncSession], listing: Listing) -> int:
    async with session_factory() as session:
        session.add(listing)
        await session.commit()
        return listing.id


async def _load_application(
    session_factory: async_sessionmaker[AsyncSession], application_id: int
) -> Application:
    async with session_factory() as session:
        row = await session.get(Application, application_id)
        assert row is not None
        return row


async def test_draft_for_listing_extracts_recipient_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("Bewerbung an jobs@firma.de senden bitte."))
    view = await _service(session_factory).draft_for_listing(listing_id)
    assert view.recipient == "jobs@firma.de"
    assert view.status is ApplicationStatus.READY
    assert view.subject == "Bewerbung: KI-Projekt"
    stored = await _load_application(session_factory, view.application_id)
    assert stored.listing_id == listing_id
    assert stored.profile_hash == "hash123"
    assert stored.tokens_in == 7


async def test_draft_for_listing_finds_email_in_raw_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = _listing(raw={"contact": {"email": "pm@endkunde.de"}})
    listing_id = await _store(session_factory, listing)
    view = await _service(session_factory).draft_for_listing(listing_id)
    assert view.recipient == "pm@endkunde.de"


async def test_draft_for_listing_resolves_and_persists_contact_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = _listing(raw={"company": "Firma GmbH", "firstName": "Anna", "lastName": "Kleinen"})
    listing_id = await _store(session_factory, listing)
    view = await _service(session_factory).draft_for_listing(listing_id)
    assert view.contact_name == "Anna Kleinen"
    stored = await _load_application(session_factory, view.application_id)
    assert stored.contact_name == "Anna Kleinen"


async def test_draft_for_listing_agency_contact_falls_back_to_description(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = _listing(
        "Ihr Ansprechpartner: Max Mustermann bei uns",
        raw={"company": "Hays AG", "firstName": "Hays", "lastName": "AG"},
    )
    listing_id = await _store(session_factory, listing)
    view = await _service(session_factory).draft_for_listing(listing_id)
    assert view.contact_name == "Max Mustermann"


async def test_draft_from_text_extracts_contact_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _service(session_factory).draft_from_text(
        "Python Projekt\nAnsprechpartner: Max Mustermann\nkontakt@kanzlei.de"
    )
    assert view.contact_name == "Max Mustermann"
    plain = await _service(session_factory).draft_from_text("Projekt ohne Kontaktangabe")
    assert plain.contact_name is None


async def test_draft_for_listing_without_email_awaits_recipient(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    view = await _service(session_factory).draft_for_listing(listing_id)
    assert view.recipient is None
    assert view.status is ApplicationStatus.AWAITING_EMAIL


async def test_draft_for_unknown_listing_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ApplicationStateError):
        await _service(session_factory).draft_for_listing(99999)


async def test_draft_from_text_uses_the_listings_own_headline_as_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view = await _service(session_factory).draft_from_text(
        "Python Backend für LegalTech\nLangtext folgt: kontakt@kanzlei.de"
    )
    assert view.title == "Python Backend für LegalTech"
    assert view.recipient == "kontakt@kanzlei.de"
    assert view.url is None


async def test_draft_from_text_without_a_headline_takes_the_generated_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A recruiter mail opens with "Hallo," — the model's project_title names it."""
    service = _service(session_factory, drafts=[_draft(title="Senior Go Developer")])
    view = await service.draft_from_text("Hallo,\n\nwir haben ein spannendes Projekt für Sie.")
    assert view.title == "Senior Go Developer"


async def test_draft_from_text_falls_back_to_the_first_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Neither a headline nor a generated title: the old first-line behaviour."""
    service = _service(session_factory, drafts=[_draft(title="")])
    view = await service.draft_from_text("Hallo,\n\nwir haben ein spannendes Projekt für Sie.")
    assert view.title == "Hallo,"


async def test_draft_from_images_forwards_them_and_records_a_marker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    generator, client = _generator([_draft()])
    service = ApplicationService(
        session_factory=session_factory, generator=generator, profile=_profile(), mailer=None
    )
    image = ImageAttachment(name="listing.png", mime_type="image/png", data=b"\x89PNG")
    view = await service.draft_from_text("", images=[image])
    # A screenshot has no headline to read, so the model names the project.
    assert view.title == "KI-Projekt"
    assert client.images == [["listing.png"]]
    stored = await _load_application(session_factory, view.application_id)
    assert "[Project listing attached as image: listing.png]" in stored.listing_text


async def test_revise_forwards_images_to_the_generator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    generator, client = _generator([_draft(), _draft("Bewerbung: überarbeitet")])
    service = ApplicationService(
        session_factory=session_factory, generator=generator, profile=_profile(), mailer=None
    )
    view = await service.draft_for_listing(listing_id)
    image = ImageAttachment(name="feedback.jpg", mime_type="image/jpeg", data=b"\xff\xd8")
    revised = await service.revise(view.application_id, "Siehe Screenshot", images=[image])
    assert revised.subject == "Bewerbung: überarbeitet"
    assert client.images[-1] == ["feedback.jpg"]


async def test_draft_from_parsed_links_stored_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = _listing()
    listing_id = await _store(session_factory, listing)
    parsed = ParsedListing(
        source="freelancermap",
        external_url=listing.external_url,
        url_hash=listing.url_hash,
        title="KI-Projekt",
        description="Direkt an hr@firma.de",
        skills=[],
        start_date=None,
        start_asap=True,
        end_date=None,
        location="Remote",
        remote_status=Remote.REMOTE,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={"firstName": "Anna", "lastName": "Kleinen"},
    )
    view = await _service(session_factory).draft_from_parsed(parsed)
    assert view.recipient == "hr@firma.de"
    assert view.contact_name == "Anna Kleinen"
    stored = await _load_application(session_factory, view.application_id)
    assert stored.listing_id == listing_id


async def test_set_recipient_validates_and_promotes_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    service = _service(session_factory)
    view = await service.draft_for_listing(listing_id)
    with pytest.raises(ApplicationStateError):
        await service.set_recipient(view.application_id, "keine adresse")
    updated = await service.set_recipient(view.application_id, " pm@firma.de ")
    assert updated.recipient == "pm@firma.de"
    assert updated.status is ApplicationStatus.READY


async def test_revise_updates_draft_and_accumulates_tokens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    service = _service(session_factory, [_draft(), _draft("Bewerbung: überarbeitet")])
    view = await service.draft_for_listing(listing_id)
    revised = await service.revise(view.application_id, "Bitte kürzer")
    assert revised.subject == "Bewerbung: überarbeitet"
    assert revised.revision_count == 1
    stored = await _load_application(session_factory, revised.application_id)
    assert stored.tokens_in == 14
    assert stored.tokens_out == 6


async def test_send_marks_sent_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    send = _FakeSend()
    service = _service(session_factory, mailer=_mailer(send))
    view = await service.draft_for_listing(listing_id)
    sent = await service.send(view.application_id)
    assert sent.status is ApplicationStatus.SENT
    assert len(send.sent) == 1
    assert send.sent[0]["To"] == "jobs@firma.de"
    stored = await _load_application(session_factory, sent.application_id)
    assert stored.sent_at is not None
    with pytest.raises(ApplicationStateError):  # double-tap guard
        await service.send(view.application_id)
    assert len(send.sent) == 1


def _cvs(base: object, *, with_english_docx: bool = True) -> CvAttachments:
    """Four CV files on disk (bar the optional EN .docx, to cover a missing one)."""
    from pathlib import Path

    root = Path(str(base))
    paths = {
        "de_pdf": root / "CV-DE.pdf",
        "en_pdf": root / "CV-EN.pdf",
        "de_docx": root / "CV-DE-Word.docx",
        "en_docx": root / "CV-EN-Word.docx",
    }
    for key, path in paths.items():
        if key != "en_docx" or with_english_docx:
            path.write_bytes(b"%PDF")
    return CvAttachments(**paths)


async def test_send_attaches_every_cv_matching_language_first(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: object,
) -> None:
    """All four CVs ride along; the draft language only decides the order."""
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    send = _FakeSend()
    english = _draft()
    english.body = "Dear Sir or Madam, I would be glad to support your project."
    service = _service(
        session_factory,
        drafts=[english, english],
        mailer=_mailer(send),
        cv_attachments=_cvs(tmp_path),
    )
    view = await service.draft_for_listing(listing_id)
    await service.send(view.application_id)

    files = [part.get_filename() for part in send.sent[0].iter_attachments()]
    assert files == ["CV-EN.pdf", "CV-EN-Word.docx", "CV-DE.pdf", "CV-DE-Word.docx"]


async def test_draft_view_names_attachments_and_reports_a_missing_one(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: object,
) -> None:
    """The draft says what a send will attach, so a missing file is visible up front."""
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    service = _service(session_factory, cv_attachments=_cvs(tmp_path, with_english_docx=False))
    view = await service.draft_for_listing(listing_id)

    assert view.attachments == ("CV-DE.pdf", "CV-DE-Word.docx", "CV-EN.pdf")
    assert view.missing_attachments == ("CV-EN-Word.docx",)


async def test_send_without_configured_cv_has_no_attachment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    send = _FakeSend()
    service = _service(session_factory, mailer=_mailer(send))  # no cv_attachments
    view = await service.draft_for_listing(listing_id)
    await service.send(view.application_id)
    assert list(send.sent[0].iter_attachments()) == []


async def test_send_without_recipient_or_mailer_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    no_mailer = _service(session_factory)
    view = await no_mailer.draft_for_listing(listing_id)
    with pytest.raises(ApplicationStateError, match="SMTP"):
        await no_mailer.send(view.application_id)
    with_mailer = _service(session_factory, mailer=_mailer(_FakeSend()))
    with pytest.raises(ApplicationStateError, match="Recipient"):
        await with_mailer.send(view.application_id)


async def test_send_failure_keeps_draft_and_records_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    send = _FakeSend(err=OSError("connection refused"))
    service = _service(session_factory, mailer=_mailer(send))
    view = await service.draft_for_listing(listing_id)
    with pytest.raises(EmailSendError):
        await service.send(view.application_id)
    stored = await _load_application(session_factory, view.application_id)
    assert stored.status is ApplicationStatus.READY
    assert stored.error is not None
    assert "connection refused" in stored.error


async def test_cancel_blocks_further_edits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    service = _service(session_factory)
    view = await service.draft_for_listing(listing_id)
    cancelled = await service.cancel(view.application_id)
    assert cancelled.status is ApplicationStatus.CANCELLED
    with pytest.raises(ApplicationStateError):
        await service.revise(view.application_id, "egal")


async def test_draft_ref_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    service = _service(session_factory)
    view = await service.draft_for_listing(listing_id)
    await service.record_draft_ref(view.application_id, "C0123:1700.42")
    found = await service.find_by_draft_ref("C0123:1700.42")
    assert found is not None
    assert found.application_id == view.application_id
    assert await service.find_by_draft_ref("C0123:9.9") is None


async def test_find_listing_id_by_url_canonicalizes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    service = _service(session_factory)
    found = await service.find_listing_id_by_url(
        "https://www.freelancermap.de/projekt/ki-projekt/?ref=telegram#top"
    )
    assert found == listing_id
    assert await service.find_listing_id_by_url("https://www.freelancermap.de/projekt/x") is None


async def test_render_listing_entity_includes_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = _listing(
        "Beschreibungstext",
        raw={"company": "Firma GmbH", "firstName": "Anna", "lastName": "Muster"},
        location="München",
    )
    await _store(session_factory, listing)
    text = render_listing_entity(listing)
    assert "KI-Projekt" in text
    assert "Firma GmbH" in text
    assert "Anna Muster" in text
    assert "München" in text
    assert "Beschreibungstext" in text


def test_render_listing_entity_carries_the_reference_number() -> None:
    def listing(raw: dict[str, object]) -> Listing:
        entity = _listing(raw=raw)
        entity.remote_status = Remote.REMOTE  # column default only lands on flush
        return entity

    assert "Reference: 12345" in render_listing_entity(listing({"id": 12345}))
    assert "Reference:" not in render_listing_entity(listing({"company": "Firma GmbH"}))


async def test_drafts_are_queryable_per_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing())
    service = _service(session_factory)
    view = await service.draft_for_listing(listing_id)
    async with session_factory() as session:
        rows = (
            (await session.scalars(select(Application).where(Application.listing_id == listing_id)))
            .unique()
            .all()
        )
    assert [row.id for row in rows] == [view.application_id]


async def test_send_blocks_interrupted_sending_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _store(session_factory, _listing("jobs@firma.de"))
    send = _FakeSend()
    service = _service(session_factory, mailer=_mailer(send))
    view = await service.draft_for_listing(listing_id)
    async with session_factory() as session:
        row = await session.get(Application, view.application_id)
        assert row is not None
        row.status = ApplicationStatus.SENDING  # simulate a crash mid-send
        await session.commit()
    with pytest.raises(ApplicationStateError, match="interrupted"):
        await service.send(view.application_id)
    assert send.sent == []
