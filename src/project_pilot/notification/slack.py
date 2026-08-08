"""Slack Block Kit message building and a thin async Web API client wrapper.

A match is two messages: a compact card in the channel (title, score, the few
facts that decide a yes/no, and the buttons) plus the full listing as its first
thread reply, so a scan run stays scannable and the detail is one click away. A
draft posts the e-mail (as a ``.txt`` file in the thread, falling back to inline
blocks), the LinkedIn text, an open-in-mail-client link, and Send/Discard buttons.
Generated texts render as native code blocks, so every one carries Slack's copy
button in its corner.

All bot chrome (labels, buttons, hints, status) is English; only the generated
application text follows the project's language.
"""

import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from project_pilot.application.schemas import LINKEDIN_LIMIT
from project_pilot.application.service import DraftView
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.evaluation.check import CheckResult
from project_pilot.models import ApplicationStatus, EvaluationStage, Verdict
from project_pilot.notification.messages import MatchMessage

logger = logging.getLogger(__name__)

# Slack caps a section's text at 3000 chars and a message at 50 blocks; stay under.
_SECTION_LIMIT = 2900
_HEADER_LIMIT = 150
# A one-message verdict (``/check`` on a stored listing) previews the description
# and keeps the full text one click away behind its "View project" link. Without
# such a link (pasted text, uploads) the description is rendered in full instead —
# and so it is in a match thread, whose card already carries the link.
_DESCRIPTION_PREVIEW = 700
# The channel card names only the top reasons; the rest lives in the thread reply.
_SUMMARY_REASONS = 2
# Sections one long text may occupy — a message is capped at 50 blocks in total.
_MAX_CODE_PARTS = 8
# A mailto: must fit in its own section, so the body it carries is bounded; the
# complete text always stays available in the copyable block above it.
_MAILTO_LIMIT = 2600

type Block = dict[str, object]


def _esc(text: str) -> str:
    """Escape the three characters Slack mrkdwn reserves (``&`` first)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _header(text: str) -> Block:
    trimmed = text if len(text) <= _HEADER_LIMIT else text[: _HEADER_LIMIT - 1] + "…"
    return {"type": "header", "text": {"type": "plain_text", "text": trimmed, "emoji": True}}


def _section(mrkdwn: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _context(mrkdwn: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}


def _button(
    text: str, *, action_id: str, value: str | None = None, url: str | None = None
) -> Block:
    element: Block = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
    }
    if value is not None:
        element["value"] = value
    if url is not None:
        element["url"] = url
    return element


def _link(url: str, label: str) -> str:
    return f"<{url}|{_esc(label)}>"


def _split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into ``<= limit`` chunks on line boundaries; nothing dropped."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def status_blocks(text: str) -> list[Block]:
    """A single mrkdwn section — used for progress/confirmation/error messages."""
    return [_section(text)]


def linkedin_search_url(name: str, company: str | None = None) -> str:
    """LinkedIn people search for the contact — ``name AND company`` when both known."""
    keywords = f"{name} AND {company}" if company else name
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}"


def _linkedin_search_actions(contact_name: str, company: str | None = None) -> Block:
    button = _button(
        f"🔍 {contact_name[:60]} on LinkedIn",
        action_id="linkedin_search",
        url=linkedin_search_url(contact_name, company),
    )
    return {"type": "actions", "elements": [button]}


def _preformatted(text: str) -> Block:
    """A native Slack code block (``rich_text_preformatted``).

    Unlike a mrkdwn ``` fence inside a section, this is the same element Slack
    renders for user-typed code blocks — including the copy button in the top-right
    corner on every client that has one.
    """
    return {
        "type": "rich_text",
        "elements": [
            {"type": "rich_text_preformatted", "elements": [{"type": "text", "text": text}]}
        ],
    }


def _code_sections(text: str, *, label: str, max_parts: int | None = None) -> list[Block]:
    """Render ``text`` as a labeled native code block (copy button in the corner).

    Long text is split across consecutive blocks; the pieces render as one
    continuous run. Nothing is dropped unless a caller sets ``max_parts`` — the
    generated texts never do; only secondary text (a description) is bounded, with
    a visible marker, to stay inside the 50-block message limit.
    """
    parts = _split_text(text.strip() or "(leer)", _SECTION_LIMIT)
    kept = parts if max_parts is None else parts[:max_parts]
    blocks: list[Block] = [_section(f"*{label}*")]
    blocks.extend(_preformatted(part) for part in kept)
    if len(kept) < len(parts):
        blocks.append(
            _context(f"… {len(parts) - len(kept)} more block(s) omitted — text unusually long.")
        )
    return blocks


def _description_blocks(description: str, *, url: str | None) -> list[Block]:
    """The listing description: previewed behind a link, or in full when there is none.

    A scan match links to the source, so a long description is trimmed on a word
    boundary and the ``View project`` button carries the rest. Pasted text and
    uploads have no such link — there the full description is rendered, split across
    blocks, because otherwise the cut-off part is simply unreadable.
    """
    text = description.strip()
    if url:
        blocks = [_section("*📄 Description*")]
        if len(text) > _DESCRIPTION_PREVIEW:
            cut = text.rfind(" ", _DESCRIPTION_PREVIEW // 2, _DESCRIPTION_PREVIEW)
            text = text[: cut if cut != -1 else _DESCRIPTION_PREVIEW].rstrip() + " …"
            blocks.append(_preformatted(text))
            blocks.append(_context("_Shortened — full description via 🔗 View project._"))
        else:
            blocks.append(_preformatted(text))
        return blocks
    return _code_sections(text, label="📄 Description", max_parts=_MAX_CODE_PARTS)


def _labeled(label: str, value: str | None) -> str | None:
    return f"*{label}:* {_esc(value)}" if value else None


def _labeled_list(label: str, values: list[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return f"*{label}:* {_esc(', '.join(picked))}" if picked else None


def _client_type(message: MatchMessage) -> str | None:
    if message.is_endcustomer is None:
        return None
    return "Direct client" if message.is_endcustomer else "Agency"


def _inline(parts: list[str | None]) -> str | None:
    """Join the given values into one middot-separated line, dropping the empty ones."""
    picked = [part for part in parts if part]
    return "  ·  ".join(picked) if picked else None


def _summary_body(message: MatchMessage) -> list[Block]:
    """The compact channel card: who is hiring, where, the terms, and the top reasons.

    Company and location get a line each and are always rendered — a listing that
    names neither is itself a signal (agency posts routinely hide the client), so
    the card says so instead of silently dropping the line.
    """
    blocks: list[Block] = [_header(f"🎯 {message.title} · {message.score}/100")]

    company = f"🏢 *{_esc(message.company)}*" if message.company else "🏢 _Company not stated_"
    location = f"📍 {_esc(message.location)}" if message.location else "📍 _Location not stated_"
    lines = [
        _inline([company, _client_type(message)]),
        _inline([location, f"🏠 {_esc(message.remote_label)}" if message.remote_label else None]),
        _inline(
            [
                f"📅 {_esc(message.start)}" if message.start else None,
                f"⏳ {_esc(message.duration_label)}" if message.duration_label else None,
                f"📊 {_esc(message.workload_label)}" if message.workload_label else None,
                f"🕒 {_esc(message.posted_ago)}" if message.posted_ago else None,
            ]
        ),
        _labeled_list("✅ Fits", message.reasons, limit=_SUMMARY_REASONS),
    ]
    summary = "\n".join(line for line in lines if line)
    if summary:
        blocks.append(_section(summary))
    blocks.append(_context("🧵 All facts, skills, gaps and the description in the thread."))
    return blocks


def _detail_body(message: MatchMessage, *, description_url: str | None) -> list[Block]:
    """Every listing fact, the full verdict, and the description.

    ``description_url`` is the link a shortened description points at; passing
    ``None`` renders the description in full (the match thread does, because its
    card above already carries the ``View project`` button).
    """
    blocks: list[Block] = []
    facts = [
        _labeled("🏢 Company", message.company),
        _labeled("👤 Contact", message.contact_name),
        _labeled("🤝 Client type", _client_type(message)),
        _labeled("📍 Location", message.location),
        _labeled("🏠 Remote", message.remote_label),
        _labeled("💼 Contract", message.contract_type),
        _labeled("📊 Workload", message.workload_label),
        _labeled("⏳ Duration", message.duration_label),
        _labeled("📅 Start", message.start),
        _labeled("🕒 Posted", message.posted_ago),
        _labeled("✍️ Apply by", message.expires_label),
        _labeled("🏭 Industry", message.industry or "unknown"),  # always shown
        _labeled("🗣 Language", message.language),
        _labeled_list("🛠 Skills", message.skills, limit=12),
    ]
    facts_text = "\n".join(fact for fact in facts if fact)
    if facts_text:
        blocks.append(_section(facts_text))

    verdict = [
        _labeled_list("✅ Fits", message.reasons, limit=3),
        _labeled_list("🎯 Your skills", message.matching_skills, limit=8),
        _labeled_list("⚠️ Gaps", message.missing_requirements, limit=4),
        _labeled_list("🚩 Risks", message.risk_flags, limit=3),
    ]
    verdict_text = "\n".join(item for item in verdict if item)
    if verdict_text:
        blocks.append(_section(verdict_text))

    if message.description:
        blocks.extend(_description_blocks(message.description, url=description_url))
    return blocks


def _match_body(message: MatchMessage) -> list[Block]:
    """Header plus the full listing in one message — the shape ``/check`` answers with."""
    return [
        _header(f"🎯 {message.title} · {message.score}/100"),
        *_detail_body(message, description_url=message.url),
    ]


def format_match_blocks(message: MatchMessage, *, listing_id: int) -> list[Block]:
    """Build the compact channel card for one matched listing (with the buttons)."""
    blocks = _summary_body(message)
    actions: list[Block] = [
        _button("📝 Apply", action_id="apply", value=str(listing_id)),
        _button("🔎 Find contact", action_id="enrich", value=str(listing_id)),
    ]
    if message.url:
        actions.append(_button("🔗 View project", action_id="open_project", url=message.url))
    blocks.append({"type": "actions", "elements": actions})
    return blocks


def format_match_detail_blocks(message: MatchMessage) -> list[Block]:
    """Build the match's thread reply: every fact, the full verdict, the description."""
    return _detail_body(message, description_url=None)


def _reason_list(reason: dict[str, object], key: str) -> list[str]:
    value = reason.get(key)
    return [str(item) for item in value] if isinstance(value, list) else []


def _hard_rule_line(reason: dict[str, object]) -> str:
    rule = str(reason.get("rule", "unknown"))
    if rule == "blacklist":
        term = str(reason.get("matched_term", ""))
        return f"🚫 Hard rule *blacklist* hit: `{_esc(term)}` (0 tokens spent)."
    required = ", ".join(_reason_list(reason, "required"))
    return f"🚫 Hard rule *must-have* failed: none of the required terms found ({_esc(required)})."


def format_check_blocks(
    result: CheckResult, *, apply_action: str | None = None, apply_value: str | None = None
) -> list[Block]:
    """Render a manual ``/check`` verdict; a pass reuses the match-message body.

    A passing check looks exactly like a scan match (full listing, apply button) so
    the follow-up flow is identical; the apply button's action/value depend on how
    the checked listing can be reached again (stored id, URL, or remembered text).
    """
    if result.passed and result.message is not None:
        blocks = _match_body(result.message)
        actions: list[Block] = []
        if apply_action is not None and apply_value is not None:
            actions.append(_button("📝 Apply", action_id=apply_action, value=apply_value))
        if result.message.url:
            actions.append(
                _button("🔗 View project", action_id="open_project", url=result.message.url)
            )
        if actions:
            blocks.append({"type": "actions", "elements": actions})
        blocks.append(
            _context(
                f"🔍 Check verdict: ✅ match — score {result.score} ≥ "
                f"threshold {result.threshold}. Nothing was stored."
            )
        )
        return blocks

    blocks = [_header(f"❌ No match: {result.title}")]
    if result.stage is EvaluationStage.HARD_RULE:
        blocks.append(_section(_hard_rule_line(result.reason)))
    elif result.is_llm_error:
        blocks.append(
            _section("⚠️ The LLM returned no verdict (fallback: no match) — try again later.")
        )
    else:
        lines = [f"*Score:* {result.score}/100 (threshold {result.threshold})"]
        if result.verdict is Verdict.MATCH:
            lines[0] += " — a match, but below your threshold"
        for label, key, limit in (
            ("📝 Reasons", "reasons", 4),
            ("🎯 Your skills", "matching_skills", 8),
            ("⚠️ Gaps", "missing_requirements", 4),
            ("🚩 Risks", "risk_flags", 3),
        ):
            line = _labeled_list(label, _reason_list(result.reason, key), limit=limit)
            if line:
                lines.append(line)
        blocks.append(_section("\n".join(lines)))
    blocks.append(_context("🔍 Check verdict — nothing was stored or notified."))
    return blocks


def check_fallback_text(result: CheckResult) -> str:
    verdict = "match" if result.passed else "no match"
    return f"🔍 Check: {verdict} — {result.title}"


def format_upload_prompt_blocks(label: str, *, key: str, can_check: bool) -> list[Block]:
    """Ask what an upload is for, as buttons.

    Slack cannot attach a file to a slash command, so the intent is chosen after
    the fact. Buttons rather than a keyword: nothing to remember, and an upload
    never spends a token until it is clicked.
    """
    actions: list[Block] = [_button("📝 Apply", action_id="upload_apply", value=key)]
    if can_check:
        actions.append(_button("🔍 Check", action_id="upload_check", value=key))
    return [
        _section(f"*📥 {_esc(label)}*\nWhat should I do with it?"),
        {"type": "actions", "elements": actions},
    ]


def upload_prompt_fallback_text(label: str) -> str:
    return f"📥 {label} — apply or check?"


def format_draft_blocks(view: DraftView, *, body_in_file: bool = False) -> list[Block]:
    """Build the Block Kit draft: full e-mail, LinkedIn, and the review buttons.

    With ``body_in_file`` the e-mail text is not rendered into the message — it was
    delivered as a ``.txt`` file in the thread (one unsplit, copyable document), so
    the draft only points there.
    """
    header_line = f"📨 Application draft: {view.title}"
    blocks: list[Block] = [_header(header_line)]

    meta = [_link(view.url, "View project")] if view.url else []
    meta.append(f"*To:* {_esc(view.recipient) if view.recipient else '❓ unknown'}")
    meta.append(f"*Subject:* `{_esc(view.subject)}`")
    blocks.append(_section("\n".join(meta)))

    if body_in_file:
        blocks.append(
            _context("📄 Full e-mail: the text file below (the newest file is the current draft).")
        )
    else:
        blocks.extend(_code_sections(view.body, label="📄 E-mail (copy)"))
    blocks.extend(
        _code_sections(
            view.linkedin_message,
            label=f"💬 LinkedIn ({len(view.linkedin_message)}/{LINKEDIN_LIMIT})",
        )
    )
    # The search button rides with the LinkedIn text in every state (also after
    # sending, when the outreach actually happens).
    if view.contact_name:
        blocks.append(_linkedin_search_actions(view.contact_name, view.company))

    if view.status in (ApplicationStatus.READY, ApplicationStatus.AWAITING_EMAIL):
        blocks.append(_attachment_section(view))
        blocks.extend(_mail_client_blocks(view))
        # 📤 Send always shows, with or without a recipient: a hidden button reads as
        # a broken draft. Pressing it without an address answers with the hint instead
        # of sending (the service guards it).
        actions: list[Block] = [
            _button("📤 Send", action_id="send", value=str(view.application_id))
        ]
        # No recipient yet: offer contact research so the address can be found instead
        # of typed in blind (the button carries the listing id, not the application id).
        if not view.recipient and view.listing_id is not None:
            actions.append(
                _button("🔎 Find contact", action_id="enrich", value=str(view.listing_id))
            )
        actions.append(_button("❌ Discard", action_id="cancel", value=str(view.application_id)))
        blocks.append({"type": "actions", "elements": actions})

    hints: list[str] = []
    if view.status is ApplicationStatus.AWAITING_EMAIL:
        hints.append("❗ No recipient detected — reply in the thread with the e-mail address.")
    if view.revision_count:
        hints.append(f"🔁 Revision #{view.revision_count}")
    hints.append(
        "✏️ Reply in the thread: free text = revise the draft · a lone e-mail "
        "address = set the recipient. Copy button: top-right corner of each text block."
    )
    blocks.append(_context(" · ".join(hints)))
    return blocks


def _attachment_section(view: DraftView) -> Block:
    """What 📤 Send will attach — and, in the same line, what is missing on disk."""
    if not view.attachments and not view.missing_attachments:
        return _context("📎 No CV configured — the e-mail is sent without an attachment.")
    lines = []
    if view.attachments:
        lines.append(f"📎 *Attachments:* {_esc(', '.join(view.attachments))}")
    else:
        lines.append("📎 *Attachments:* none — no configured CV was found on disk")
    if view.missing_attachments:
        lines.append(f"⚠️ Missing in `cv/`: {_esc(', '.join(view.missing_attachments))}")
    return _context(" · ".join(lines))


def _fit_encoded(text: str, budget: int) -> str:
    """The longest whole-line prefix of ``text`` whose percent-encoding fits ``budget``."""
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        used += len(quote(line + "\n"))
        if used > budget:
            break
        kept.append(line)
    return "\n".join(kept)


def _mail_client_blocks(view: DraftView) -> list[Block]:
    """The mailto link, pre-filled with subject and as much body as it can carry.

    A mrkdwn ``mailto:`` link, not a URL button: Slack clients only open http(s)
    button URLs and silently drop a mailto click, while a link in text opens the OS
    mail client. It is always offered — a missing recipient is simply left blank and
    typed there. A mailto can never carry attachments, so the CVs only ride along
    via 📤 Send.
    """
    prefix = f"mailto:{view.recipient or ''}?subject={quote(view.subject)}&body="
    budget = _MAILTO_LIMIT - len(prefix)
    encoded = quote(view.body)
    truncated = len(encoded) > budget
    if truncated:
        encoded = quote(_fit_encoded(view.body, budget))
    blocks = [_section(_link(prefix + encoded, "📧 Open in mail client"))]
    if truncated:
        blocks.append(
            _context(
                "✉️ The mail client opens with the beginning of the letter (a mailto link "
                "is length-limited) — copy the full text from the e-mail block or text file "
                "above, and attach the CVs yourself. 📤 Send delivers everything "
                "including the attachments."
            )
        )
    return blocks


def format_contact_blocks(enrichment: ContactEnrichment) -> list[Block]:
    """Build the Block Kit message for a contact-research result (data + research links)."""
    subject = enrichment.company or enrichment.person or "listing"
    blocks: list[Block] = [_header(f"🔎 Contact research: {subject}")]

    facts = [
        _labeled("🏢 Company", enrichment.company),
        _labeled("👤 Contact", enrichment.person),
        (_link(enrichment.website, "🌐 Website") if enrichment.website else None),
        _labeled_list("🧑‍💼 Named on site", enrichment.persons, limit=5),
    ]
    facts_text = "\n".join(fact for fact in facts if fact)
    if facts_text:
        blocks.append(_section(facts_text))

    if enrichment.emails:
        blocks.extend(_code_sections("\n".join(enrichment.emails), label="📧 E-mails (best first)"))
    if enrichment.phones:
        blocks.extend(_code_sections("\n".join(enrichment.phones), label="📞 Phone"))
    if not enrichment.emails and not enrichment.phones:
        blocks.append(
            _section(
                "_No direct e-mail or phone found on the company site — "
                "use the LinkedIn/Google links below._"
            )
        )

    if enrichment.linkedin_message:
        blocks.extend(
            _code_sections(
                enrichment.linkedin_message,
                label=f"💬 LinkedIn connection message ({len(enrichment.linkedin_message)}/300)",
            )
        )

    links = enrichment.links
    link_buttons = [
        _button("🔗 Company on LinkedIn", action_id="open_li_company", url=links.linkedin_company),
        _button("👥 People on LinkedIn", action_id="open_li_people", url=links.linkedin_people),
        _button("🔍 Google contact", action_id="open_google", url=links.google_contact),
    ]
    blocks.append({"type": "actions", "elements": link_buttons})
    blocks.append(
        _context(
            "🔒 The LinkedIn/Google buttons open a search in your browser — nothing is scraped. "
            "Reply in a draft's thread with an address to set it as the recipient."
        )
    )
    return blocks


def contact_fallback_text(enrichment: ContactEnrichment) -> str:
    subject = enrichment.company or enrichment.person or "listing"
    return f"🔎 Contact research: {subject}"


def sent_confirmation_blocks(view: DraftView) -> list[Block]:
    """Thread confirmation after a send: the LinkedIn text to copy plus the search button."""
    blocks = [_section(f"✅ Application sent to *{_esc(view.recipient or '')}*")]
    blocks.extend(_code_sections(view.linkedin_message, label="💬 LinkedIn message (copy)"))
    if view.contact_name:
        blocks.append(_linkedin_search_actions(view.contact_name, view.company))
    return blocks


def sent_fallback_text(view: DraftView) -> str:
    return f"✅ Application sent to {view.recipient or ''}"


def match_fallback_text(message: MatchMessage) -> str:
    return f"🎯 New match: {message.title} ({message.score}/100)"


def match_detail_fallback_text(message: MatchMessage) -> str:
    return f"📋 Full listing: {message.title}"


def draft_fallback_text(view: DraftView) -> str:
    return f"📨 Application draft: {view.title}"


@dataclass(frozen=True, slots=True)
class PostedMessage:
    """Where a posted Slack message lives (its channel and thread timestamp)."""

    channel: str
    ts: str

    @property
    def ref(self) -> str:
        """Stable ``channel:ts`` reference stored for thread-reply routing."""
        return f"{self.channel}:{self.ts}"


class SlackResponse(Protocol):
    def get(self, key: str, default: object = None, /) -> object: ...


class SlackWebClient(Protocol):
    """The subset of ``slack_sdk`` ``AsyncWebClient`` used here (fakeable in tests)."""

    async def chat_postMessage(  # noqa: N802 - mirrors slack_sdk's method name
        self,
        *,
        channel: str,
        text: str,
        blocks: list[Block] | None = None,
        thread_ts: str | None = None,
        unfurl_links: bool = True,
        unfurl_media: bool = True,
    ) -> SlackResponse: ...

    async def chat_update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[Block] | None = None,
    ) -> SlackResponse: ...

    async def files_upload_v2(
        self,
        *,
        channel: str,
        content: str,
        filename: str,
        title: str | None = None,
        thread_ts: str | None = None,
    ) -> SlackResponse: ...


class SlackClient:
    """Posts and updates Slack messages; errors are logged and surface as ``None``."""

    def __init__(self, *, channel: str, web_client: SlackWebClient) -> None:
        self._channel = channel
        self._web = web_client

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None:
        return await self._post(text=text, blocks=blocks, thread_ts=thread_ts)

    async def post_text(self, text: str, *, thread_ts: str | None = None) -> PostedMessage | None:
        return await self._post(text=text, blocks=None, thread_ts=thread_ts)

    async def upload_text(
        self, *, content: str, filename: str, title: str, thread_ts: str | None = None
    ) -> bool:
        """Upload ``content`` as a text-file snippet — one unsplit, copyable document.

        Requires the ``files:write`` bot scope and a channel *ID* in the config
        (file uploads do not resolve channel names). Failure is reported as False so
        the caller can fall back to rendering the text inline.
        """
        try:
            response = await self._web.files_upload_v2(
                channel=self._channel,
                content=content,
                filename=filename,
                title=title,
                thread_ts=thread_ts,
            )
        except Exception as err:
            logger.warning("slack file upload failed: %s", err)
            return False
        if not response.get("ok"):
            logger.warning("slack file upload rejected: %s", response.get("error"))
            return False
        return True

    async def update_blocks(self, channel: str, ts: str, blocks: list[Block], text: str) -> bool:
        """Replace an existing message's blocks in place (draft revisions/send/cancel)."""
        try:
            response = await self._web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        except Exception as err:
            logger.warning("slack update failed: %s", err)
            return False
        if not response.get("ok"):
            logger.warning("slack update rejected: %s", response.get("error"))
            return False
        return True

    async def _post(
        self, *, text: str, blocks: list[Block] | None, thread_ts: str | None
    ) -> PostedMessage | None:
        try:
            response = await self._web.chat_postMessage(
                channel=self._channel,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                unfurl_links=False,  # no giant link previews cluttering the message
                unfurl_media=False,
            )
        except Exception as err:  # slack_sdk raises SlackApiError / transport errors
            logger.warning("slack post failed: %s", err)
            return None
        if not response.get("ok"):
            logger.warning("slack post rejected: %s", response.get("error"))
            return None
        channel = response.get("channel")
        ts = response.get("ts")
        if isinstance(channel, str) and isinstance(ts, str):
            return PostedMessage(channel=channel, ts=ts)
        return None


class SlackNotifier:
    """Pipeline notifier: posts match messages (with apply button) and warnings to Slack."""

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def send_match(self, message: MatchMessage, *, listing_id: int) -> bool:
        """Post the compact card, then the full listing as its first thread reply.

        The alert counts as delivered once the card is posted; a failing detail
        reply is logged by the client and never revokes the notification.
        """
        posted = await self._client.post_blocks(
            format_match_blocks(message, listing_id=listing_id), match_fallback_text(message)
        )
        if posted is None:
            return False
        await self._client.post_blocks(
            format_match_detail_blocks(message),
            match_detail_fallback_text(message),
            thread_ts=posted.ts,
        )
        return True

    async def send_warning(self, text: str) -> bool:
        return await self._client.post_text(text) is not None
