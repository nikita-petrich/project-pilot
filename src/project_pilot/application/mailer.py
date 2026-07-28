"""Async SMTP delivery of application e-mails (aiosmtplib, direct to Nik's server)."""

import asyncio
import logging
import mimetypes
import re
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Protocol

import aiosmtplib

from project_pilot.application.signature import CID_REF, Signature
from project_pilot.config import SmtpConfig
from project_pilot.errors import EmailSendError

logger = logging.getLogger(__name__)

# The HTML alternative styles the body to match the signature, so the mail reads
# as one piece instead of a plain letter with a designed block glued underneath.
_BODY_STYLE = (
    "font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.45;color:#1a1a1a;"
)


def _body_to_html(body: str) -> str:
    """Render the plain-text draft as HTML paragraphs (blank line = new paragraph)."""
    paragraphs = (block for block in re.split(r"\n\s*\n", body.strip()) if block.strip())
    return "".join(
        '<p style="margin:0 0 12px;">'
        + "<br>".join(escape(line) for line in block.splitlines())
        + "</p>"
        for block in paragraphs
    )


def _rewrite_cids(html: str, cids: Mapping[str, str]) -> str:
    """Swap the template's ``cid:<name>`` placeholders for this message's Content-IDs."""
    return CID_REF.sub(
        lambda match: (
            f"cid:{cids[match.group(1)][1:-1]}" if match.group(1) in cids else match.group(0)
        ),
        html,
    )


def _html_document(body: str, signature: Signature, cids: Mapping[str, str]) -> str:
    """The full HTML alternative: the draft body followed by the signature block."""
    return (
        f'<html><body><div style="{_BODY_STYLE}">'
        f"{_body_to_html(body)}{_rewrite_cids(signature.html, cids)}"
        "</div></body></html>"
    )


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
        self,
        config: SmtpConfig,
        *,
        send_fn: SmtpSendFn | None = None,
        timeout: float = 30.0,
        cid_factory: Callable[[], str] = make_msgid,
    ) -> None:
        self._config = config
        self._send_fn: SmtpSendFn = send_fn if send_fn is not None else aiosmtplib.send
        self._timeout = timeout
        self._cid_factory = cid_factory

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Path] = (),
        signature: Signature | None = None,
    ) -> None:
        """Deliver the message; raise ``EmailSendError`` on any SMTP or network failure."""
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = to
        message["Subject"] = " ".join(subject.splitlines())  # headers must never fold
        self._set_body(message, body, signature)
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

    def _set_body(self, message: EmailMessage, body: str, signature: Signature | None) -> None:
        """Plain text alone, or a text/HTML alternative whose images ride along as CIDs.

        Content-IDs are minted per message so they can never collide with an ID in
        quoted or forwarded content.
        """
        if signature is None:
            message.set_content(body)
            return
        message.set_content(f"{body}\n\n{signature.text}\n")
        cids = {image.name: self._cid_factory() for image in signature.images}
        message.add_alternative(_html_document(body, signature, cids), subtype="html")
        html_part = list(message.iter_parts())[-1]
        for image in signature.images:
            html_part.add_related(
                image.data,
                maintype=image.maintype,
                subtype=image.subtype,
                cid=cids[image.name],
            )

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
