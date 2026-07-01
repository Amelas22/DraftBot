"""
Notification service for sending DMs and other notifications to users.
"""

import asyncio
import discord
from loguru import logger
from helpers.display_names import get_member_name_plain
from preference_service import get_players_dm_notification_preferences
from services.debt_service import get_balance_with
from helpers.utils import not_none

# Rate limiting constants to avoid Discord API throttling
DM_BATCH_SIZE = 8  # Number of DMs to send per batch (matches typical draft size)
DM_BATCH_DELAY = 1.0  # Seconds to wait between batches


async def send_dm(bot_or_client, user_id: str, message: str, view=None, label: str | None = None) -> bool:
    """
    Send a DM to a single user with standard error handling.

    Args:
        label: Human-readable description of who this user is (e.g. "debtor JohnDoe")
              for clearer log messages.

    Returns True if the DM was sent successfully, False otherwise.
    """
    who = f"{label} ({user_id})" if label else f"user {user_id}"
    logger.debug(f"Attempting DM to {who}: {message}")
    try:
        user = not_none(bot_or_client.get_user(int(user_id)) or await bot_or_client.fetch_user(int(user_id)))
        await user.send(message, view=view)
        logger.info(f"Successfully sent DM to {who}")
        return True
    except discord.Forbidden:
        logger.info(f"Could not DM {who} - DMs disabled or bot blocked. Message: {message}")
        return False
    except discord.HTTPException as e:
        if e.code == 50007:  # Cannot send messages to this user
            logger.info(f"Could not DM {who} - DMs disabled (50007). Message: {message}")
            return False
        logger.warning(f"HTTP error sending DM to {who}: {e}. Message: {message}")
        return False


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
                dm_message = message_builder(display_name, channel_link)
                if await send_dm(bot_or_client, user_id, dm_message, label=f"{notification_type} {display_name}"):
                    dm_sent_count += 1

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


async def send_debt_transfer_dms(
    bot, guild: "discord.Guild", guild_id: str, transferrer_id: str,
    debtor_id: str, creditor_id: str, amount: int
):
    """Send DM notifications to debtor and creditor about a debt transfer."""
    transferrer_name = get_member_name_plain(guild, transferrer_id)
    debtor_name = get_member_name_plain(guild, debtor_id)
    creditor_name = get_member_name_plain(guild, creditor_id)

    # Get post-transfer balances from debtor's perspective
    # positive = creditor owes debtor, negative = debtor owes creditor
    debtor_to_transferrer = await get_balance_with(guild_id, debtor_id, transferrer_id)
    debtor_to_creditor_post = await get_balance_with(guild_id, debtor_id, creditor_id)

    # Derive pre-transfer balance: transfer decreased debtor's balance with creditor by `amount`
    debtor_to_creditor_pre = debtor_to_creditor_post + amount

    remaining_to_transferrer = abs(debtor_to_transferrer) if debtor_to_transferrer < 0 else 0

    def _format_balance(balance, other_name):
        """Format a balance between two users. Positive = other owes you, negative = you owe other."""
        if balance < 0:
            return f"you owed {other_name} {abs(balance)} tix"
        elif balance > 0:
            return f"{other_name} owed you {balance} tix"
        else:
            return f"you and {other_name} were settled"

    pre_debtor = _format_balance(debtor_to_creditor_pre, creditor_name)
    post_debtor = _format_balance(debtor_to_creditor_post, creditor_name).replace("owed", "owe").replace("were", "are")
    # Flip sign for creditor's perspective
    pre_creditor = _format_balance(-debtor_to_creditor_pre, debtor_name)
    post_creditor = _format_balance(-debtor_to_creditor_post, debtor_name).replace("owed", "owe").replace("were", "are")

    # DM debtor
    debtor_msg = f"{transferrer_name} transferred {amount} tix of your debt to {creditor_name}."
    if remaining_to_transferrer > 0:
        debtor_msg += f" You still owe {transferrer_name} {remaining_to_transferrer} tix."
    else:
        debtor_msg += f" You no longer owe {transferrer_name}."
    debtor_msg += f" Previously {pre_debtor}. Now {post_debtor}."

    # DM creditor
    creditor_msg = f"{transferrer_name} transferred {amount} tix of {debtor_name}'s debt to you."
    creditor_msg += f" Previously {pre_creditor}. Now {post_creditor}."

    await send_dm(bot, debtor_id, debtor_msg, label=f"debtor {debtor_name}")
    await send_dm(bot, creditor_id, creditor_msg, label=f"creditor {creditor_name}")


async def send_settlement_notification_dm(
    bot, guild: "discord.Guild", guild_id: str, settler_id: str,
    payer_id: str, payee_id: str, amount: int
):
    """Send a DM to the other party when a settlement is recorded."""
    settler_name = get_member_name_plain(guild, settler_id)
    payer_name = get_member_name_plain(guild, payer_id)
    payee_name = get_member_name_plain(guild, payee_id)

    # Determine who to notify (the party that didn't initiate)
    other_id = payee_id if settler_id == payer_id else payer_id

    # Get post-settlement balance from other party's perspective
    other_to_settler = await get_balance_with(guild_id, other_id, settler_id)

    if other_to_settler < 0:
        balance_msg = f"You still owe {settler_name} {abs(other_to_settler)} tix."
    elif other_to_settler > 0:
        balance_msg = f"{settler_name} still owes you {other_to_settler} tix."
    else:
        balance_msg = f"You and {settler_name} are fully settled."

    if settler_id == payer_id:
        msg = f"💰 {payer_name} has recorded that they paid {payee_name} {amount} tix. {balance_msg}"
    else:
        msg = f"💰 {payee_name} has recorded that {payer_name} paid them {amount} tix. {balance_msg}"

    await send_dm(bot, other_id, msg, label=f"settlement notify {other_id}")


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
