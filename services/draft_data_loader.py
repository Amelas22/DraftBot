"""
Async data loading for draft analysis.

Functions for fetching draft data from DigitalOcean Spaces.
Used by DraftAnalysis factory methods.
"""

from typing import Optional, Dict
from helpers.digital_ocean_helper import DigitalOceanHelper
from loguru import logger


async def load_from_spaces(object_key: str) -> Optional[Dict]:
    """
    Load draft data from DigitalOcean Spaces.

    Args:
        object_key: Spaces object path (e.g., "team/PowerLSV-123.json")

    Returns:
        Draft data dict or None if load failed
    """
    if not object_key:
        return None

    helper = DigitalOceanHelper()
    try:
        draft_data = await helper.download_json(object_key)
        if draft_data:
            logger.info(f"Loaded draft data from Spaces: {object_key}")
        else:
            logger.warning(f"No data returned for: {object_key}")
        return draft_data
    except Exception as e:
        logger.error(f"Error loading from Spaces ({object_key}): {e}")
        return None
