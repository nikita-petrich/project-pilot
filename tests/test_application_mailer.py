"""Tests for the SMTP mailer (fake transport, TLS mode selection, error mapping)."""

from email.message import EmailMessage

import aiosmtplib
import pytest

from project_pilot.application.mailer import SmtpMailer
from project_pilot.config import SmtpConfig
from project_pilot.errors import EmailSendError


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
