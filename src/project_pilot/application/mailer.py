"""Async SMTP delivery of application e-mails (aiosmtplib, direct to Nik's server)."""

import asyncio
import logging
import mimetypes
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

import aiosmtplib

from project_pilot.config import SmtpConfig
from project_pilot.errors import EmailSendError

logger = logging.getLogger(__name__)


class SmtpSendFn(Protocol):
    """The subset of ``aiosmtplib.send`` the mailer uses (injectable in tests)."""

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
    ) -> object: ...


class SmtpMailer:
    """Sends one plain-text e-mail per call through the configured SMTP server.

    Port 465 implies implicit TLS; every other port uses STARTTLS unless disabled
    via ``SMTP_STARTTLS=false``.
    """

    def __init__(
        self, config: SmtpConfig, *, send_fn: SmtpSendFn | None = None, timeout: float = 30.0
    ) -> None:
        self._config = config
        self._send_fn: SmtpSendFn = send_fn if send_fn is not None else aiosmtplib.send
        self._timeout = timeout

    async def send(
        self, *, to: str, subject: str, body: str, attachments: Sequence[Path] = ()
    ) -> None:
        """Deliver the message; raise ``EmailSendError`` on any SMTP or network failure."""
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = to
        message["Subject"] = " ".join(subject.splitlines())  # headers must never fold
        message.set_content(body)
        for path in attachments:
            await self._attach(message, path)
        use_tls = self._config.port == 465
        try:
            await self._send_fn(
                message,
                hostname=self._config.host,
                port=self._config.port,
                username=self._config.username,
                password=self._config.password,
                use_tls=use_tls,
                start_tls=self._config.use_starttls and not use_tls,
                timeout=self._timeout,
            )
        except (aiosmtplib.SMTPException, OSError) as err:
            raise EmailSendError(f"smtp send to {to} failed: {err}") from err
        logger.info("application e-mail sent to %s", to)

    @staticmethod
    async def _attach(message: EmailMessage, path: Path) -> None:
        """Read ``path`` off the event loop and add it as a MIME attachment."""
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as err:
            raise EmailSendError(f"could not read attachment {path}: {err}") from err
        mime, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(
            data, maintype=maintype, subtype=subtype or "octet-stream", filename=path.name
        )
