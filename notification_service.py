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


async def _send_notification_dms(
    bot_or_client,
    draft_session,
    guild_id,
    channel_id,
    channel_name,
    guild_name,
    notification_type,
    message_builder
):
    """
    Generic DM notification sender with rate limiting and preference checking.

    Args:
        bot_or_client: Discord bot or client instance (must have fetch_user method)
        draft_session: The draft session object with sign_ups
        guild_id: Guild/server ID (as string)
        channel_id: Channel ID (as string)
        channel_name: Name of the channel
        guild_name: Name of the guild/server
        notification_type: Type of notification for logging (e.g., "ready check", "teams created")
        message_builder: Callable that takes (display_name, channel_link) and returns the DM message string

    Returns:
        tuple: (dm_sent_count, enabled_count) - number of DMs sent and number of users with DMs enabled
    """
    logger.info(f"Starting DM notification process for {notification_type}")

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

        # Create Discord channel link (reused for all messages)
        channel_link = f"https://discord.com/channels/{guild_id}/{channel_id}"

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

                    # Build personalized message
                    dm_message = message_builder(display_name, channel_link)

                    # Send DM
                    await user.send(dm_message)
                    dm_sent_count += 1
                    logger.info(f"Successfully sent {notification_type} DM to {display_name} (ID: {user_id})")

                except discord.Forbidden:
                    logger.warning(f"Could not send DM to {display_name} (ID: {user_id}) - DMs are disabled or bot is blocked")
                except discord.HTTPException as e:
                    logger.warning(f"HTTP error sending DM to {display_name} (ID: {user_id}): {e}")
                except Exception as e:
                    logger.error(f"Unexpected error sending DM to {display_name} (ID: {user_id}): {e}")
                    logger.exception("Full exception traceback:")

            # Add delay between batches (but not after the last batch)
            if batch_index + DM_BATCH_SIZE < total_users:
                logger.debug(f"Waiting {DM_BATCH_DELAY}s before next batch")
                await asyncio.sleep(DM_BATCH_DELAY)

        logger.info(f"{notification_type.title()} DM notification complete: {dm_sent_count}/{enabled_count} messages sent successfully")
        return dm_sent_count, enabled_count

    except Exception as e:
        logger.error(f"❌ Error in {notification_type} DM notification: {e}")
        logger.exception("Full exception traceback:")
        return 0, 0


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
    def build_message(display_name, channel_link):
        return (
            f"🔔 **Ready Check Initiated**\n\n"
            f"A ready check has been started for the draft you signed up for in **{guild_name}**.\n\n"
            f"Click here to jump to the channel: [#{channel_name}]({channel_link})\n\n"
            f"Please respond by clicking the Ready button!\n\n"
            f"_To disable these notifications, use `/toggle_dm_notifications` in {guild_name}_"
        )

    return await _send_notification_dms(
        bot_or_client, draft_session, guild_id, channel_id, channel_name, guild_name,
        "ready check", build_message
    )


async def send_teams_created_dms(bot_or_client, draft_session, guild_id, channel_id, channel_name, guild_name):
    """
    Send DM notifications with personalized draft links when teams are created.

    Only sends to users who have DM notifications enabled.

    Args:
        bot_or_client: Discord bot or client instance (must have fetch_user method)
        draft_session: The draft session object with sign_ups and get_draft_link_for_user method
        guild_id: Guild/server ID (as string)
        channel_id: Channel ID where teams were created (as string)
        channel_name: Name of the channel
        guild_name: Name of the guild/server

    Returns:
        tuple: (dm_sent_count, enabled_count) - number of DMs sent and number of users with DMs enabled
    """
    def build_message(display_name, channel_link):
        draft_link = draft_session.get_draft_link_for_user(display_name)
        return (
            f"🎲 **Teams Created - Draft Ready!**\n\n"
            f"Teams have been created for the draft in **{guild_name}**.\n\n"
            f"**Your Link:** [Draftmancer Link]({draft_link})\n\n"
            f"Click here to jump to the channel: [#{channel_name}]({channel_link})\n\n"
            f"_To disable these notifications, use `/toggle_dm_notifications` in {guild_name}_"
        )

    return await _send_notification_dms(
        bot_or_client, draft_session, guild_id, channel_id, channel_name, guild_name,
        "teams created", build_message
    )
