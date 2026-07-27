"""Pure builder for the LinkedIn connection ("Vernetzungs") message.

A short, personalized German note Nik copies into a LinkedIn connection request to
the Ansprechpartner. Always produced (even from just a company), capped at
LinkedIn's connection-note length so it is never rejected as too long.
"""

import re

# LinkedIn caps a connection-request note at 300 characters.
LINKEDIN_CONNECT_LIMIT = 300

_TITLES = frozenset({"dr", "prof", "dipl", "ing", "msc", "bsc"})
_NAME_TOKEN_RE = re.compile(r"[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß'-]*")


def _first_name(person: str | None) -> str | None:
    if not person:
        return None
    for token in _NAME_TOKEN_RE.findall(person):
        name = str(token)
        if name.lower().strip(".") not in _TITLES:
            return name
    return None


def _hook(company: str | None, title: str | None) -> str:
    if title and company:
        return f'ich habe Ihr Projekt "{title}" bei {company} entdeckt und finde es sehr spannend'
    if title:
        return f'ich habe Ihr Projekt "{title}" entdeckt und finde es sehr spannend'
    if company:
        return f"ich bin auf {company} aufmerksam geworden"
    return "ich bin auf Ihre Projektausschreibung aufmerksam geworden"


def _assemble(*, first: str | None, hook: str, sender: str | None) -> str:
    greeting = f"Hallo {first}," if first else "Hallo,"
    core = "und würde mich gerne mit Ihnen vernetzen, um mich kurz zum Projekt auszutauschen."
    sign = f" Beste Grüße, {sender}" if sender else " Beste Grüße!"
    return re.sub(r"\s+", " ", f"{greeting} {hook} {core}{sign}").strip()


def build_connection_message(
    *,
    person: str | None = None,
    company: str | None = None,
    title: str | None = None,
    sender: str | None = None,
) -> str:
    """A ≤300-char LinkedIn connection note, personalized from what is known."""
    first = _first_name(person)
    message = _assemble(first=first, hook=_hook(company, title), sender=sender)
    if len(message) <= LINKEDIN_CONNECT_LIMIT or not title:
        return _cap(message)
    # The project title is the only unbounded part — shorten it to fit, don't cut the ask.
    overflow = len(message) - LINKEDIN_CONNECT_LIMIT
    trimmed = title[: max(8, len(title) - overflow - 1)].rstrip() + "…"
    return _cap(_assemble(first=first, hook=_hook(company, trimmed), sender=sender))


def _cap(message: str) -> str:
    if len(message) <= LINKEDIN_CONNECT_LIMIT:
        return message
    return message[: LINKEDIN_CONNECT_LIMIT - 1].rstrip() + "…"
