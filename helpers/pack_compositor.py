"""
Pack Compositor - Generate visual composite images of MTG card packs for quiz display.

This module downloads Scryfall card images and composites them into a grid layout.
"""

import asyncio
import time
import aiohttp
from io import BytesIO
from PIL import Image
from typing import Optional, List
from loguru import logger

from helpers.card_image_fetcher import fetch_card_image

PACK_COMPOSITE_DEADLINE_SECONDS = 45


class PackCompositor:
    """Creates composite images of card packs for visual display."""

    def __init__(self, card_width: int = 244, card_height: int = 340, border_pixels: int = 5):
        """
        Initialize the compositor with card dimensions.

        Args:
            card_width: Width of each card image (Scryfall normal size: 244px)
            card_height: Height of each card image (Scryfall normal size: 340px)
            border_pixels: Pixels of spacing between cards
        """
        self.card_width = card_width
        self.card_height = card_height
        self.border = border_pixels

    async def download_card_image(
        self,
        session: aiohttp.ClientSession,
        card_id: str,
        carddata: dict,
        timeout: int = 10,
        deadline: Optional[float] = None,
    ) -> Optional[Image.Image]:
        """Download one card image via the shared retry-aware fetcher."""
        return await fetch_card_image(
            session, card_id, carddata, timeout=timeout, deadline=deadline
        )

    async def create_pack_composite(
        self,
        pack_card_ids: List[str],
        carddata: dict,
        timeout: int = 10
    ) -> Optional[BytesIO]:
        """
        Create a composite image of all cards in a pack.

        Args:
            pack_card_ids: List of 15 card UUIDs in the pack
            carddata: The draft's carddata dictionary
            timeout: Download timeout in seconds

        Returns:
            BytesIO containing PNG image data, or None on failure
        """
        try:
            if len(pack_card_ids) != 15:
                logger.warning(f"Expected 15 cards in pack, got {len(pack_card_ids)}")
                # Continue anyway, will handle missing cards

            # Download all card images in parallel, all-or-nothing under a deadline
            deadline = time.monotonic() + PACK_COMPOSITE_DEADLINE_SECONDS
            semaphore = asyncio.Semaphore(10)

            async def _one(session, card_id):
                async with semaphore:
                    return await self.download_card_image(
                        session, card_id, carddata, timeout, deadline
                    )

            async with aiohttp.ClientSession() as session:
                results = await asyncio.gather(
                    *(_one(session, cid) for cid in pack_card_ids),
                    return_exceptions=True,
                )

            valid_images = []
            for i, img in enumerate(results):
                if isinstance(img, Exception) or img is None:
                    logger.error(
                        f"[pack-composite] card {pack_card_ids[i]} (index {i}) unfetchable "
                        f"({'exception' if isinstance(img, Exception) else 'none'}); "
                        f"aborting composite (all-or-nothing)"
                    )
                    return None
                valid_images.append((i, img))

            logger.info(f"Successfully downloaded {len(valid_images)}/15 card images")

            return await asyncio.to_thread(self._render_grid, valid_images)

        except Exception as e:
            logger.error(f"Error creating pack composite: {e}", exc_info=True)
            return None

    def _render_grid(self, valid_images: List[tuple]) -> BytesIO:
        """Synchronous compositing + JPEG encoding (offloaded via asyncio.to_thread
        so the PIL canvas build and .save() don't block the event loop).

        valid_images: [(index, PIL.Image), ...] positioned into a 5x3 grid.
        """
        # Create composite image
        # Layout: 5 cards wide × 3 cards tall
        cols = 5
        rows = 3

        # Calculate canvas size
        canvas_width = (cols * self.card_width) + ((cols + 1) * self.border)
        canvas_height = (rows * self.card_height) + ((rows + 1) * self.border)

        # Create blank canvas with black background
        canvas = Image.new('RGB', (canvas_width, canvas_height), color=(0, 0, 0))

        # Paste each card image onto canvas
        for index, img in valid_images:
            # Calculate grid position (0-indexed)
            row = index // cols
            col = index % cols

            # Calculate pixel position
            x = self.border + (col * (self.card_width + self.border))
            y = self.border + (row * (self.card_height + self.border))

            # Resize image if needed
            if img.size != (self.card_width, self.card_height):
                img = img.resize((self.card_width, self.card_height), Image.Resampling.LANCZOS)

            # Paste onto canvas
            canvas.paste(img, (x, y))

        # Save to BytesIO
        # Use JPEG with quality=85 to reduce file size significantly
        # PNG was too large (2.4MB) and caused Discord upload timeouts
        output = BytesIO()

        # Convert RGBA to RGB for JPEG (JPEG doesn't support transparency)
        if canvas.mode == 'RGBA':
            # Create white background
            rgb_canvas = Image.new('RGB', canvas.size, (255, 255, 255))
            rgb_canvas.paste(canvas, mask=canvas.split()[3] if len(canvas.split()) == 4 else None)
            canvas = rgb_canvas

        canvas.save(output, format='JPEG', quality=85, optimize=True)
        output.seek(0)

        logger.info(f"Successfully created pack composite: {canvas_width}x{canvas_height}px, size: {len(output.getvalue())} bytes")
        return output
