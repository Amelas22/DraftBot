"""Reading a cube's card list off CubeCobra.

The bot already stores a cube's NAME on every draft session and renders it as a
cubecobra.com link, so a cube is already a first-class identifier here -- this
adds the one thing that was missing, which is the list behind it.

`/cube/api/cubelist/<id>` returns plain names, one per line, and that is all a
deposit needs: the TradeBot records which printings actually crossed and hands
those exact copies back, so naming a printing here would be a promise this side
cannot keep and does not need to. The JSON endpoint carries printings and is
170x larger (766 KB against 4.5 KB for a 300-card cube); its per-card `mtgo_id`
is also per-PRINTING, so a card whose cube entry is a promo would read as "not
on MTGO" when the card is on MTGO perfectly well under another printing.

The plain list also leaves basics out -- they live on the cube's own `basics`
board -- which is the right default for a bot that already holds plenty.
"""
from collections import Counter
from typing import Any, Optional

import aiohttp
from loguru import logger

CUBE_LIST_URL = "https://cubecobra.com/cube/api/cubelist/{}"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def fetch_cube(cube: str) -> "Optional[list[dict[str, Any]]]":
    """`[{"name": str, "qty": int}]` for a cube, or None if it cannot be read.

    Repeats are counted rather than listed: a cube that runs four Lightning
    Bolt sends them as one item with a quantity, which is what the serve's
    items[] wants and what stops a deck of four becoming four line items.

    None rather than an empty list for a failure, because "the cube is empty"
    and "CubeCobra did not answer" lead to different messages -- one is a cube
    to fix, the other is a thing to retry.
    """
    url = CUBE_LIST_URL.format(cube.strip())
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("cube list {} -> HTTP {}", cube, resp.status)
                    return None
                text = await resp.text()
    except Exception as e:
        logger.warning("cube list {} could not be read: {}", cube, e)
        return None

    names = [line.strip() for line in text.splitlines() if line.strip()]
    if not names:
        return []
    counted = Counter(names)
    return [{"name": n, "qty": q} for n, q in counted.items()]
