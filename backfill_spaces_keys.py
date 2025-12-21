"""
Backfill script to populate spaces_object_key for existing drafts

This script:
1. Lists all JSON files from DigitalOcean Spaces
2. Extracts draft_id from filenames
3. Matches with database records by draft_id
4. Updates spaces_object_key field
"""

import asyncio
import os
from typing import Optional, Dict
from loguru import logger
from session import AsyncSessionLocal
from models import DraftSession
from sqlalchemy import select
from helpers.digital_ocean_helper import DigitalOceanHelper

# Constants
BATCH_SIZE = 100
PROGRESS_LOG_INTERVAL = 100


def extract_draft_id_from_filename(filename: str) -> Optional[str]:
    """
    Extract draft_id from Draftmancer filename.

    Expected format: {cube}-{timestamp}-{draft_id}.json
    Where draft_id may have optional 'DB' prefix.

    Examples:
        "AlphaFrog-1716339969737-DBE06VTLQZ.json" -> "E06VTLQZ"
        "PowerLSV-123-ABC123.json" -> "ABC123"

    Args:
        filename: The filename to parse

    Returns:
        Draft ID without 'DB' prefix, or None if parsing failed
    """
    stem = filename.removesuffix('.json')
    parts = stem.split('-')

    if len(parts) < 3:
        return None

    draft_id = parts[-1]
    return draft_id.removeprefix('DB')


async def list_all_json_files() -> Dict[str, str]:
    """
    List all JSON files in Spaces and build a draft_id → object_key mapping.

    Returns:
        Dict mapping draft_id to full object key
    """
    logger.info("Listing all JSON files from Spaces...")

    helper = DigitalOceanHelper()
    if not helper.config_valid:
        logger.error("DigitalOcean Spaces configuration incomplete. Check .env file.")
        return {}

    # List all objects (both 'team' and 'swiss' folders)
    # Note: raw_endpoint returns paths with bucket prefix
    bucket = helper.bucket
    all_objects = []
    for folder in ['team', 'swiss']:
        # Use bucket-prefixed path for listing
        prefix = f'{bucket}/{folder}/'
        logger.info(f"Listing objects with prefix: {prefix}")
        objects = await helper.list_objects(prefix)
        logger.info(f"Found {len(objects)} objects for prefix {prefix}")
        if objects:
            logger.debug(f"Sample objects: {objects[:3]}")
        all_objects.extend(objects)

    logger.info(f"Total objects found: {len(all_objects)}")
    draft_id_to_key = {}

    for key_path in all_objects:
        # Normalize: Strip bucket prefix if present
        # Spaces returns: "magic-draft-logs/team/file.json"
        # We want to store: "team/file.json" (boto3 expects this format)
        normalized_key = key_path
        if key_path.startswith(f"{helper.bucket}/"):
            normalized_key = key_path[len(helper.bucket) + 1:]

        # Only process JSON files
        if not normalized_key.endswith('.json'):
            continue

        # Extract draft_id from filename
        filename = normalized_key.split('/')[-1]
        draft_id = extract_draft_id_from_filename(filename)

        if draft_id:
            draft_id_to_key[draft_id] = normalized_key

    logger.info(f"Found {len(draft_id_to_key)} JSON files mapped by draft_id")
    return draft_id_to_key


async def backfill_spaces_object_keys(dry_run: bool = False, limit: Optional[int] = None):
    """
    Backfill spaces_object_key for all drafts with data_received=True

    Args:
        dry_run: If True, only report what would be updated without making changes
        limit: If set, only process this many drafts (for testing)
    """
    logger.info("Starting backfill of spaces_object_key...")

    # Step 1: List all JSON files from Spaces and build draft_id mapping
    draft_id_to_key = await list_all_json_files()

    if not draft_id_to_key:
        logger.error("No JSON files found in Spaces!")
        return

    logger.info(f"Built mapping for {len(draft_id_to_key)} draft IDs")

    # Step 2: Get drafts to update from database
    async with AsyncSessionLocal() as session:
        stmt = select(DraftSession).where(
            DraftSession.data_received == True
            # Note: We update ALL drafts, even if they have a key (to fix incorrect ones)
        ).order_by(DraftSession.id.desc())

        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        drafts_to_update = result.scalars().all()

        logger.info(f"Found {len(drafts_to_update)} drafts to update{' (limited)' if limit else ''}")

        if dry_run:
            logger.info("DRY RUN - No changes will be made")

        updated_count = 0
        not_found_count = 0

        # Step 3: Match and update
        for i, draft in enumerate(drafts_to_update):
            if i % PROGRESS_LOG_INTERVAL == 0 and i > 0:
                logger.info(f"Progress: {i}/{len(drafts_to_update)}")

            # Look up object key by draft_id
            if draft.draft_id in draft_id_to_key:
                object_key = draft_id_to_key[draft.draft_id]

                if not dry_run:
                    draft.spaces_object_key = object_key

                updated_count += 1
                logger.debug(f"✅ Matched: {draft.draft_id} → {object_key}")
            else:
                not_found_count += 1

            # Commit in batches
            if not dry_run and (i + 1) % BATCH_SIZE == 0:
                await session.commit()
                logger.debug(f"Committed batch at {i + 1}")

        # Final commit
        if not dry_run:
            await session.commit()

        logger.info("")
        logger.info(f"{'='*50}")
        logger.info(f"Backfill {'(DRY RUN) ' if dry_run else ''}complete!")
        logger.info(f"   Updated: {updated_count}")
        logger.info(f"   Not found: {not_found_count}")
        logger.info(f"{'='*50}")


if __name__ == "__main__":
    import sys

    # Parse command line arguments
    dry_run = "--execute" not in sys.argv
    limit = None

    # Check for --limit N argument
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[i + 1])
            except ValueError:
                print(f"Invalid limit value: {sys.argv[i + 1]}")
                sys.exit(1)

    if dry_run:
        print("\n🔍 DRY RUN MODE - No changes will be made")
        print("Run with --execute to actually update the database")
    if limit:
        print(f"📊 LIMIT: Processing only {limit} drafts\n")
    else:
        print("")

    asyncio.run(backfill_spaces_object_keys(dry_run=dry_run, limit=limit))

