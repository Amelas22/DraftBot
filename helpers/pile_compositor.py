"""Mana-value pile-view composite for a drafted pool.

Buckets a pool by mana value (with a dedicated Lands column) and renders each
bucket as an overlapping vertical stack: every card but the bottom one is
offset down by a name-bar height so its name/mana cost shows, and the bottom
card shows full art. The composed image stacks the main deck block, a
labeled SIDEBOARD divider, and the sideboard block vertically. Fetches art
via the shared retry-aware fetcher and is all-or-nothing (any unfetchable
card -> None)."""

import asyncio
import time
from io import BytesIO
from typing import List, Optional, Tuple

import aiohttp
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from helpers.card_image_fetcher import fetch_card_image

PILE_COMPOSITE_DEADLINE_SECONDS = 45
_MV_COLUMNS = ["0", "1", "2", "3", "4", "5", "6", "7+"]


def _is_land(info: dict) -> bool:
    return info.get("mana_cost", "") == "" and (info.get("cmc") or 0) == 0


def _mv_label(info: dict) -> str:
    cmc = int(info.get("cmc") or 0)
    return "7+" if cmc >= 7 else str(cmc)


def bucket_cards(card_ids: List[str], carddata: dict) -> List[Tuple[str, List[str]]]:
    """Ordered (column_label, [card_id,...]) for non-empty columns only.
    Column order: Lands, 0..6, 7+. Cards within a column are sorted by name."""
    column_order = ["Lands", *_MV_COLUMNS]
    buckets = {label: [] for label in column_order}
    for cid in card_ids:
        info = carddata.get(cid)
        # A card absent from carddata entirely defaults to MV0, not Lands —
        # an empty info dict would otherwise trivially satisfy the land rule.
        label = "Lands" if info is not None and _is_land(info) else _mv_label(info or {})
        buckets[label].append(cid)

    def _name(cid):
        return ((carddata.get(cid) or {}).get("name") or "").lower()

    ordered = []
    for label in column_order:
        cids = buckets[label]
        if cids:
            ordered.append((label, sorted(cids, key=_name)))
    return ordered


class PileImageBuilder:
    """Renders one deck's pool as an MV-bucketed overlapping pile image."""

    def __init__(self, card_width: int = 160, card_height: int = 223,
                 name_bar_ratio: float = 0.18, border: int = 8):
        self.card_width = card_width
        self.card_height = card_height
        self.name_bar = max(1, int(card_height * name_bar_ratio))
        self.border = border

    async def build(self, main_ids: List[str], side_ids: List[str], carddata: dict) -> Optional[BytesIO]:
        main_cols = bucket_cards(main_ids, carddata)
        side_cols = bucket_cards(side_ids, carddata)
        if not main_cols and not side_cols:
            logger.error("[pile] no cards to render")
            return None

        deadline = time.monotonic() + PILE_COMPOSITE_DEADLINE_SECONDS
        semaphore = asyncio.Semaphore(10)

        async def _one(session, cid):
            async with semaphore:
                return await fetch_card_image(
                    session, cid, carddata, deadline=deadline
                )

        # Fetch every unique card once (a pool can repeat a card_id).
        unique_ids = list({cid for cols in (main_cols, side_cols) for _, cids in cols for cid in cids})
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *(_one(session, cid) for cid in unique_ids),
                return_exceptions=True,
            )

        images = {}
        for cid, img in zip(unique_ids, results):
            if isinstance(img, Exception) or img is None:
                logger.error(f"[pile] card {cid} unfetchable; aborting (all-or-nothing)")
                return None
            images[cid] = img

        try:
            return await asyncio.to_thread(self._render_deck, main_cols, side_cols, images)
        except Exception as e:
            logger.error(f"[pile] render failed: {e}", exc_info=True)
            return None

    def _render_columns(self, columns, images) -> Image.Image:
        """One block of MV pile columns -> a PIL canvas (was the body of _render)."""
        cw, ch, nb, b = self.card_width, self.card_height, self.name_bar, self.border

        def col_height(cids):
            return ch + nb * (len(cids) - 1)

        canvas_w = b + len(columns) * (cw + b)
        canvas_h = b + max(col_height(cids) for _, cids in columns) + b
        canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))

        for col_idx, (_label, cids) in enumerate(columns):
            x = b + col_idx * (cw + b)
            for row_idx, cid in enumerate(cids):
                img = images[cid]
                if img.size != (cw, ch):
                    img = img.resize((cw, ch), Image.Resampling.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                y = b + row_idx * nb
                canvas.paste(img, (x, y))

        return canvas

    def _divider(self, width: int, text: str) -> Image.Image:
        """A labeled divider strip separating the main deck from the sideboard."""
        h = 28
        strip = Image.new("RGB", (width, h), (40, 40, 40))
        draw = ImageDraw.Draw(strip)
        draw.text((self.border, 7), text, fill=(230, 230, 230), font=ImageFont.load_default())
        return strip

    def _render_deck(self, main_cols, side_cols, images) -> BytesIO:
        main_block = self._render_columns(main_cols, images) if main_cols else None
        side_block = self._render_columns(side_cols, images) if side_cols else None

        present = [bl for bl in (main_block, side_block) if bl is not None]
        total_w = max(max(bl.width for bl in present), self.border * 2)

        blocks = []
        if main_block is not None:
            blocks.append(main_block)
        if side_block is not None:
            blocks.append(self._divider(total_w, "SIDEBOARD"))
            blocks.append(side_block)

        total_h = sum(bl.height for bl in blocks)
        canvas = Image.new("RGB", (total_w, total_h), (0, 0, 0))
        y = 0
        for bl in blocks:
            canvas.paste(bl, (0, y))
            y += bl.height

        out = BytesIO()
        canvas.save(out, format="JPEG", quality=85, optimize=True)
        out.seek(0)
        return out
