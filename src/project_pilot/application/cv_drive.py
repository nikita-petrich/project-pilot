"""Fetch the application CVs from a public Google Drive folder, by name.

The CVs used to be committed under ``cv/`` and baked into the image, so updating
one meant a commit and a redeploy. Instead they now live once in a public Drive
folder, and this refreshes a local cache from there before each draft and each
send — so swapping a CV in Drive is the whole update, no deploy.

Drive offers no credential-free way to list a public folder through its API, so
the listing comes from the folder's ``embeddedfolderview`` page — the same list
Drive renders for an embedded folder. It is not a documented API; if Google ever
changes its shape the parse below finds nothing and the refresh becomes a no-op
that keeps the last cached file. The whole refresh is best-effort and never
raises: a Drive hiccup must never block a draft or a send, which fall back on the
last good cached CV. The download is the ordinary ``uc?export=download`` endpoint,
whose 303 to ``drive.usercontent.google.com`` httpx follows.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_LISTING_URL = "https://drive.google.com/embeddedfolderview?id={folder_id}"
_DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id={file_id}"

# Each file in an ``embeddedfolderview`` listing is a
#   <div class="flip-entry" id="entry-<id>"> … <div class="flip-entry-title"><name></div>
# so the id lives on the entry container and the name in a nested title div.
_ENTRY_ID = re.compile(r'id="entry-([A-Za-z0-9_-]+)"')
_ENTRY_TITLE = re.compile(r'class="flip-entry-title">([^<]+)<')

# A CV PDF is a few megabytes; this bounds a runaway or wrong response.
_MAX_PDF_BYTES = 25_000_000
# The PDF magic number: anything else (an HTML error or login page) must never
# overwrite a good cached file.
_PDF_MAGIC = b"%PDF"


def parse_folder_listing(html: str) -> dict[str, str]:
    """Map each file name to its Drive id from an ``embeddedfolderview`` page.

    Splitting on the entry container scopes each id to its own title, so a
    malformed entry drops out instead of pairing with the following entry's name.
    """
    listing: dict[str, str] = {}
    for chunk in html.split('class="flip-entry"')[1:]:
        id_match = _ENTRY_ID.search(chunk)
        title_match = _ENTRY_TITLE.search(chunk)
        if id_match is None or title_match is None:
            continue
        # First listed wins, so a duplicate name resolves deterministically.
        listing.setdefault(title_match.group(1), id_match.group(1))
    return listing


class CvRefresher(Protocol):
    """Refreshes the local CV cache from its source (best-effort, never raises)."""

    async def refresh(self) -> None: ...


class DriveCvRefresher:
    """Downloads the target CVs from a public Drive folder into their local paths.

    Each target's file name is looked up in the folder, so replacing a CV is just
    dropping a file of the same name into Drive. Every step is best-effort: a folder
    that is unreachable, a name that is absent, or a body that is not a PDF leaves
    the existing cached file untouched, so a send always has the last good CV.
    """

    def __init__(self, *, folder_id: str, targets: Sequence[Path], timeout: float = 15.0) -> None:
        self._folder_id = folder_id
        self._targets = tuple(targets)
        self._timeout = timeout

    async def refresh(self) -> None:
        """Refresh every configured CV file from the Drive folder, in place."""
        if not self._targets:
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                listing = await self._listing(client)
                if not listing:
                    return
                for target in self._targets:
                    await self._refresh_one(client, listing, target)
        except httpx.HTTPError as err:
            # Best-effort: a Drive outage must never block a draft or a send.
            logger.warning("CV refresh from Drive failed, keeping the cache: %s", err)

    async def _listing(self, client: httpx.AsyncClient) -> dict[str, str]:
        response = await client.get(_LISTING_URL.format(folder_id=self._folder_id))
        response.raise_for_status()
        listing = parse_folder_listing(response.text)
        if not listing:
            logger.warning("Drive folder listing parsed no files; keeping the CV cache")
        return listing

    async def _refresh_one(
        self, client: httpx.AsyncClient, listing: dict[str, str], target: Path
    ) -> None:
        file_id = listing.get(target.name)
        if file_id is None:
            logger.warning("CV %s is not in the Drive folder; keeping the cache", target.name)
            return
        data = await self._download(client, file_id)
        if data is not None:
            await asyncio.to_thread(_write_atomic, target, data)

    async def _download(self, client: httpx.AsyncClient, file_id: str) -> bytes | None:
        response = await client.get(_DOWNLOAD_URL.format(file_id=file_id))
        response.raise_for_status()
        data = response.content
        if len(data) > _MAX_PDF_BYTES:
            logger.warning("Drive file %s exceeds the size budget; keeping the cache", file_id)
            return None
        if not data.startswith(_PDF_MAGIC):
            # An HTML interstitial or error page rather than the PDF itself.
            logger.warning("Drive file %s did not return a PDF; keeping the cache", file_id)
            return None
        return data


def _write_atomic(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` via a temp file and rename, so no reader sees a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
