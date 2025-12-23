"""
Pack Compositor - Generate visual composite images of MTG card packs for quiz display.

This module downloads Scryfall card images and composites them into a grid layout.
"""

import asyncio
import aiohttp
from io import BytesIO
from PIL import Image
from typing import Optional, List
from loguru import logger


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

    def get_scryfall_image_url(self, card_id: str, carddata: dict) -> Optional[str]:
        """
        Get Scryfall image URL for a card.

        First tries to extract from carddata if available, otherwise constructs
        the URL directly from the Scryfall card ID using Scryfall's API.

        Args:
            card_id: The Scryfall card UUID
            carddata: The draft's carddata dictionary

        Returns:
            Image URL string or None if not found
        """
        try:
            # First, try to get from carddata if it exists
            card_info = carddata.get(card_id)
            if card_info:
                # Try to get normal image URL
                image_uris = card_info.get("image_uris", {})
                if "normal" in image_uris:
                    return image_uris["normal"]

                # For double-faced cards, try card_faces
                card_faces = card_info.get("card_faces", [])
                if card_faces and len(card_faces) > 0:
                    face_image_uris = card_faces[0].get("image_uris", {})
                    if "normal" in face_image_uris:
                        return face_image_uris["normal"]

            # Fallback: Construct Scryfall API URL directly from card ID
            # This works because the card_id IS the Scryfall UUID
            # The API will redirect to the actual image
            scryfall_url = f"https://api.scryfall.com/cards/{card_id}?format=image&version=normal"
            logger.debug(f"Using Scryfall API URL for card {card_id}")
            return scryfall_url

        except Exception as e:
            logger.error(f"Error getting image URL for card {card_id}: {e}")
            return None

    async def download_card_image(
        self,
        session: aiohttp.ClientSession,
        card_id: str,
        carddata: dict,
        timeout: int = 10
    ) -> Optional[Image.Image]:
        """
        Download a single card image from Scryfall.

        Args:
            session: aiohttp session for making requests
            card_id: The card UUID
            carddata: The draft's carddata dictionary
            timeout: Request timeout in seconds

        Returns:
            PIL Image object or None on failure
        """
        try:
            # Get image URL
            image_url = self.get_scryfall_image_url(card_id, carddata)
            if not image_url:
                return None

            # Download image
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    logger.warning(f"Failed to download image for {card_id}: HTTP {response.status}")
                    return None

                image_bytes = await response.read()
                return Image.open(BytesIO(image_bytes))

        except asyncio.TimeoutError:
            logger.warning(f"Timeout downloading image for card {card_id}")
            return None
        except Exception as e:
            logger.error(f"Error downloading image for card {card_id}: {e}")
            return None

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

            # Download all card images in parallel
            async with aiohttp.ClientSession() as session:
                download_tasks = [
                    self.download_card_image(session, card_id, carddata, timeout)
                    for card_id in pack_card_ids
                ]
                card_images = await asyncio.gather(*download_tasks, return_exceptions=True)

            # Filter out failed downloads and exceptions
            valid_images = []
            for i, img in enumerate(card_images):
                if isinstance(img, Exception):
                    logger.warning(f"Exception downloading card {i}: {img}")
                elif img is not None:
                    valid_images.append((i, img))
                else:
                    logger.warning(f"Failed to download card at index {i}")

            if not valid_images:
                logger.error("Failed to download any card images")
                return None

            logger.info(f"Successfully downloaded {len(valid_images)}/15 card images")

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
            output = BytesIO()
            canvas.save(output, format='PNG', optimize=True)
            output.seek(0)

            logger.info(f"Successfully created pack composite: {canvas_width}x{canvas_height}px")
            return output

        except Exception as e:
            logger.error(f"Error creating pack composite: {e}", exc_info=True)
            return None
