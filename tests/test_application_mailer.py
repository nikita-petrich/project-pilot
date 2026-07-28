"""Tests for the SMTP mailer (fake transport, TLS mode selection, error mapping)."""

from collections.abc import Callable
from email.message import EmailMessage
from itertools import count

import aiosmtplib
import pytest

from project_pilot.application.mailer import SmtpMailer
from project_pilot.application.signature import InlineImage, Signature
from project_pilot.config import SmtpConfig
from project_pilot.errors import EmailSendError


def _cids() -> Callable[[], str]:
    """Deterministic Content-ID factory so assertions can name the expected value."""
    counter = count(1)
    return lambda: f"<cid{next(counter)}@project-pilot>"


def _signature() -> Signature:
    return Signature(
        html='<table><tr><td><img src="cid:photo"></td><td>Nikita Petrich</td></tr></table>',
        text="--\nNikita Petrich\nn.petrich@sequenz.io",
        images=(InlineImage(name="photo", data=b"\xff\xd8jpeg", maintype="image", subtype="jpeg"),),
    )


def _config(port: int = 587, use_starttls: bool = True) -> SmtpConfig:
    return SmtpConfig(
        host="mail.example.com",
        port=port,
        username="user",
        password="secret",
        sender="nik@example.com",
        use_starttls=use_starttls,
    )


class _FakeSend:
    def __init__(self, *, err: Exception | None = None) -> None:
        self.err = err
        self.message: EmailMessage | None = None
        self.kwargs: dict[str, object] = {}

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
        self.message = message
        self.kwargs = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
            "start_tls": start_tls,
            "timeout": timeout,
        }
        if self.err is not None:
            raise self.err
        return {}


async def test_send_builds_message_and_uses_starttls() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake).send(
        to="pm@firma.de", subject="Bewerbung", body="Hallo"
    )
    assert fake.message is not None
    assert fake.message["From"] == "nik@example.com"
    assert fake.message["To"] == "pm@firma.de"
    assert fake.message["Subject"] == "Bewerbung"
    assert "Hallo" in fake.message.get_content()
    assert fake.kwargs["hostname"] == "mail.example.com"
    assert fake.kwargs["use_tls"] is False
    assert fake.kwargs["start_tls"] is True


async def test_send_port_465_switches_to_implicit_tls() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(port=465), send_fn=fake).send(to="a@b.de", subject="s", body="b")
    assert fake.kwargs["use_tls"] is True
    assert fake.kwargs["start_tls"] is False


async def test_smtp_error_becomes_email_send_error() -> None:
    fake = _FakeSend(err=aiosmtplib.SMTPException("auth failed"))
    with pytest.raises(EmailSendError, match="auth failed"):
        await SmtpMailer(_config(), send_fn=fake).send(to="a@b.de", subject="s", body="b")


async def test_network_error_becomes_email_send_error() -> None:
    fake = _FakeSend(err=OSError("connection refused"))
    with pytest.raises(EmailSendError):
        await SmtpMailer(_config(), send_fn=fake).send(to="a@b.de", subject="s", body="b")


async def test_subject_newlines_are_flattened() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake).send(to="a@b.de", subject="Zeile1\nZeile2", body="b")
    assert fake.message is not None
    assert fake.message["Subject"] == "Zeile1 Zeile2"


async def test_send_attaches_files_with_name_and_type(tmp_path: object) -> None:
    from pathlib import Path

    cv = Path(str(tmp_path)) / "CV-DE.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake).send(
        to="a@b.de", subject="s", body="b", attachments=[cv]
    )
    assert fake.message is not None
    parts = list(fake.message.iter_attachments())
    assert [p.get_filename() for p in parts] == ["CV-DE.pdf"]
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 fake"


async def test_missing_attachment_file_raises_email_send_error(tmp_path: object) -> None:
    from pathlib import Path

    missing = Path(str(tmp_path)) / "nope.pdf"
    with pytest.raises(EmailSendError, match="attachment"):
        await SmtpMailer(_config(), send_fn=_FakeSend()).send(
            to="a@b.de", subject="s", body="b", attachments=[missing]
        )


async def test_without_signature_the_mail_stays_plain_text() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake).send(to="a@b.de", subject="s", body="Hallo")
    assert fake.message is not None
    assert fake.message.get_content_type() == "text/plain"


async def test_signature_produces_text_and_html_alternatives() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake, cid_factory=_cids()).send(
        to="a@b.de",
        subject="s",
        body="Guten Tag\n\nMit freundlichen Grüßen\nNikita",
        signature=_signature(),
    )
    assert fake.message is not None
    assert fake.message.get_content_type() == "multipart/alternative"

    plain = fake.message.get_body(preferencelist=("plain",))
    html = fake.message.get_body(preferencelist=("html",))
    assert plain is not None and html is not None
    # The text part carries the plain-text signature for clients that ignore HTML.
    assert "n.petrich@sequenz.io" in plain.get_content()
    # The HTML part carries the body as paragraphs plus the signature block.
    assert "<p" in html.get_content()
    assert "Nikita Petrich" in html.get_content()


async def test_cid_placeholder_is_rewritten_and_image_embedded() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake, cid_factory=_cids()).send(
        to="a@b.de", subject="s", body="Hallo", signature=_signature()
    )
    assert fake.message is not None
    html = fake.message.get_body(preferencelist=("html",))
    assert html is not None
    # The template placeholder is replaced by this message's real Content-ID.
    assert 'src="cid:cid1@project-pilot"' in html.get_content()
    assert "cid:photo" not in html.get_content()

    related = [part for part in fake.message.walk() if part.get_content_type() == "image/jpeg"]
    assert len(related) == 1
    assert related[0]["Content-ID"] == "<cid1@project-pilot>"
    assert related[0].get_payload(decode=True) == b"\xff\xd8jpeg"


async def test_inline_image_is_not_offered_as_an_attachment_next_to_the_cv(
    tmp_path: object,
) -> None:
    from pathlib import Path

    cv = Path(str(tmp_path)) / "CV-DE.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake, cid_factory=_cids()).send(
        to="a@b.de", subject="s", body="Hallo", attachments=[cv], signature=_signature()
    )
    assert fake.message is not None
    # Only the CV is a real attachment; the photo lives inside the HTML alternative.
    assert [p.get_filename() for p in fake.message.iter_attachments()] == ["CV-DE.pdf"]


async def test_body_html_escapes_markup_from_the_draft() -> None:
    fake = _FakeSend()
    await SmtpMailer(_config(), send_fn=fake, cid_factory=_cids()).send(
        to="a@b.de", subject="s", body="Preis < 100 & fair", signature=_signature()
    )
    assert fake.message is not None
    html = fake.message.get_body(preferencelist=("html",))
    assert html is not None
    assert "Preis &lt; 100 &amp; fair" in html.get_content()
