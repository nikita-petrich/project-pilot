"""Application flow: draft creation, revision, recipient handling, guarded sending."""

import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.application.documents import (
    ImageAttachment,
    annotate_image_listing,
    fallback_listing_title,
)
from project_pilot.application.generator import ApplicationGenerator, GeneratedDraft
from project_pilot.application.mailer import SmtpMailer
from project_pilot.application.schemas import ApplicationDraft
from project_pilot.config import CvAttachments
from project_pilot.db import session_scope
from project_pilot.errors import ApplicationStateError
from project_pilot.evaluation.llm import render_listing, render_listing_entity
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.normalize import (
    canonicalize_url,
    compute_url_hash,
    detect_language,
    extract_listing_title,
    resolve_contact_name,
)
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import Application, ApplicationStatus
from project_pilot.profile_loader import Profile
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_EXCLUDED_EMAIL_RE = re.compile(r"no-?reply|@freelancermap\.|@example\.", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_email(text: str) -> bool:
    """True when ``text`` is nothing but a single e-mail address."""
    return _EMAIL_RE.fullmatch(text.strip()) is not None


def extract_email(text: str) -> str | None:
    """First plausible contact address in ``text`` (skips noreply/platform addresses)."""
    for match in _EMAIL_RE.finditer(text):
        candidate = match.group(0).rstrip(".")
        if _EXCLUDED_EMAIL_RE.search(candidate):
            continue
        return candidate
    return None


def _raw_str(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _contact_from_raw(raw: dict[str, object], description: str) -> str | None:
    """Resolve the contact person from a raw source record plus the description text."""
    return resolve_contact_name(
        _raw_str(raw, "firstName"), _raw_str(raw, "lastName"), _raw_str(raw, "company"), description
    )


def _iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


@dataclass(frozen=True, slots=True)
class DraftView:
    """Display-ready snapshot of an application (what the bot renders)."""

    application_id: int
    title: str
    url: str | None
    contact_name: str | None
    recipient: str | None
    subject: str
    body: str
    linkedin_message: str
    status: ApplicationStatus
    revision_count: int
    listing_id: int | None = None
    company: str | None = None
    attachments: tuple[str, ...] = ()
    missing_attachments: tuple[str, ...] = ()


def _to_view(
    application: Application,
    *,
    attachments: tuple[str, ...] = (),
    missing_attachments: tuple[str, ...] = (),
) -> DraftView:
    return DraftView(
        application_id=application.id,
        title=application.listing_title,
        url=application.listing_url,
        contact_name=application.contact_name,
        recipient=application.recipient_email,
        subject=application.subject,
        body=application.body,
        linkedin_message=application.linkedin_message,
        status=application.status,
        revision_count=application.revision_count,
        listing_id=application.listing_id,
        company=application.company,
        attachments=attachments,
        missing_attachments=missing_attachments,
    )


class ApplicationService:
    """Domain logic for the apply flow; one session per interaction (unit of work)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        generator: ApplicationGenerator,
        profile: Profile,
        mailer: SmtpMailer | None,
        cv_attachments: CvAttachments | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._generator = generator
        self._profile = profile
        self._mailer = mailer
        self._cv_attachments = cv_attachments

    async def draft_for_listing(self, listing_id: int) -> DraftView:
        """Generate and persist a draft for a stored listing (Apply button, known URL)."""
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            listing = await repo.get_listing(listing_id)
            if listing is None:
                raise ApplicationStateError(f"Project {listing_id} not found")
            listing_text = render_listing_entity(listing)
            recipient = extract_email(
                "\n".join((listing.description or "", *_iter_strings(listing.raw or {})))
            )
            contact_name = _contact_from_raw(listing.raw or {}, listing.description or "")
            generated = await self._generate(listing_text, contact_name=contact_name)
            application = self._build_application(
                generated,
                listing_text=listing_text,
                title=listing.title,
                url=listing.external_url,
                listing_id=listing.id,
                recipient=recipient,
                contact_name=contact_name,
                company=_raw_str(listing.raw or {}, "company"),
            )
            await repo.add_application(application)
            return self._view(application)

    async def draft_from_parsed(self, parsed: ParsedListing) -> DraftView:
        """Generate a draft for a freshly fetched detail page (``/apply <url>``)."""
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            stored = await repo.get_listing_by_hash(parsed.url_hash)
            listing_text = render_listing(parsed)
            recipient = extract_email("\n".join((parsed.description, *_iter_strings(parsed.raw))))
            contact_name = _contact_from_raw(parsed.raw, parsed.description)
            generated = await self._generate(listing_text, contact_name=contact_name)
            application = self._build_application(
                generated,
                listing_text=listing_text,
                title=parsed.title,
                url=parsed.external_url,
                listing_id=stored.id if stored is not None else None,
                recipient=recipient,
                contact_name=contact_name,
                company=_raw_str(parsed.raw, "company"),
            )
            await repo.add_application(application)
            return self._view(application)

    async def draft_from_text(
        self, text: str, *, images: Sequence[ImageAttachment] = ()
    ) -> DraftView:
        """Generate a draft from a pasted description and/or listing screenshots.

        Attached images go straight into the vision LLM call; the persisted
        ``listing_text`` keeps a marker per image so later revisions (which can no
        longer see the pixels) know the listing arrived as a screenshot.
        """
        stripped = text.strip()
        listing_text = annotate_image_listing(stripped, images)
        # A recruiter mail opens with "Hallo," — read the real headline out of the
        # text, and let the model's project_title name it when there is none.
        heading = extract_listing_title(stripped)
        contact_name = resolve_contact_name(None, None, None, text)
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            generated = await self._generate(listing_text, images=images, contact_name=contact_name)
            title = (
                heading or generated.draft.project_title or fallback_listing_title(stripped, images)
            )
            application = self._build_application(
                generated,
                listing_text=listing_text,
                title=title,
                url=None,
                listing_id=None,
                recipient=extract_email(text),
                contact_name=contact_name,
            )
            await repo.add_application(application)
            return self._view(application)

    async def revise(
        self, application_id: int, instruction: str, *, images: Sequence[ImageAttachment] = ()
    ) -> DraftView:
        """Rewrite the draft per Nik's reply; sent/cancelled applications are immutable."""
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            application = await self._editable(repo, application_id)
            current = ApplicationDraft(
                project_title=application.listing_title,
                subject=application.subject,
                body=application.body,
                linkedin_message=application.linkedin_message,
            )
            generated = await self._generator.revise(
                profile_text=self._profile.text,
                listing_text=application.listing_text,
                current=current,
                instruction=instruction,
                images=images,
                contact_name=application.contact_name,
            )
            application.subject = generated.draft.subject
            application.body = generated.draft.body
            application.linkedin_message = generated.draft.linkedin_message
            application.revision_count += 1
            application.tokens_in = (application.tokens_in or 0) + (generated.tokens_in or 0)
            application.tokens_out = (application.tokens_out or 0) + (generated.tokens_out or 0)
            await session.flush()
            return self._view(application)

    async def set_recipient(self, application_id: int, email: str) -> DraftView:
        """Set/replace the recipient address (reply containing a bare e-mail)."""
        address = email.strip()
        if not is_email(address):
            raise ApplicationStateError(f"{address!r} is not a valid e-mail address")
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            application = await self._editable(repo, application_id)
            application.recipient_email = address
            if application.status is ApplicationStatus.AWAITING_EMAIL:
                application.status = ApplicationStatus.READY
            await session.flush()
            return self._view(application)

    async def send(self, application_id: int) -> DraftView:
        """Deliver the e-mail via SMTP; committed status steps guard against double sends."""
        if self._mailer is None:
            raise ApplicationStateError(
                "SMTP is not configured (set SMTP_HOST/SMTP_USER/SMTP_PASSWORD)"
            )
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            application = await self._editable(repo, application_id)
            if not application.recipient_email:
                raise ApplicationStateError(
                    "Recipient missing - reply to the draft with the e-mail address"
                )
            recipient = application.recipient_email
            subject = application.subject
            body = application.body
            # Atomic READY -> SENDING so two concurrent Send clicks cannot both
            # deliver; the loser sees the claim fail. Committed before the SMTP call,
            # so a crash mid-send leaves 'sending' behind (blocks a blind retry).
            if not await repo.claim_for_send(application_id):
                raise ApplicationStateError(
                    "A send is already in progress for this draft - "
                    "check your Sent folder before retrying."
                )

        # Every configured CV rides along (PDF and Word, DE and EN); the draft's
        # language only decides which one leads.
        cvs = self._cv_attachments
        language = detect_language(body)
        attachments = cvs.for_language(language) if cvs is not None else []
        missing = cvs.missing(language) if cvs is not None else []
        if missing:
            logger.warning(
                "sending %d without %s (file not found)",
                application_id,
                ", ".join(path.name for path in missing),
            )

        # The SMTP call happens outside any unit of work so a slow/failing server
        # never holds a transaction; the outcome is recorded in a follow-up one.
        try:
            await self._mailer.send(
                to=recipient, subject=subject, body=body, attachments=attachments
            )
        except Exception as err:
            try:
                async with session_scope(self._session_factory) as session:
                    failed = await Repository(session).get_application(application_id)
                    if failed is not None:
                        failed.status = ApplicationStatus.READY
                        failed.error = str(err)
            except Exception:  # never mask the send failure with a bookkeeping failure
                logger.exception("could not record send failure for %d", application_id)
            raise

        async with session_scope(self._session_factory) as session:
            # The send routine owns the SENDING -> SENT transition, so it reads the
            # row directly rather than through ``_editable`` (which now refuses a
            # SENDING row to protect the in-flight delivery from other actions).
            sent = await Repository(session).get_application(application_id)
            if sent is None:  # pragma: no cover - the row was just updated
                raise ApplicationStateError(f"Draft {application_id} not found")
            sent.status = ApplicationStatus.SENT
            sent.sent_at = _utcnow()
            sent.error = None
            await session.flush()
            return self._view(sent)

    async def cancel(self, application_id: int) -> DraftView:
        async with session_scope(self._session_factory) as session:
            application = await self._editable(Repository(session), application_id)
            application.status = ApplicationStatus.CANCELLED
            await session.flush()
            return self._view(application)

    async def record_draft_ref(self, application_id: int, draft_ref: str) -> None:
        """Remember the Slack message (``channel:ts``) that shows the draft (routing key)."""
        async with session_scope(self._session_factory) as session:
            application = await Repository(session).get_application(application_id)
            if application is not None:
                application.draft_ref = draft_ref

    async def find_by_draft_ref(self, draft_ref: str) -> DraftView | None:
        async with session_scope(self._session_factory) as session:
            application = await Repository(session).get_application_by_draft_ref(draft_ref)
            return self._view(application) if application is not None else None

    async def find_listing_id_by_url(self, url: str) -> int | None:
        url_hash = compute_url_hash(canonicalize_url(url, BASE_URL))
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing_by_hash(url_hash)
            return listing.id if listing is not None else None

    def _view(self, application: Application) -> DraftView:
        """Snapshot for rendering, including the CVs a send would attach.

        Naming them in the draft makes a missing file visible before the send rather
        than after it — the recipient can only see what was actually on disk.
        """
        if self._cv_attachments is None:
            return _to_view(application)
        language = detect_language(application.body)
        return _to_view(
            application,
            attachments=tuple(path.name for path in self._cv_attachments.for_language(language)),
            missing_attachments=tuple(path.name for path in self._cv_attachments.missing(language)),
        )

    async def _generate(
        self,
        listing_text: str,
        *,
        images: Sequence[ImageAttachment] = (),
        contact_name: str | None = None,
    ) -> GeneratedDraft:
        return await self._generator.generate(
            profile_text=self._profile.text,
            listing_text=listing_text,
            images=images,
            contact_name=contact_name,
        )

    def _build_application(
        self,
        generated: GeneratedDraft,
        *,
        listing_text: str,
        title: str,
        url: str | None,
        listing_id: int | None,
        recipient: str | None,
        contact_name: str | None,
        company: str | None = None,
    ) -> Application:
        return Application(
            listing_id=listing_id,
            listing_url=url,
            listing_title=title,
            listing_text=listing_text,
            contact_name=contact_name,
            company=company,
            recipient_email=recipient,
            subject=generated.draft.subject,
            body=generated.draft.body,
            linkedin_message=generated.draft.linkedin_message,
            status=ApplicationStatus.READY if recipient else ApplicationStatus.AWAITING_EMAIL,
            model=generated.model,
            prompt_version=generated.prompt_version,
            profile_hash=self._profile.profile_hash,
            tokens_in=generated.tokens_in,
            tokens_out=generated.tokens_out,
        )

    async def _editable(self, repo: Repository, application_id: int) -> Application:
        application = await repo.get_application(application_id)
        if application is None:
            raise ApplicationStateError(f"Draft {application_id} not found")
        if application.status is ApplicationStatus.SENT:
            raise ApplicationStateError("This application has already been sent")
        if application.status is ApplicationStatus.CANCELLED:
            raise ApplicationStateError("This draft has been discarded")
        if application.status is ApplicationStatus.SENDING:
            # A send is mid-flight (or one crashed and left this behind): editing,
            # cancelling, or re-sending now would corrupt the in-flight delivery.
            raise ApplicationStateError(
                "A send is in progress for this draft - check your Sent folder. "
                "Discard it or create a new one if the send did not complete."
            )
        return application
