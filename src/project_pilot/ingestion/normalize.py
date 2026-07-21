"""Normalization: URL canonicalization, German date parsing, remote heuristic."""

import hashlib
import re
from datetime import UTC, date, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from project_pilot.models import PostedPrecision, RemoteStatus

_BERLIN = ZoneInfo("Europe/Berlin")
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_NEIN_RE = re.compile(r"\bnein\b")


def canonicalize_url(url: str, base: str) -> str:
    """Resolve against ``base`` and drop the query, fragment, and trailing slash."""
    absolute = urljoin(base, url.strip())
    parts = urlsplit(absolute)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def compute_url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def resolve_url(url: str, base: str) -> str:
    """Resolve ``url`` against ``base`` keeping its query (for pagination links)."""
    return urljoin(base, url.strip())


def parse_german_date(text: str) -> date | None:
    match = _DATE_RE.search(text)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_start(text: str) -> tuple[date | None, bool]:
    """Return (start_date, start_asap): "ab sofort" -> asap, "keine Angabe" -> neither."""
    lowered = text.strip().lower()
    if not lowered or "keine angabe" in lowered:
        return None, False
    if "sofort" in lowered or "asap" in lowered:
        return None, True
    return parse_german_date(text), False


def parse_end(text: str) -> date | None:
    lowered = text.strip().lower()
    if not lowered or "keine angabe" in lowered or "offen" in lowered:
        return None
    return parse_german_date(text)


def remote_status_from_text(text: str) -> RemoteStatus:
    lowered = text.lower()
    if "hybrid" in lowered:
        return RemoteStatus.HYBRID
    if "vor ort" in lowered or "onsite" in lowered or _NEIN_RE.search(lowered):
        return RemoteStatus.ONSITE
    if "remote" in lowered or "homeoffice" in lowered or "home office" in lowered:
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


def parse_posted(
    time_datetime: str | None, date_text: str | None
) -> tuple[datetime | None, PostedPrecision]:
    """Prefer a machine-readable timestamp (minute); else a German date (day)."""
    if time_datetime:
        try:
            parsed = datetime.fromisoformat(time_datetime)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_BERLIN)
            return parsed.astimezone(UTC), PostedPrecision.MINUTE
    if date_text:
        day = parse_german_date(date_text)
        if day is not None:
            midnight = datetime(day.year, day.month, day.day, tzinfo=_BERLIN)
            return midnight.astimezone(UTC), PostedPrecision.DAY
    return None, PostedPrecision.UNKNOWN
