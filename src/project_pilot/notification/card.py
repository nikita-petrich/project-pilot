"""Retiring a match card once the match has been taken up.

A card is a decision surface. Once the decision is made it is clutter, and the
**Ablehnen** button already proves the point: a declined match disappears. The
other decision — applying — could not do the same, because Telegram reports no
press on a URL button and Bewerben has to stay a URL button to open the Claude
chat in one tap (see :mod:`project_pilot.notification.telegram`).

So the card is retired on the first unambiguous consequence of that tap instead:
the chat drafts the application, and the draft removes the card. A tap that leads
nowhere leaves the card standing, which is the outcome anyone would want.
"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.db import session_scope
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)


class CardDeleter(Protocol):
    """What this needs of a notifier: the ability to delete one message."""

    async def delete_card(self, message_id: int) -> bool: ...


class TelegramCardRemover:
    """Removes a listing's match card from the Telegram feed, at most once."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: CardDeleter,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

    async def remove_card(self, listing_id: int) -> bool:
        """Delete the card for ``listing_id``; False when there is none to delete.

        The id is taken (and cleared) before the API call, so a second draft on
        the same listing does not ask Telegram to delete a message twice.
        """
        async with session_scope(self._session_factory) as session:
            message_id = await Repository(session).take_card_message_id(listing_id)
        if message_id is None:
            return False
        deleted = await self._notifier.delete_card(message_id)
        logger.info(
            "match card for listing %s %s",
            listing_id,
            "removed" if deleted else "could not be removed",
        )
        return deleted
