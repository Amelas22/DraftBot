"""
Ring Bearer Service

Manages the ring bearer role - a single-holder role that can be claimed by:
1. Becoming #1 on any win streak leaderboard
2. Defeating the current ring bearer in a match
3. Being #1 and extending your streak while not having the role

Only one player per guild can hold the ring bearer role at a time.
"""

import discord
from datetime import datetime
from loguru import logger
from typing import Optional
from database.db_session import db_session
from models.ring_bearer_state import RingBearerState
from config import get_config
from services.leaderboard_service import (
    get_win_streak_leaderboard_data,
    get_perfect_streak_leaderboard_data,
    get_draft_win_streak_leaderboard_data
)
from helpers.display_names import get_display_name


async def update_ring_bearer_for_guild(bot, guild_id: str, session_id: Optional[str] = None, streak_extensions: Optional[dict] = None):
    """
    Called after leaderboard updates to check if ring bearer should transfer.

    Only transfers if a #1 leader extended their streak in the draft that just completed.

    Args:
        bot: Discord bot instance
        guild_id: Guild ID string
        session_id: Draft session ID (for logging). If None, this is a manual refresh.
        streak_extensions: Dict of {player_id: {streak_type_increased: bool}}
    """
    try:
        config = get_config(guild_id)
        rb_config = config.get("ring_bearer", {})

        # Check if feature is enabled
        if not rb_config.get("enabled", False):
            return

        # Ring bearer should only consider ACTIVE streaks, not completed ones
        # This ensures that if your streak is broken, you lose the ring bearer eligibility
        timeframe = "active"

        guild = bot.get_guild(int(guild_id))
        if not guild:
            logger.warning(f"Could not find guild {guild_id} for ring bearer update")
            return

        async with db_session() as session:
            # Get current ring bearer state
            ring_bearer_state = await RingBearerState.get_ring_bearer(guild_id, session)
            current_bearer_id = ring_bearer_state.current_bearer_id if ring_bearer_state else None

            # Check each streak category for #1 players
            streak_categories = rb_config.get("streak_categories", [
                "longest_win_streak",
                "perfect_streak",
                "draft_win_streak"
            ])

            logger.info(f"[RING BEARER] Checking categories {streak_categories} for guild {guild_id}, session {session_id}")
            logger.info(f"[RING BEARER] Streak extensions available: {streak_extensions is not None}")

            for category in streak_categories:
                logger.info(f"[RING BEARER] Checking category: {category}")

                # Get #1 leader for this category (only active streaks)
                leader_data = await get_leaderboard_leader(guild_id, category, timeframe, session)

                if not leader_data:
                    logger.info(f"[RING BEARER] No #1 leader in {category}")
                    continue

                leader_id = leader_data["player_id"]

                # Extract streak length and key for logging
                if category == "longest_win_streak":
                    streak_length = leader_data.get("longest_win_streak", 0)
                    streak_key = "win_streak_increased"
                elif category == "perfect_streak":
                    streak_length = leader_data.get("perfect_streak", 0)
                    streak_key = "perfect_streak_increased"
                elif category == "draft_win_streak":
                    streak_length = leader_data.get("draft_win_streak", 0)
                    streak_key = "draft_win_streak_increased"
                else:
                    continue

                logger.info(f"[RING BEARER] #1 in {category}: {leader_id} (streak={streak_length})")

                # Check if this leader extended their streak
                if streak_extensions and session_id:
                    # Normal draft - check if #1 extended in this draft
                    player_extensions = streak_extensions.get(leader_id, {})
                    extended = player_extensions.get(streak_key, False)

                    logger.info(f"[RING BEARER] Leader extended {category}: {extended}")

                    if extended:
                        logger.info(f"[RING BEARER] TRANSFER: {leader_id} gets ring via {category}")
                        await transfer_ring_bearer(
                            bot=bot,
                            guild_id=guild_id,
                            new_bearer_id=leader_id,
                            acquired_via=category,
                            previous_bearer_id=current_bearer_id,
                            streak_info=leader_data
                        )
                        return  # Transfer happened, done
                    else:
                        logger.info(f"[RING BEARER] Leader did not extend in this draft")
                else:
                    # Manual refresh - no streak info, transfer to any #1 leader
                    if leader_id != current_bearer_id:
                        logger.info(f"[RING BEARER] TRANSFER (manual refresh): {leader_id} gets ring via {category}")
                        await transfer_ring_bearer(
                            bot=bot,
                            guild_id=guild_id,
                            new_bearer_id=leader_id,
                            acquired_via=category,
                            previous_bearer_id=current_bearer_id,
                            streak_info=leader_data
                        )
                        return

            logger.info(f"[RING BEARER] No transfer: No #1 leader extended in this draft")

    except Exception as e:
        logger.error(f"Error updating ring bearer for guild {guild_id}: {e}")


async def check_match_defeat_transfer(bot, guild_id: str, winner_id: str, loser_id: str, session_id: str):
    """
    Called after match results are processed.
    Check if loser was ring bearer - if so, transfer to winner.

    Args:
        bot: Discord bot instance
        guild_id: Guild ID string
        winner_id: Winner's Discord ID
        loser_id: Loser's Discord ID
        session_id: Draft session ID (for logging)
    """
    try:
        logger.info(f"[RING BEARER] Checking match defeat transfer for session {session_id} in guild {guild_id}")
        logger.info(f"[RING BEARER] Winner: {winner_id}, Loser: {loser_id}")

        config = get_config(guild_id)
        rb_config = config.get("ring_bearer", {})

        # Check if feature is enabled
        if not rb_config.get("enabled", False):
            logger.info(f"[RING BEARER] Ring bearer feature is not enabled for guild {guild_id}")
            return

        logger.info(f"[RING BEARER] Ring bearer feature is enabled, checking current bearer...")

        async with db_session() as session:
            # Get current ring bearer state
            ring_bearer_state = await RingBearerState.get_ring_bearer(guild_id, session)

            if not ring_bearer_state or not ring_bearer_state.current_bearer_id:
                logger.info(f"[RING BEARER] No current ring bearer in guild {guild_id}")
                return

            current_bearer = ring_bearer_state.current_bearer_id
            logger.info(f"[RING BEARER] Current ring bearer: {current_bearer}")

            # Check if loser was the ring bearer
            if ring_bearer_state.current_bearer_id == loser_id:
                logger.info(f"[RING BEARER] Ring bearer {loser_id} was defeated by {winner_id} in session {session_id} - transferring!")
                await transfer_ring_bearer(
                    bot=bot,
                    guild_id=guild_id,
                    new_bearer_id=winner_id,
                    acquired_via="match_defeat",
                    previous_bearer_id=loser_id
                )
            else:
                logger.info(f"[RING BEARER] Loser {loser_id} is not the ring bearer (current bearer: {current_bearer})")

    except Exception as e:
        logger.error(f"[RING BEARER] Error checking ring bearer defeat in guild {guild_id}: {e}")


async def transfer_ring_bearer(bot, guild_id: str, new_bearer_id: str, acquired_via: str,
                               previous_bearer_id: Optional[str] = None, streak_info: Optional[dict] = None):
    """
    Transfer the ring bearer role between players.

    Args:
        bot: Discord bot instance
        guild_id: Guild ID string
        new_bearer_id: New ring bearer's Discord ID
        acquired_via: How acquired ('match_defeat', 'win_streak', 'perfect_streak', 'draft_win_streak')
        previous_bearer_id: Previous ring bearer's Discord ID (for announcements)
        streak_info: Dict with streak data (streak length, win %, etc.) for leaderboard transfers
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            logger.warning(f"Could not find guild {guild_id} for ring bearer transfer")
            return

        config = get_config(guild_id)
        rb_config = config.get("ring_bearer", {})
        role_name = rb_config.get("role_name", "ring bearer")

        # Sync the Discord role
        await sync_ring_bearer_role(guild, new_bearer_id, previous_bearer_id, role_name)

        # Update database state
        async with db_session() as session:
            await RingBearerState.set_ring_bearer(
                guild_id=guild_id,
                bearer_id=new_bearer_id,
                acquired_via=acquired_via,
                previous_bearer_id=previous_bearer_id,
                session=session
            )

        # Post announcement
        await post_ring_bearer_announcement(
            bot=bot,
            guild_id=guild_id,
            new_bearer_id=new_bearer_id,
            acquired_via=acquired_via,
            previous_bearer_id=previous_bearer_id,
            streak_info=streak_info
        )

        logger.info(f"Transferred ring bearer to {new_bearer_id} via {acquired_via} in guild {guild_id}")

    except Exception as e:
        logger.error(f"Error transferring ring bearer in guild {guild_id}: {e}")


async def sync_ring_bearer_role(guild: discord.Guild, new_bearer_id: str, old_bearer_id: Optional[str] = None,
                                role_name: str = "ring bearer"):
    """
    Sync ring bearer role:
    1. Remove from old bearer (if exists and still in server)
    2. Add to new bearer

    Args:
        guild: Discord guild
        new_bearer_id: New ring bearer's Discord ID
        old_bearer_id: Old ring bearer's Discord ID (optional)
        role_name: Name of the ring bearer role
    """
    try:
        # Find the ring bearer role
        ring_bearer_role = discord.utils.get(guild.roles, name=role_name)

        if not ring_bearer_role:
            logger.warning(f"Ring bearer role '{role_name}' not found in guild {guild.id}. Please create it manually.")
            return

        # Remove from old bearer if exists
        if old_bearer_id:
            old_bearer = guild.get_member(int(old_bearer_id))
            if old_bearer and ring_bearer_role in old_bearer.roles:
                try:
                    await old_bearer.remove_roles(ring_bearer_role)
                    logger.info(f"Removed ring bearer role from {get_display_name(old_bearer, guild)}")
                except discord.Forbidden:
                    logger.warning(f"Cannot remove ring bearer role from {get_display_name(old_bearer, guild)} - missing permissions")
                except discord.HTTPException as e:
                    logger.error(f"HTTP error removing ring bearer role from {get_display_name(old_bearer, guild)}: {e}")

        # Add to new bearer
        new_bearer = guild.get_member(int(new_bearer_id))
        if new_bearer:
            if ring_bearer_role not in new_bearer.roles:
                try:
                    await new_bearer.add_roles(ring_bearer_role)
                    logger.info(f"Added ring bearer role to {get_display_name(new_bearer, guild)}")
                except discord.Forbidden:
                    logger.warning(f"Cannot add ring bearer role to {get_display_name(new_bearer, guild)} - missing permissions")
                except discord.HTTPException as e:
                    logger.error(f"HTTP error adding ring bearer role to {get_display_name(new_bearer, guild)}: {e}")
        else:
            logger.warning(f"New ring bearer {new_bearer_id} not found in guild {guild.id}")

    except Exception as e:
        logger.error(f"Error syncing ring bearer role in guild {guild.id}: {e}")


async def post_ring_bearer_announcement(bot, guild_id: str, new_bearer_id: str, acquired_via: str,
                                        previous_bearer_id: Optional[str] = None, streak_info: Optional[dict] = None):
    """
    Post announcement to draft-results channel when ring bearer transfers.

    Args:
        bot: Discord bot instance
        guild_id: Guild ID string
        new_bearer_id: New ring bearer's Discord ID
        acquired_via: How acquired ('match_defeat', 'win_streak', 'perfect_streak', 'draft_win_streak')
        previous_bearer_id: Previous ring bearer's Discord ID
        streak_info: Dict with streak data for leaderboard transfers
    """
    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return

        config = get_config(guild_id)
        results_channel_name = config.get("channels", {}).get("draft_results", "draft-results")
        results_channel = discord.utils.get(guild.text_channels, name=results_channel_name)

        if not results_channel:
            logger.warning(f"Draft results channel '{results_channel_name}' not found in guild {guild_id}")
            return

        rb_config = config.get("ring_bearer", {})
        icon = rb_config.get("icon", "🏆")

        # Get member objects for display names
        new_bearer = guild.get_member(int(new_bearer_id))
        if not new_bearer:
            logger.warning(f"Could not find new ring bearer {new_bearer_id} in guild {guild_id}")
            return

        new_bearer_display = get_display_name(new_bearer, guild)

        # Build announcement message based on acquisition method
        if acquired_via == "match_defeat":
            if previous_bearer_id:
                previous_bearer = guild.get_member(int(previous_bearer_id))
                previous_display = get_display_name(previous_bearer, guild) if previous_bearer else "the previous holder"
                description = f"**{new_bearer_display}** has claimed the **Coveted Jewel** by defeating **{previous_display}** in combat!"
            else:
                description = f"**{new_bearer_display}** has claimed the **Coveted Jewel** by defeating the previous holder!"

        elif acquired_via == "longest_win_streak":
            streak_length = streak_info.get("longest_win_streak", "?") if streak_info else "?"
            description = f"**{new_bearer_display}** has claimed the **Coveted Jewel** with a **{streak_length}-game win streak**!"
            if previous_bearer_id:
                previous_bearer = guild.get_member(int(previous_bearer_id))
                if previous_bearer:
                    previous_display = get_display_name(previous_bearer, guild)
                    description += f"\n*Previous holder: {previous_display}*"

        elif acquired_via == "perfect_streak":
            streak_length = streak_info.get("perfect_streak", "?") if streak_info else "?"
            description = f"**{new_bearer_display}** has claimed the **Coveted Jewel** with a **{streak_length}-match perfect streak** (all 2-0 wins)!"
            if previous_bearer_id:
                previous_bearer = guild.get_member(int(previous_bearer_id))
                if previous_bearer:
                    previous_display = get_display_name(previous_bearer, guild)
                    description += f"\n*Previous holder: {previous_display}*"

        elif acquired_via == "draft_win_streak":
            streak_length = streak_info.get("draft_win_streak", "?") if streak_info else "?"
            description = f"**{new_bearer_display}** has claimed the **Coveted Jewel** with a **{streak_length}-draft win streak**!"
            if previous_bearer_id:
                previous_bearer = guild.get_member(int(previous_bearer_id))
                if previous_bearer:
                    previous_display = get_display_name(previous_bearer, guild)
                    description += f"\n*Previous holder: {previous_display}*"

        else:
            description = f"**{new_bearer_display}** has claimed the **Coveted Jewel**!"

        # Create embed
        embed = discord.Embed(
            title=f"{icon} Coveted Jewel Transfer {icon}",
            description=description,
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )

        await results_channel.send(embed=embed)
        logger.info(f"Posted ring bearer announcement in guild {guild_id}")

    except Exception as e:
        logger.error(f"Error posting ring bearer announcement in guild {guild_id}: {e}")


async def get_leaderboard_leader(guild_id: str, category: str, timeframe: str, session):
    """
    Get the #1 player for a specific leaderboard category.

    Args:
        guild_id: Guild ID string
        category: Category name ('longest_win_streak', 'perfect_streak', 'draft_win_streak')
        timeframe: Timeframe string ('active', '30d', '90d', 'lifetime')
        session: Database session

    Returns:
        Dict with leader data or None if no leader found
    """
    try:
        # Query the appropriate leaderboard function
        if category == "longest_win_streak":
            players = await get_win_streak_leaderboard_data(guild_id, timeframe, limit=1, session=session)
        elif category == "perfect_streak":
            players = await get_perfect_streak_leaderboard_data(guild_id, timeframe, limit=1, session=session)
        elif category == "draft_win_streak":
            players = await get_draft_win_streak_leaderboard_data(guild_id, timeframe, limit=1, session=session)
        else:
            logger.warning(f"Unknown streak category: {category}")
            return None

        if players and len(players) > 0:
            return players[0]

        return None

    except Exception as e:
        logger.error(f"Error getting leaderboard leader for {category} in guild {guild_id}: {e}")
        return None
