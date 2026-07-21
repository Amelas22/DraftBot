"""Shared, retry-aware card-image fetch for quiz composites.

Walks a URL ladder (captured printing -> DFC face -> Scryfall by-UUID ->
Scryfall by-name = a different printing) and retries transient failures with
exponential backoff, honouring an optional wall-clock deadline. Returns a PIL
image, or None only after the whole ladder is exhausted (or the deadline
passes)."""

import asyncio
import time
from io import BytesIO
from typing import List, Optional
from urllib.parse import quote

import aiohttp
from loguru import logger
from PIL import Image

SCRYFALL_HEADERS = {
    "User-Agent": "DraftBot/1.0 (quiz card images)",
    "Accept": "image/*",
}
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


def build_image_url_ladder(card_id: str, carddata: dict) -> List[str]:
    """Ordered, de-duplicated candidate image URLs for one card."""
    urls: List[str] = []
    info = carddata.get(card_id) or {}

    image_uris = info.get("image_uris") or {}
    if image_uris.get("normal"):
        urls.append(image_uris["normal"])

    faces = info.get("card_faces") or []
    if faces:
        face_uris = (faces[0] or {}).get("image_uris") or {}
        if face_uris.get("normal"):
            urls.append(face_uris["normal"])

    urls.append(f"https://api.scryfall.com/cards/{card_id}?format=image&version=normal")

    name = info.get("name")
    if name:
        urls.append(
            f"https://api.scryfall.com/cards/named?exact={quote(name)}"
            "&format=image&version=normal"
        )

    return list(dict.fromkeys(urls))


async def fetch_card_image(
    session: aiohttp.ClientSession,
    card_id: str,
    carddata: dict,
    *,
    max_retries: int = 3,
    base_delay: float = 0.5,
    timeout: int = 10,
    deadline: Optional[float] = None,
) -> Optional[Image.Image]:
    """Fetch one card image, retrying transient failures with exponential
    backoff and falling through the URL ladder. `deadline` is an absolute
    time.monotonic() timestamp; once passed the fetch gives up immediately."""
    for url in build_image_url_ladder(card_id, carddata):
        headers = SCRYFALL_HEADERS if "api.scryfall.com" in url else None
        for attempt in range(max_retries):
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(f"[card-image] deadline exceeded fetching {card_id}")
                return None
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        body = await resp.read()
                        try:
                            return Image.open(BytesIO(body))
                        except Exception as e:
                            logger.warning(
                                f"[card-image] undecodable image body for {card_id} "
                                f"at {url}: {e}"
                            )
                            break  # treat as a failed rung -> next rung
                    if resp.status in _TRANSIENT_STATUSES:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(base_delay * (2 ** attempt))
                        continue
                    break  # definitive failure for this rung -> next rung
            except (asyncio.TimeoutError, aiohttp.ClientError):
                if attempt < max_retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
                continue
    logger.warning(f"[card-image] all rungs exhausted for {card_id}")
    return None
