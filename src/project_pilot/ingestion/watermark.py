"""Watermark pagination stop criterion."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from project_pilot.ingestion.parser import ListingSummary


@dataclass(frozen=True, slots=True)
class PageDecision:
    new_summaries: list[ListingSummary]
    should_stop: bool


def evaluate_page(
    summaries: Iterable[ListingSummary],
    known_hashes: set[str],
    watermark: datetime | None,
) -> PageDecision:
    """Split a list page into new summaries and decide whether to stop paginating.

    Stop once a known hash or a listing older than the watermark appears: the
    "newest first" ordering means everything after it is already captured. A seed
    run (no known hashes, no watermark) processes the whole page and keeps going.
    """
    new: list[ListingSummary] = []
    should_stop = False
    for summary in summaries:
        if summary.url_hash in known_hashes:
            should_stop = True
            continue
        if (
            watermark is not None
            and summary.posted_at is not None
            and summary.posted_at < watermark
        ):
            should_stop = True
            continue
        new.append(summary)
    return PageDecision(new_summaries=new, should_stop=should_stop)
