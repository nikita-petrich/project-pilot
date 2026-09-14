"""Pure builder for the LinkedIn connection ("Vernetzungs") message.

A short, personalized German note Nik copies into a LinkedIn connection request to
the Ansprechpartner. Always produced (even from just a company), capped at
LinkedIn's connection-note length so it is never rejected as too long.
"""

import re

# LinkedIn caps a connection-request note at 300 characters.
LINKEDIN_CONNECT_LIMIT = 300


def _full_name(person: str | None) -> str | None:
    """The contact's name as given, one space between words; ``None`` when absent."""
    if not person:
        return None
    return " ".join(person.split()) or None


def _hook(company: str | None, title: str | None) -> str:
    if title and company:
        return f'ich habe Ihr Projekt "{title}" bei {company} entdeckt und finde es sehr spannend'
    if title:
        return f'ich habe Ihr Projekt "{title}" entdeckt und finde es sehr spannend'
    if company:
        return f"ich bin auf {company} aufmerksam geworden"
    return "ich bin auf Ihre Projektausschreibung aufmerksam geworden"


def _assemble(*, name: str | None, hook: str, sender: str | None, offer_du: bool) -> str:
    # Formal throughout, like the application itself: "Hallo Talissa," in front of
    # "Ihr Projekt … mit Ihnen" read as two registers in one sentence. With no
    # Herr/Frau to go on, the full name is the formal form — never guessed from a
    # first name.
    greeting = f"Guten Tag {name}," if name else "Guten Tag,"
    core = "und würde mich gerne mit Ihnen vernetzen, um mich kurz zum Projekt auszutauschen."
    # Offer first-name terms while still addressing formally — the standard, polite
    # German networking move ("gerne per Du").
    du = " Gerne auch per Du." if offer_du else ""
    sign = f" Beste Grüße, {sender}" if sender else " Beste Grüße!"
    return re.sub(r"\s+", " ", f"{greeting} {hook} {core}{du}{sign}").strip()


def build_connection_message(
    *,
    person: str | None = None,
    company: str | None = None,
    title: str | None = None,
    sender: str | None = None,
    offer_du: bool = False,
) -> str:
    """A ≤300-char LinkedIn connection note, personalized from what is known.

    With ``offer_du`` the note offers the recipient to switch to the informal "Du".
    """
    name = _full_name(person)
    message = _assemble(name=name, hook=_hook(company, title), sender=sender, offer_du=offer_du)
    if len(message) <= LINKEDIN_CONNECT_LIMIT or not title:
        return _cap(message)
    # The project title is the only unbounded part — shorten it to fit, don't cut the ask.
    overflow = len(message) - LINKEDIN_CONNECT_LIMIT
    trimmed = title[: max(8, len(title) - overflow - 1)].rstrip() + "…"
    return _cap(
        _assemble(name=name, hook=_hook(company, trimmed), sender=sender, offer_du=offer_du)
    )


def _cap(message: str) -> str:
    if len(message) <= LINKEDIN_CONNECT_LIMIT:
        return message
    return message[: LINKEDIN_CONNECT_LIMIT - 1].rstrip() + "…"
