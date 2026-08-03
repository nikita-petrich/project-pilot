"""Normalization: URL canonicalization, German date parsing, remote heuristic."""

import hashlib
import re
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from project_pilot.models import PostedPrecision, RemoteStatus

_BERLIN = ZoneInfo("Europe/Berlin")
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def canonicalize_url(url: str, base: str) -> str:
    """Resolve against ``base`` and drop the query, fragment, and trailing slash."""
    absolute = urljoin(base, url.strip())
    parts = urlsplit(absolute)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def compute_url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def parse_german_date(text: str) -> date | None:
    match = _DATE_RE.search(text)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


_DE_WORDS = frozenset(
    [
        "der",
        "die",
        "das",
        "und",
        "oder",
        "für",
        "mit",
        "von",
        "im",
        "ist",
        "eine",
        "einen",
        "wir",
        "sie",
        "werden",
        "nicht",
        "auch",
        "sind",
        "bei",
        "aus",
        "dem",
        "den",
        "zur",
        "zum",
        "sowie",
        "unserem",
        "unseren",
        "erfahrung",
        "kenntnisse",
    ]
)
_EN_WORDS = frozenset(
    [
        "the",
        "and",
        "or",
        "for",
        "with",
        "of",
        "is",
        "are",
        "we",
        "you",
        "will",
        "not",
        "this",
        "that",
        "from",
        "your",
        "our",
        "their",
        "as",
        "at",
        "have",
        "experience",
        "knowledge",
        "role",
        "requirements",
        "skills",
    ]
)


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(ag|gmbh|mbh|kg|se|ltd|limited|inc|llc|b\.?v\.?|s\.?a\.?|plc|group|holding"
    r"|consulting|solutions|services|recruit\w*|technologies|systems|hays)\b",
    re.IGNORECASE,
)
_CONTACT_LABEL_RE = re.compile(
    r"(?i:contact person|ansprechpartner(?:in)?|kontaktperson)\s*:?\s+"
    r"([A-ZÄÖÜ][a-zäöüß]+\s+[A-ZÄÖÜ][a-zäöüß]+)"
)
_REMOTE_HINT_RE = re.compile(
    r"\b(remote|homeoffice|home[ -]?office|hybrid|nearshore|offshore|mobiles arbeiten"
    r"|remote work|100\s*%\s*remote)\b",
    re.IGNORECASE,
)


def looks_like_company(name: str) -> bool:
    """True if ``name`` reads like a company (legal-form/agency suffix), not a person."""
    return bool(_COMPANY_SUFFIX_RE.search(name))


def extract_contact_person(text: str) -> str | None:
    """Pull a "First Last" contact name from a "contact person:"/"Ansprechpartner:" label."""
    if not text:
        return None
    match = _CONTACT_LABEL_RE.search(text)
    if match is None:
        return None
    name = match.group(1).strip()
    return None if looks_like_company(name) else name


def resolve_contact_name(
    first_name: str | None, last_name: str | None, company: str | None, text: str
) -> str | None:
    """Prefer the structured contact when it names a person, else pull one from ``text``.

    freelancermap's structured contact holds the real person on direct posts but the
    agency name on brokered ones; company-like values fall back to the text label.
    """
    structured = " ".join(part for part in (first_name, last_name) if part) or None
    if structured and not looks_like_company(structured) and structured != company:
        return structured
    return extract_contact_person(text)


def is_onsite_only(remote_percent: int | None, location: str | None, description: str) -> bool:
    """True only for clearly on-site roles: structured 0% remote and no remote hint anywhere."""
    if _REMOTE_HINT_RE.search(f"{location or ''} {description}"):
        return False
    return remote_percent == 0


def detect_language(text: str) -> str | None:
    """Best-effort de/en detection from stopword counts (None if no clear signal)."""
    if not text:
        return None
    lowered = text.lower()
    if any(char in lowered for char in "äöüß"):
        return "de"
    tokens = re.findall(r"[a-zäöüß]+", lowered)
    if not tokens:
        return None
    german = sum(1 for token in tokens if token in _DE_WORDS)
    english = sum(1 for token in tokens if token in _EN_WORDS)
    if german == english:
        return None
    return "de" if german > english else "en"


def remote_status_from_percent(percent: int | None) -> RemoteStatus:
    """Map freelancermap's ``remoteInPercent`` to a remote status (100=remote, 0=onsite)."""
    if percent is None:
        return RemoteStatus.UNKNOWN
    if percent >= 100:
        return RemoteStatus.REMOTE
    if percent <= 0:
        return RemoteStatus.ONSITE
    return RemoteStatus.HYBRID


def start_from_parts(
    year: int | None, month: int | None, text: str | None
) -> tuple[date | None, bool]:
    """Resolve a structured start (year+month) or free text into (start_date, start_asap)."""
    lowered = (text or "").strip().lower()
    if "sofort" in lowered or "asap" in lowered:
        return None, True
    if year and month:
        try:
            return date(year, month, 1), False
        except ValueError:
            return None, False
    return None, False


def html_to_text(html: str) -> str:
    """Flatten an HTML description fragment into whitespace-normalized plain text."""
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def next_page_url(url: str) -> str:
    """Return ``url`` with ``pagenr`` incremented (added as 2 when absent)."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    updated: list[tuple[str, str]] = []
    found = False
    for key, value in pairs:
        if key == "pagenr":
            try:
                value = str(int(value) + 1)
            except ValueError:
                value = "2"
            found = True
        updated.append((key, value))
    if not found:
        updated.append(("pagenr", "2"))
    query = urlencode(updated)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


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
