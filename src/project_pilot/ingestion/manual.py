"""Manual ingestion: turn anything Nik hands to a Claude surface into a listing.

The scanner is not the only way a project reaches project-pilot. A recruiter
mails, a client sends a PDF, someone screenshots a listing, an n8n workflow
forwards a description. Those used to stay outside the database, which meant
they could be judged and drafted but never sent, reported on, or found again.

This module builds the stored row for them. Two rules make it safe:

- **Provenance is recorded, never guessed.** ``Listing.origin`` names the channel
  and ``raw["ingest"]`` keeps the detail, so a listing always says how it got here.
- **The dedupe key stays the scanner's.** A real listing URL is canonicalized and
  hashed exactly as ``ingestion.parser`` does it, so pasting a link the scanner
  later finds (or already found) updates that one row instead of forking a
  duplicate. Text with no URL is keyed by the text itself, so the same mail
  pasted twice is the same listing.

Nothing here is bound to freelancermap. The board is read off the URL (or passed
in), so a listing from another platform, a mail, or an automation is stored the
same way and only the scraper stays single-source.
"""

import hashlib
import re
from datetime import datetime

from project_pilot.ingestion.normalize import (
    canonicalize_listing_url,
    compute_url_hash,
    extract_listing_title,
    source_from_url,
)
from project_pilot.models import Listing, ListingOrigin, ListingStatus, RemoteStatus

# Stands in for the listing URL of something that has none, so `external_url`
# keeps its NOT NULL/unique contract without pretending to be fetchable.
INGEST_URL_PREFIX = "pilot://ingest/"
# The board key for a listing that carries no URL to read one off.
MANUAL_SOURCE = "manual"
_TITLE_MAX = 512
_WHITESPACE_RE = re.compile(r"\s+")


def _text_key(text: str) -> str:
    """A stable hash of the text's words, so re-pasting is not a new listing."""
    return hashlib.sha256(_WHITESPACE_RE.sub(" ", text).strip().encode("utf-8")).hexdigest()


def listing_key(*, text: str, url: str | None) -> tuple[str, str]:
    """``(external_url, url_hash)`` for an ingested listing.

    With an absolute URL the pair is identical to what the scraper would compute
    for the same page - that is what lets a pasted link and a scanned listing be
    one row. Anything else (no URL, or a bare path that could belong to any host)
    is keyed by its own text rather than guessed at.
    """
    canonical = canonicalize_listing_url(url) if url else None
    if canonical is not None:
        return canonical, compute_url_hash(canonical)
    digest = _text_key(text)
    return f"{INGEST_URL_PREFIX}{digest[:16]}", digest


def _title_for(text: str, given: str | None) -> str:
    if given and given.strip():
        return given.strip()[:_TITLE_MAX]
    heading = extract_listing_title(text)
    if heading:
        return heading[:_TITLE_MAX]
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return (first_line or "Ohne Titel")[:120]


def build_manual_listing(
    *,
    text: str,
    origin: ListingOrigin,
    now: datetime,
    title: str | None = None,
    url: str | None = None,
    source: str | None = None,
    company: str | None = None,
    note: str | None = None,
) -> Listing:
    """The unsaved ``Listing`` for one supplied project description, from any board.

    ``source`` names the platform it came from. Passed in it is taken as given;
    otherwise it is read off the URL, and text with no URL is ``manual``.
    """
    external_url, url_hash = listing_key(text=text, url=url)
    board = (source or "").strip() or (source_from_url(url) if url else None) or MANUAL_SOURCE
    ingest: dict[str, object] = {"origin": origin.value, "received_at": now.isoformat()}
    if note and note.strip():
        ingest["note"] = note.strip()
    if url and url.strip():
        ingest["supplied_url"] = url.strip()
    raw: dict[str, object] = {"ingest": ingest}
    if company and company.strip():
        raw["company"] = company.strip()
    return Listing(
        source=board[:64],
        external_url=external_url,
        url_hash=url_hash,
        title=_title_for(text, title),
        description=text.strip(),
        skills=[],
        location=None,
        remote_status=RemoteStatus.UNKNOWN,
        first_seen_at=now,
        last_seen_at=now,
        status=ListingStatus.NEW,
        origin=origin,
        raw=raw,
    )
