"""Oracle text, type line and P/T for a set of Scryfall card ids.

Draftmancer's `carddata` carries name, mana cost, cmc, colors, bare type and
subtypes -- but no oracle text, no power/toughness, and no combined type line.
It does carry the Scryfall `id`, so everything missing is one lookup away.

Fetched in bulk (`/cards/collection`, 75 identifiers per POST). There is no
disk cache: the bot builds well under one page a day, so a page costs about
four requests and caching would buy nothing while adding an absolute path and
a corrupt-cache branch to reason about.

Follows the same Scryfall etiquette as helpers/card_image_fetcher.py -- a real
User-Agent and <=10 req/s -- because this hits the API, not the image CDN.

Degrades rather than raises: a failed batch costs those cards their oracle
text, and the page renders without it.
"""
import asyncio
import time
from typing import Any, Iterable

import aiohttp
from loguru import logger

ENDPOINT = "https://api.scryfall.com/cards/collection"
BATCH = 75
_MIN_INTERVAL = 0.11          # <=10 req/s, per Scryfall's guidance
_TIMEOUT = aiohttp.ClientTimeout(total=30)
HEADERS = {
    "User-Agent": "DraftBot/1.0 (draft table viewer)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_next_slot = 0.0  # time.monotonic() when the next API call may go


async def _throttle() -> None:
    """Reserve the next api.scryfall.com slot and sleep until it arrives.

    The read and the reserve have no await between them, so concurrent builds
    cannot grab the same slot on a single-threaded event loop.
    """
    global _next_slot
    now = time.monotonic()
    wait = _next_slot - now
    _next_slot = max(now, _next_slot) + _MIN_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)


async def _fetch_collection(ids: list[str], session: Any) -> dict[str, Any]:
    """One POST to /cards/collection. Seam for tests to replace.

    Takes the session rather than opening its own so a page's several batches
    share one connection instead of paying a TLS handshake apiece.
    """
    await _throttle()
    body = {"identifiers": [{"id": i} for i in ids]}
    async with session.post(ENDPOINT, json=body, headers=HEADERS) as resp:
        resp.raise_for_status()
        return await resp.json()


def _faces(card: dict[str, Any]) -> list[dict[str, str]]:
    """Oracle text/type/PT per face, so a DFC keeps both halves.

    A double-faced card has no top-level oracle_text; its two faces live in
    card_faces. Single-faced cards come back as a one-element list so the
    renderer has exactly one shape to handle.
    """
    faces = card.get("card_faces") or [card]
    out: list[dict[str, str]] = []
    for face in faces:
        pt = ""
        if face.get("power") is not None and face.get("toughness") is not None:
            pt = f"{face['power']}/{face['toughness']}"
        elif face.get("loyalty") is not None:
            pt = f"[{face['loyalty']}]"
        out.append({
            "name": face.get("name") or "",
            "cost": face.get("mana_cost") or "",
            "typeLine": face.get("type_line") or "",
            "oracle": face.get("oracle_text") or "",
            "pt": pt,
        })
    return out


async def enrich(card_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{scryfall_id: {"faces": [...]}} for every id that could be resolved.

    Ids that fail to resolve -- or whose batch errored -- are simply absent, so
    a caller reads this with .get() and renders what it has.
    """
    wanted = sorted({i for i in card_ids if i})
    resolved: dict[str, dict[str, Any]] = {}
    failed = 0

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            try:
                payload = await _fetch_collection(chunk, session)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError,
                    ValueError) as exc:
                failed += len(chunk)
                logger.warning(
                    f"scryfall: batch of {len(chunk)} failed ({exc}) -- skipped")
                continue
            for card in payload.get("data", []):
                resolved[card["id"]] = {"faces": _faces(card)}

    logger.info(f"scryfall: {len(resolved)} resolved, {failed} unavailable, "
                f"{len(wanted)} requested")
    return resolved
