"""Mechanical corrections to a generated application body.

The prompt states these rules, and the model follows them — usually. The same prompt
produced "Guten Tag X,\\n\\nich bin …" for one listing and "Ich bin …" for the next,
and a confidentiality notice as one paragraph once and hard-wrapped mid-sentence the
next time. Neither is a matter of judgment, so neither is left to chance: they are
fixed here, after generation, where the result can be tested.
"""

import re

# The notice exactly as the prompt demands it, one paragraph per language. The prompt
# shows it wrapped for readability; a test keeps these texts and the prompt in step.
CONFIDENTIALITY_NOTICES = (
    "Der Inhalt dieser E-Mail ist ausschließlich für den bezeichneten Adressaten bestimmt. "
    "Wenn Sie nicht der vorgesehene Adressat dieser E-Mail oder dessen Vertreter sein "
    "sollten, so beachten Sie bitte, dass jede Form der Kenntnisnahme, Veröffentlichung, "
    "Vervielfältigung oder Weitergabe des Inhalts dieser E-Mail unzulässig ist. Wir bitten "
    "Sie, sich in diesem Fall mit dem Absender der E-Mail in Verbindung zu setzen.",
    "The contents of this e-mail are intended solely for the named addressee. If you are "
    "not the intended recipient of this e-mail or their representative, please note that "
    "any form of review, publication, reproduction or disclosure of the contents of this "
    "e-mail is not permitted. In this case, please contact the sender of the e-mail.",
)

# "Guten Tag Talissa Blajan," / "Sehr geehrte Frau Müller," — a salutation line ending in
# a comma, then a blank line, then the first sentence.
_SALUTATION_THEN_ICH_RE = re.compile(r"\A(\s*[^\n]*,[ \t]*\n\s*\n\s*)Ich\b")


def lowercase_after_salutation(body: str) -> str:
    """ "Ich" opening the text after a comma salutation becomes "ich".

    German letter form continues the sentence after "Guten Tag …," in lower case
    unless the first word is a noun. The pronoun is never one, so this is safe to fix
    blindly; any other opening word is left alone rather than guessed at.
    """
    return _SALUTATION_THEN_ICH_RE.sub(r"\1ich", body, count=1)


def _whitespace_tolerant(text: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(word) for word in text.split()))


_NOTICE_PATTERNS = tuple(
    (_whitespace_tolerant(notice), notice) for notice in CONFIDENTIALITY_NOTICES
)


def unwrap_confidentiality_notice(body: str) -> str:
    """The notice as one paragraph, however the model broke its lines.

    Every other paragraph of the mail flows; a notice wrapped at 80 columns reads as
    broken on a phone. Only the exact notice text is touched.
    """
    for pattern, notice in _NOTICE_PATTERNS:
        body = pattern.sub(notice, body)
    return body


def end_at_confidentiality_notice(body: str) -> str:
    """Drop anything the model wrote after the notice, which the prompt makes the last block.

    Seen on a real revision: the LinkedIn note reappeared under the notice, inside the
    e-mail. Whatever follows the notice is never part of the letter, so it is cut —
    the notice itself, with the letter above it, is kept exactly.
    """
    for notice in CONFIDENTIALITY_NOTICES:
        index = body.find(notice)
        if index >= 0:
            return body[: index + len(notice)]
    return body


def tidy_body(body: str) -> str:
    """All corrections, in the order they apply to a finished draft."""
    return end_at_confidentiality_notice(
        unwrap_confidentiality_notice(lowercase_after_salutation(body))
    )
