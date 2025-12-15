"""
Notification service for sending DMs and other notifications to users.
"""

import asyncio
import discord
from loguru import logger
from preference_service import get_players_dm_notification_preferences

# Rate limiting constants to avoid Discord API throttling
DM_BATCH_SIZE = 8  # Number of DMs to send per batch (matches typical draft size)
DM_BATCH_DELAY = 1.0  # Seconds to wait between batches


async def send_ready_check_dms(bot_or_client, draft_session, guild_id, channel_id, channel_name, guild_name):
    """
    Send DM notifications to users who have DM notifications enabled for a ready check.

    Args:
        bot_or_client: Discord bot or client instance (must have fetch_user method)
        draft_session: The draft session object with sign_ups
        guild_id: Guild/server ID (as string)
        channel_id: Channel ID where ready check was posted (as string)
        channel_name: Name of the channel where ready check was posted
        guild_name: Name of the guild/server

    Returns:
        tuple: (dm_sent_count, enabled_count) - number of DMs sent and number of users with DMs enabled
    """
    logger.info("Starting DM notification process for ready check")

    dm_sent_count = 0
    enabled_count = 0

    try:
        # Get all signed up user IDs
        player_ids = list(draft_session.sign_ups.keys())

        # Get DM notification preferences for all players
        dm_preferences = await get_players_dm_notification_preferences(player_ids, guild_id)

        # Log preferences for debugging
        enabled_count = sum(1 for enabled in dm_preferences.values() if enabled)
        logger.info(f"DM Preferences: {enabled_count}/{len(player_ids)} players have notifications enabled")

        # Collect users who have DM notifications enabled
        users_to_notify = [
            (user_id, display_name)
            for user_id, display_name in draft_session.sign_ups.items()
            if dm_preferences.get(user_id, False)
        ]

        if not users_to_notify:
            logger.info("No users to notify, skipping DM sending")
            return 0, 0

        # Process users in batches to avoid rate limiting
        total_users = len(users_to_notify)
        logger.info(f"Processing {total_users} users in batches of {DM_BATCH_SIZE}")

        for batch_index in range(0, total_users, DM_BATCH_SIZE):
            batch = users_to_notify[batch_index:batch_index + DM_BATCH_SIZE]
            batch_number = (batch_index // DM_BATCH_SIZE) + 1
            total_batches = (total_users + DM_BATCH_SIZE - 1) // DM_BATCH_SIZE

            logger.debug(f"Processing batch {batch_number}/{total_batches} ({len(batch)} users)")

            for user_id, display_name in batch:
                try:
                    # Get the user object
                    user = await bot_or_client.fetch_user(int(user_id))
                    if not user:
                        logger.error(f"Could not fetch user object for {user_id}")
                        continue

                    # Create Discord channel link
                    channel_link = f"https://discord.com/channels/{guild_id}/{channel_id}"

                    # Create DM message
                    dm_message = (
                        f"🔔 **Ready Check Initiated**\n\n"
                        f"A ready check has been started for the draft you signed up for in **{guild_name}**.\n\n"
                        f"Click here to jump to the channel: [#{channel_name}]({channel_link})\n\n"
                        f"Please respond by clicking the Ready button!\n\n"
                        f"_To disable these notifications, use `/toggle_dm_notifications` in {guild_name}_"
                    )

                    # Send DM
                    await user.send(dm_message)
                    dm_sent_count += 1
                    logger.info(f"Successfully sent ready check DM to {display_name} (ID: {user_id})")

                except discord.Forbidden as e:
                    logger.warning(f"Could not send DM to {display_name} (ID: {user_id}) - DMs are disabled or bot is blocked")
                except discord.HTTPException as e:
                    logger.warning(f"HTTP error sending DM to {display_name} (ID: {user_id}): {e}")
                except Exception as e:
                    logger.error(f"Unexpected error sending DM to {display_name} (ID: {user_id}): {e}")
                    logger.exception(f"Full exception traceback:")

            # Add delay between batches (but not after the last batch)
            if batch_index + DM_BATCH_SIZE < total_users:
                logger.debug(f"Waiting {DM_BATCH_DELAY}s before next batch")
                await asyncio.sleep(DM_BATCH_DELAY)

        logger.info(f"DM notification process complete: {dm_sent_count}/{enabled_count} messages sent successfully")
        return dm_sent_count, enabled_count

    except Exception as e:
        logger.error(f"❌ Error in send_ready_check_dms: {e}")
        logger.exception("Full exception traceback:")
        # Don't fail the ready check if DM sending fails
        return 0, 0
