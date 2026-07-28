"""Pure text/HTML mining for contact data: e-mails, phones, persons, contact links.

No I/O — every function takes text/HTML and returns plain data, so the heuristics
(de-obfuscation, phone shapes, German Impressum person labels) are exhaustively unit
tested without touching the network.
"""

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from project_pilot.ingestion.normalize import html_to_text, looks_like_company

# --- e-mail -----------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Addresses that are never a human contact: platform senders, samples, monitoring
# noise, and image/asset filenames that happen to contain an ``@`` (``logo@2x.png``).
_EXCLUDED_EMAIL_RE = re.compile(
    r"no-?reply|@(?:example|sentry|wixpress|freelancermap|sentry\.io)\.|"
    r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js)$",
    re.IGNORECASE,
)
_AT_BRACKET_RE = re.compile(r"\s*[\[({]\s*at\s*[\])}]\s*", re.IGNORECASE)
_DOT_BRACKET_RE = re.compile(r"\s*[\[({]\s*(?:dot|punkt)\s*[\])}]\s*", re.IGNORECASE)
# ``name at domain dot tld`` written out in words — only rewritten as a whole, so a
# stray " at " in prose (without the trailing " dot <tld>") is left untouched.
_SPELLED_EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9.-]+)\s+(?:dot|punkt)\s+([A-Za-z]{2,})",
    re.IGNORECASE,
)

# Local-part hints that mark a usable role mailbox, most useful first.
_ROLE_HINTS: tuple[str, ...] = (
    "bewerbung",
    "jobs",
    "karriere",
    "career",
    "recruit",
    "hr",
    "personal",
    "kontakt",
    "contact",
    "info",
    "office",
    "mail",
    "hello",
    "hallo",
    "welcome",
)


def deobfuscate(text: str) -> str:
    """Turn common anti-scrape spellings (``info [at] x [dot] de``) back into e-mails."""
    text = _AT_BRACKET_RE.sub("@", text)
    text = _DOT_BRACKET_RE.sub(".", text)
    return _SPELLED_EMAIL_RE.sub(r"\1@\2.\3", text)


def extract_emails(text: str) -> list[str]:
    """All plausible human contact addresses, lower-cased and de-duplicated in order."""
    seen: dict[str, None] = {}
    for match in _EMAIL_RE.finditer(deobfuscate(text)):
        candidate = match.group(0).rstrip(".").lower()
        if _EXCLUDED_EMAIL_RE.search(candidate):
            continue
        seen.setdefault(candidate, None)
    return list(seen)


def _role_priority(email: str) -> int:
    local = email.split("@", 1)[0]
    for index, hint in enumerate(_ROLE_HINTS):
        if hint in local:
            return index
    return len(_ROLE_HINTS)


def _person_tokens(person: str) -> list[str]:
    return [token for token in re.split(r"[\s.]+", person.lower()) if len(token) > 2]


def rank_emails(emails: list[str], person: str | None) -> list[str]:
    """Order addresses best-first: person-name match, then role mailbox, then rest.

    Stable within a tier, so the original discovery order breaks ties.
    """
    tokens = _person_tokens(person) if person else []

    def sort_key(item: tuple[int, str]) -> tuple[int, int, int]:
        index, email = item
        local = email.split("@", 1)[0]
        person_match = 0 if tokens and any(token in local for token in tokens) else 1
        return (person_match, _role_priority(email), index)

    return [email for _, email in sorted(enumerate(emails), key=sort_key)]


# --- phone ------------------------------------------------------------------

_PHONE_RE = re.compile(r"(?<![\w+])(\+?\d[\d\s()/.\-]{6,}\d)")
_DATE_RE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")


def _normalize_phone(raw: str) -> tuple[str, str] | None:
    """Return (display, dedupe-key) for a plausible phone, or ``None`` to reject it."""
    cleaned = raw.strip().strip(".-/ ")
    if _DATE_RE.match(cleaned):
        return None
    plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    if not 7 <= len(digits) <= 15:
        return None
    # German/international numbers begin with a trunk 0 or a country-code '+'/'00';
    # requiring that discards years, IDs, and prices that the loose regex catches.
    if not (plus or digits.startswith("0")):
        return None
    display = re.sub(r"\s+", " ", re.sub(r"[()/]", " ", cleaned)).strip()
    key = ("+" if plus else "") + digits
    return display, key


def extract_phones(text: str) -> list[str]:
    """All plausible phone numbers, de-duplicated by digits, first spelling kept."""
    seen: dict[str, str] = {}
    for match in _PHONE_RE.finditer(text):
        normalized = _normalize_phone(match.group(1))
        if normalized is None:
            continue
        display, key = normalized
        seen.setdefault(key, display)
    return list(seen.values())


# --- persons ----------------------------------------------------------------

_NAME = r"(?:Dr\.\s*|Prof\.\s*)?[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+){1,2}"
_PERSON_LABEL_RE = re.compile(
    r"(?:Gesch[äa]ftsf[üu]hrer(?:in)?|Inhaber(?:in)?|Vorstand|Vertreten durch|"
    r"Vertretungsberechtigt\w*|Ansprechpartner(?:in)?|Kontaktperson|Contact person)"
    r"\s*:?\s*(" + _NAME + r")"
)


def extract_persons(text: str) -> list[str]:
    """Contact-person names behind German Impressum labels (Geschäftsführer, …)."""
    seen: dict[str, None] = {}
    for match in _PERSON_LABEL_RE.finditer(text):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        # Look a few chars past the capture so a name pattern that truncated a legal
        # form ("Muster Gmb|H") is still recognized as a company and dropped.
        window = text[match.start(1) : match.end(1) + 4]
        if not looks_like_company(name) and not looks_like_company(window):
            seen.setdefault(name, None)
    return list(seen)


# --- contact-page discovery -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContactLink:
    """One on-site page worth reading for contact data, with its fetch priority."""

    kind: str
    url: str
    priority: int


# kind -> (priority, url-or-text needles). Lower priority is fetched first.
_LINK_KINDS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("impressum", 0, ("impressum", "imprint", "legal-notice", "legal_notice", "legal")),
    ("kontakt", 1, ("kontakt", "contact")),
    ("team", 2, ("team", "ueber-uns", "über-uns", "about", "unternehmen", "mitarbeiter", "people")),
    ("karriere", 3, ("karriere", "career", "jobs", "stellen", "join")),
)


def _classify(haystack: str) -> tuple[str, int] | None:
    for kind, priority, needles in _LINK_KINDS:
        if any(needle in haystack for needle in needles):
            return kind, priority
    return None


def find_contact_links(html: str, base_url: str) -> list[ContactLink]:
    """Same-site Impressum/Kontakt/Team/Karriere links, de-duped and priority-ordered."""
    base_host = urlsplit(base_url).netloc.lower()
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, ContactLink] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower() not in ("", base_host):
            continue  # stay on the company's own site
        classified = _classify(f"{parts.path.lower()} {anchor.get_text(' ', strip=True).lower()}")
        if classified is None:
            continue
        kind, priority = classified
        canonical = parts._replace(fragment="").geturl()
        found.setdefault(canonical, ContactLink(kind=kind, url=canonical, priority=priority))
    return sorted(found.values(), key=lambda link: (link.priority, link.url))


# --- whole-page scan --------------------------------------------------------

_MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.IGNORECASE)
_TEL_RE = re.compile(r"tel:([+0-9][0-9\s()/.\-]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PageContacts:
    """Contact data mined from one page."""

    emails: list[str]
    phones: list[str]
    persons: list[str]


def scan_html(html: str) -> PageContacts:
    """Pull e-mails, phones, and persons from a page's ``mailto:``/``tel:`` links and text."""
    text = html_to_text(html)
    emails = extract_emails("\n".join(_MAILTO_RE.findall(html)) + "\n" + text)
    phones = extract_phones("\n".join(_TEL_RE.findall(html)) + "\n" + text)
    persons = extract_persons(text)
    return PageContacts(emails=emails, phones=phones, persons=persons)
