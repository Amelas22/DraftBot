"""
Draft message utilities for updating Discord messages and timestamps.
"""

import discord
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import update, select

from session import AsyncSessionLocal, DraftSession, StakeInfo
from views.view_helpers import EmbedHelper
from helpers.utils import get_cube_thumbnail_url


async def update_draft_message(bot, session_id: str):
    """Update the draft message with current sign-ups, stakes, and other info."""
    logger.info(f"Starting update for draft message with session ID: {session_id}")

    # Fetch draft session
    from session import get_draft_session
    draft_session = await get_draft_session(session_id)
    if not draft_session:
        logger.error("Failed to fetch draft session for updating the message.")
        return

    channel_id = int(draft_session.draft_channel_id)
    message_id = int(draft_session.message_id)
    logger.info(f"Fetched draft session. Channel ID: {channel_id}, Message ID: {message_id}")

    # Fetch channel
    channel = bot.get_channel(channel_id)
    if not channel:
        logger.error(f"Channel with ID {channel_id} not found.")
        return

    try:
        # Fetch message
        message = await channel.fetch_message(message_id)
        logger.info(f"Fetched message with ID: {message_id} from channel {channel_id}")

        # Update embed with sign-ups
        embed = message.embeds[0]  # Assuming there's at least one embed in the message
        
        # Ensure sign_ups is not None before accessing its length
        if draft_session.sign_ups is None:
            draft_session.sign_ups = {}
            # Update the session in the database with the initialized sign_ups
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        update(DraftSession)
                        .where(DraftSession.session_id == draft_session.session_id)
                        .values(sign_ups={})
                    )
                    await db_session.commit()
            logger.info(f"Initialized empty sign_ups for session ID: {session_id}")
            
        sign_up_count = len(draft_session.sign_ups)
        sign_ups_field_name = "Sign-Ups:"
        
        # For staked drafts, fetch the stake information
        stake_info_by_player = {}
        if draft_session.session_type == "staked":
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
                    results = await db_session.execute(stake_stmt)
                    stake_infos = results.scalars().all()
                    
                    # Create a lookup for stake info by player ID
                    for stake_info in stake_infos:
                        stake_amount = stake_info.max_stake
                        is_capped = getattr(stake_info, 'is_capped', True)  # Default to True if not set
                        stake_info_by_player[stake_info.player_id] = {
                            'amount': stake_amount,
                            'is_capped': is_capped
                        }
        
        # Create sign-ups string with stake amounts for staked drafts
        if draft_session.session_type == "staked":
            sign_ups_list = []
            for user_id, display_name in draft_session.sign_ups.items():
                # Create user-specific draft link
                user_draft_link = draft_session.get_draft_link_for_user(display_name)
                # Create hyperlink markdown format
                linked_name = f"[{display_name}]({user_draft_link})"
                # Default to "Not set" if no stake has been set yet
                if user_id in stake_info_by_player:
                    stake_amount = stake_info_by_player[user_id]['amount']
                    is_capped = stake_info_by_player[user_id]['is_capped']
                    capped_emoji = "🧢" if is_capped else "🏎️"  # Cap emoji for capped, lightning for uncapped
                    sign_ups_list.append((user_id, linked_name, stake_amount, is_capped, capped_emoji))
                else:
                    sign_ups_list.append((user_id, linked_name, "Not set", True, "❓"))
            
            # Sort by stake amount (highest first)
            # Convert "Not set" to -1 for sorting purposes
            def sort_key(item):
                stake = item[2]
                return -1 if stake == "Not set" else stake
            
            sign_ups_list.sort(key=sort_key, reverse=True)
            
            # Format with stakes and capping status
            formatted_sign_ups = []
            for user_id, display_name, stake_amount, is_capped, emoji in sign_ups_list:
                if stake_amount == "Not set":
                    formatted_sign_ups.append(f"❌ Not set: {display_name}")
                else:
                    formatted_sign_ups.append(f"{emoji} {stake_amount} tix: {display_name}")
            
            sign_ups_str = f"**Players ({sign_up_count}):**\n" + ('\n'.join(formatted_sign_ups) if formatted_sign_ups else 'No players yet.')
        else:
            if draft_session.sign_ups:
                linked_names = []
                for user_id, display_name in draft_session.sign_ups.items():
                    user_draft_link = draft_session.get_draft_link_for_user(display_name)
                    linked_name = f"[{display_name}]({user_draft_link})"
                    linked_names.append(linked_name)
                sign_ups_str = f"**Players ({sign_up_count}):**\n" + '\n'.join(linked_names)
            else:
                sign_ups_str = f"**Players (0):**\nNo players yet."
        
        # Helper function to update or add fields consistently
        def update_field(field_name, field_value, inline=False, expected_index=None):
            field_index = None
            # Look for the field by name
            for i, field in enumerate(embed.fields):
                if field.name == field_name:
                    field_index = i
                    break
            
            # If field exists, update it
            if field_index is not None:
                embed.set_field_at(field_index, name=field_name, value=field_value, inline=inline)
                logger.info(f"Updated {field_name} field for session {session_id}")
            else:
                # Field doesn't exist, add it
                logger.warning(f"{field_name} field not found in embed for session {session_id}, adding it")
                embed.add_field(name=field_name, value=field_value, inline=inline)
        
        # Find and remove any existing sign-up continuation fields to start fresh
        fields_to_remove = []
        for i, field in enumerate(embed.fields):
            if field.name.startswith(sign_ups_field_name) and field.name != sign_ups_field_name:
                fields_to_remove.append(i)
        
        # Remove fields in reverse order
        for idx in sorted(fields_to_remove, reverse=True):
            embed.remove_field(idx)
        
        # Check if the sign-ups string is too long
        if len(sign_ups_str) > 1000:  # Using 1000 to be safe (Discord limit is 1024)
            # Split the sign-ups into parts using our helper function
            parts = EmbedHelper.split_content_for_embed(sign_ups_str, include_header=True)
            
            # Update or add fields with standardized names
            for i, part in enumerate(parts):
                field_name = sign_ups_field_name if i == 0 else f"{sign_ups_field_name} (cont. {i})"
                update_field(field_name, part, inline=False)
        else:
            # Use the original approach for short sign-ups lists
            update_field(sign_ups_field_name, sign_ups_str, inline=False)
        
        # Update cube field
        cube_field_name = "Cube:"
        cube_field_value = f"[{draft_session.cube}](https://cubecobra.com/cube/list/{draft_session.cube})"
        update_field(cube_field_name, cube_field_value, inline=True)
                
        # Get the thumbnail URL for the cube
        thumbnail_url = get_cube_thumbnail_url(draft_session.cube)
        embed.set_thumbnail(url=thumbnail_url)
        logger.info(f"Updated thumbnail for cube: {draft_session.cube}")
        
        await message.edit(embed=embed)
        logger.info(f"Successfully updated message for session ID: {session_id}")

    except Exception as e:
        logger.exception(f"Failed to update message for session {session_id}. Error: {e}")


async def update_last_draft_timestamp(session_id: str, guild, bot):
    """Update the last_draft_timestamp and assign Active role to all players in a draft."""
    guild_id = str(guild.id)
    current_time = datetime.now()
    
    # Get config for this guild to check role name
    from config import get_config
    config = get_config(guild_id)
    
    # Get activity tracking settings (for database update purposes only)
    activity_tracking_enabled = config.get("activity_tracking", {}).get("enabled", False)
    
    # Always get the active role name regardless of activity tracking setting
    active_role_name = config.get("activity_tracking", {}).get("active_role", "Active")
    
    # Find the active role if it exists
    active_role = None
    if active_role_name:
        active_role = discord.utils.get(guild.roles, name=active_role_name)
        if not active_role:
            logger.warning(f"Active role '{active_role_name}' not found in guild {guild.name}")
    
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            # Get the draft session
            from session import get_draft_session
            stmt = select(DraftSession).where(DraftSession.session_id == session_id)
            draft_session = await db_session.scalar(stmt)
            
            if not draft_session:
                logger.error(f"Draft session {session_id} not found when updating timestamps.")
                return
            
            # Get all players in the draft
            player_ids = draft_session.team_a + draft_session.team_b
            
            # Update last_draft_timestamp for each player and assign Active role if it exists
            for player_id in player_ids:
                # Update timestamp in database
                from models.player import PlayerStats
                stmt = select(PlayerStats).where(
                    PlayerStats.player_id == player_id,
                    PlayerStats.guild_id == guild_id
                )
                player_stat = await db_session.scalar(stmt)
                
                if player_stat:
                    player_stat.last_draft_timestamp = current_time
                    logger.info(f"Updated last_draft_timestamp for player {player_stat.display_name}")
                else:
                    logger.warning(f"Player {player_id} not found in PlayerStats.")
                
                # Always assign Active role if it exists, regardless of activity tracking setting
                if active_role:
                    try:
                        # Get the member object
                        member = guild.get_member(int(player_id))
                        if member:
                            # Check if member already has the role
                            if active_role not in member.roles:
                                await member.add_roles(active_role)
                                logger.info(f"Added Active role to {member.display_name}")
                        else:
                            # Get username from draft session sign_ups if possible
                            username = draft_session.sign_ups.get(player_id, "Unknown")
                            logger.warning(f"Member {player_id} ({username}) not found in guild {guild.name}")
                    except Exception as e:
                        logger.error(f"Error assigning Active role to player {player_id}: {e}")
            
            await db_session.commit()