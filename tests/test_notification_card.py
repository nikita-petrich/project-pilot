"""Retiring a match card once the match has been taken up (skipped without Postgres)."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.models import Listing
from project_pilot.notification.card import TelegramCardRemover
from project_pilot.repository import Repository

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class _FakeNotifier:
    def __init__(self, *, deletes: bool = True) -> None:
        self.deleted: list[int] = []
        self._deletes = deletes

    async def delete_card(self, message_id: int) -> bool:
        self.deleted.append(message_id)
        return self._deletes


def _listing(url_hash: str = "h1") -> Listing:
    return Listing(
        source="freelancermap",
        external_url=f"https://example.test/{url_hash}",
        url_hash=url_hash,
        title="AI Developer",
    )


async def _notified(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """A listing whose card was pushed, with the message id recorded."""
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(_listing())
        await repo.mark_notified([listing], NOW, card_message_id=5150)
        await session.commit()
        return listing.id


async def test_the_card_is_removed_once_the_match_is_taken_up(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _notified(session_factory)
    notifier = _FakeNotifier()
    remover = TelegramCardRemover(session_factory=session_factory, notifier=notifier)

    assert await remover.remove_card(listing_id) is True
    assert notifier.deleted == [5150]


async def test_a_second_draft_does_not_ask_telegram_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Deleting is not idempotent from our side: a repeat would ask Telegram to
    # remove a message that is already gone and log a failure for nothing.
    listing_id = await _notified(session_factory)
    notifier = _FakeNotifier()
    remover = TelegramCardRemover(session_factory=session_factory, notifier=notifier)

    await remover.remove_card(listing_id)
    assert await remover.remove_card(listing_id) is False
    assert notifier.deleted == [5150]


async def test_a_listing_that_never_had_a_card_is_left_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An ingested listing was never pushed, so there is nothing to retire.
    async with session_factory() as session:
        listing, _ = await Repository(session).upsert_listing(_listing("h2"))
        await session.commit()
        listing_id = listing.id
    notifier = _FakeNotifier()

    remover = TelegramCardRemover(session_factory=session_factory, notifier=notifier)

    assert await remover.remove_card(listing_id) is False
    assert notifier.deleted == []


async def test_a_suppressed_match_records_no_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An on-site-only match is marked handled without a push; marking it must not
    # invent a message id that a later draft would try to delete.
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(_listing("h3"))
        await repo.mark_notified([listing], NOW)
        await session.commit()
        assert listing.notified_at is not None
        assert listing.card_message_id is None
