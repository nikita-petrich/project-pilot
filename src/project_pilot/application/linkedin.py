"""Fitting a generated LinkedIn note into LinkedIn's connection-note limit.

The model is told to stay under the limit, but an occasional overshoot is normal.
Cutting the tail is the worst possible repair: the note ends with the booking link
and the phone number, so a tail cut leaves a half-written URL or a broken number
("… or call me: +49 1…"). Instead the *middle* is dropped — the "why I fit"
sentence, which the prompt itself names as the part to sacrifice — so the opening
(who and which project) and the ending (call to action) always survive intact.
"""

import re

# A sentence ends at .!?… followed by whitespace. URLs and phone numbers carry no
# space after their dots, so they are never split.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_BOUNDARY.split(text.strip()) if part]


def _trim_words(text: str, limit: int) -> str:
    """The longest whole-word prefix of ``text`` that fits ``limit`` (with an ellipsis)."""
    if limit <= 1:
        return ""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    if cut <= 0:
        return ""
    return text[:cut].rstrip(" ,;:-") + "…"


def fit_linkedin_message(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters while keeping its opening and ending.

    Whole sentences are dropped from the middle first; only when the opening and the
    ending alone still do not fit is the opening trimmed on a word boundary.
    """
    message = text.strip()
    if len(message) <= limit:
        return message

    parts = _sentences(message)
    if len(parts) >= 3:
        first, last, middle = parts[0], parts[-1], parts[1:-1]
        while middle:
            middle.pop()  # the filler nearest the call to action goes first
            candidate = " ".join([first, *middle, last])
            if len(candidate) <= limit:
                return candidate

    if len(parts) >= 2:
        last = parts[-1]
        head = _trim_words(" ".join(parts[:-1]), limit - len(last) - 1)
        if head:
            return f"{head} {last}"
        if len(last) <= limit:
            return last

    return _trim_words(message, limit) or message[: limit - 1].rstrip() + "…"
