"""
Draft room creation and pairing functionality.
"""

import discord
import asyncio
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from session import AsyncSessionLocal, DraftSession
from utils import (
    calculate_pairings, generate_draft_summary_embed, post_pairings,
    update_player_stats_for_draft
)
from livedrafts import create_live_draft_summary
from services.draft_setup_manager import DraftSetupManager
from datacollections import DraftLogManager
from views.draft_message_utils import update_last_draft_timestamp


async def create_rooms_and_pairings(view_class, bot, guild, session_id: str, 
                                  interaction=None, session_type=None):
    """Create rooms and post pairings for a draft session."""
    logger.info(f"Starting create_rooms_pairings for session_id={session_id}, session_type={session_type}")
    
    try:
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Fetch draft session
                stmt = select(DraftSession).options(selectinload(DraftSession.match_results))\
                       .filter(DraftSession.session_id == session_id)
                session = await db_session.scalar(stmt)

                if not session:
                    logger.warning(f"Draft session not found for session_id={session_id}")
                    if interaction:
                        await interaction.followup.send("Draft session not found.", ephemeral=True)
                    return False

                # Check if rooms already exist
                if session.draft_chat_channel:
                    logger.info(f"Rooms already exist for session_id={session_id}")
                    if interaction:
                        await interaction.followup.send(
                            "Rooms and pairings have already been created for this draft.", 
                            ephemeral=True
                        )
                    return False

                # Update session state
                session.are_rooms_processing = True
                session.session_stage = 'pairings'
                
                # Calculate pairings
                await _calculate_session_pairings(session, db_session)
                
                # Update player stats if applicable
                await _update_player_stats_if_needed(session, guild, bot)
                
                # Create channels
                temp_view = view_class(bot, session_id, session_type or session.session_type)
                draft_chat_channel = await _create_channels(temp_view, guild, session)
                
                # Post draft summary
                await _post_draft_summary(bot, session, draft_chat_channel)
                
                # Clean up original message
                await _cleanup_original_message(bot, session)
                
                # Update deletion time
                session.deletion_time = datetime.now() + timedelta(days=7)
                
                await db_session.commit()
                
        # Post-commit actions
        await _handle_post_commit_actions(bot, guild, session, interaction)
        
        return True
        
    except Exception:
        logger.exception(f"Unhandled exception in create_rooms_pairings for session_id={session_id}")
        if interaction:
            await interaction.followup.send("An error occurred.", ephemeral=True)
        return False


async def _calculate_session_pairings(session, db_session):
    """Calculate pairings based on session type."""
    logger.debug(f"Calculating pairings for session_type={session.session_type}")
    
    if session.session_type != "swiss":
        await calculate_pairings(session, db_session)
    else:
        state_to_save, match_counter = await calculate_pairings(session, db_session)
        session.match_counter = match_counter
        session.swiss_matches = state_to_save
        logger.debug(f"Swiss pairings calculated: match_counter={match_counter}")


async def _update_player_stats_if_needed(session, guild, bot):
    """Update player stats for applicable session types."""
    if session.session_type in ("random", "staked"):
        logger.debug(f"Updating player stats for session_id={session.session_id}")
        await update_player_stats_for_draft(session.session_id, guild)
    
    if session.session_type in ("random", "staked", "premade"):
        logger.debug(f"Updating last draft timestamp for session_id={session.session_id}")
        await update_last_draft_timestamp(session.session_id, guild, bot)


async def _create_channels(temp_view, guild, session):
    """Create the necessary channels for the draft."""
    draft_chat_channel = None
    
    if session.session_type == "swiss":
        # Swiss draft - everyone in one channel
        all_members = []
        for user_id in session.sign_ups.keys():
            member = guild.get_member(int(user_id))
            if member:
                all_members.append(member)
                
        channel_id = await temp_view.create_team_channel(guild, "Draft", all_members)
        session.draft_chat_channel = str(channel_id)
        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
        
    elif session.session_type != "test":
        # Team-based draft
        team_a_members = [guild.get_member(int(uid)) for uid in session.team_a if guild.get_member(int(uid))]
        team_b_members = [guild.get_member(int(uid)) for uid in session.team_b if guild.get_member(int(uid))]
        all_members = team_a_members + team_b_members
        
        # Create main draft channel
        channel_id = await temp_view.create_team_channel(guild, "Draft", all_members, session.team_a, session.team_b)
        session.draft_chat_channel = str(channel_id)
        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
        
        # Create team channels
        await temp_view.create_team_channel(guild, "Red-Team", team_a_members, session.team_a, session.team_b)
        await temp_view.create_team_channel(guild, "Blue-Team", team_b_members, session.team_a, session.team_b)
        
    else:
        # Test draft - use existing channel
        draft_chat_channel = guild.get_channel(int(session.draft_channel_id))
        session.draft_chat_channel = session.draft_channel_id
        
    return draft_chat_channel


async def _post_draft_summary(bot, session, draft_chat_channel):
    """Post the draft summary message."""
    draft_summary_embed = await generate_draft_summary_embed(bot, session.session_id)
    sign_up_tags = ' '.join(f"<@{user_id}>" for user_id in session.sign_ups.keys())
    
    await draft_chat_channel.send(
        f"Pairings posted below. Good luck in your matches! {sign_up_tags}"
    )
    
    if session.session_type == "staked":
        from views.stake_views import StakeCalculationButton
        stake_view = discord.ui.View(timeout=None)
        stake_view.add_item(StakeCalculationButton(session.session_id))
        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed, view=stake_view)
    else:
        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed)
    
    if session.session_type != "test":
        await draft_summary_message.pin()
        
    session.draft_summary_message_id = str(draft_summary_message.id)


async def _cleanup_original_message(bot, session):
    """Delete the original draft message."""
    draft_channel = bot.get_channel(int(session.draft_channel_id))
    if draft_channel:
        try:
            original_message = await draft_channel.fetch_message(int(session.message_id))
            await original_message.delete()
            logger.debug(f"Deleted original message {session.message_id}")
        except discord.NotFound:
            logger.warning(f"Original message {session.message_id} not found")
        except discord.HTTPException as e:
            logger.error(f"Failed to delete message: {e}")


async def _handle_post_commit_actions(bot, guild, session, interaction):
    """Handle actions after database commit."""
    logger.debug("Running post_pairings tasks")
    await post_pairings(bot, guild, session.session_id)
    await create_live_draft_summary(bot, session.session_id)
    
    if interaction:
        await interaction.followup.send("Pairings posted.", ephemeral=True)
    
    # Start draft log manager if needed
    draft_setup_manager = DraftSetupManager.get_active_manager(session.session_id)
    if not draft_setup_manager and session.draft_link:
        logger.debug("Starting DraftLogManager for live session keep-alive")
        manager = DraftLogManager(
            session.session_id,
            session.draft_link,
            session.draft_id,
            session.session_type,
            session.cube,
            discord_client=bot,
            guild_id=int(guild.id)
        )
        asyncio.create_task(manager.keep_draft_session_alive())

async def create_rooms_and_pairings(view_class, bot, guild, session_id: str, 
                                  interaction=None, session_type=None):
    """Create rooms and post pairings for a draft session."""
    logger.info(f"Starting create_rooms_pairings for session_id={session_id}, session_type={session_type}")
    
    try:
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Fetch draft session
                stmt = select(DraftSession).options(selectinload(DraftSession.match_results))\
                       .filter(DraftSession.session_id == session_id)
                session = await db_session.scalar(stmt)

                if not session:
                    logger.warning(f"Draft session not found for session_id={session_id}")
                    if interaction:
                        await interaction.followup.send("Draft session not found.", ephemeral=True)
                    return False

                # Check if rooms already exist
                if session.draft_chat_channel:
                    logger.info(f"Rooms already exist for session_id={session_id}")
                    if interaction:
                        await interaction.followup.send(
                            "Rooms and pairings have already been created for this draft.", 
                            ephemeral=True
                        )
                    return False

                # Update session state
                session.are_rooms_processing = True
                session.session_stage = 'pairings'
                
                # Calculate pairings
                await _calculate_session_pairings(session, db_session)
                
                # Update player stats if applicable
                await _update_player_stats_if_needed(session, guild, bot)
                
                # Create channels
                temp_view = view_class(bot, session_id, session_type or session.session_type)
                draft_chat_channel = await _create_channels(temp_view, guild, session)
                
                # Post draft summary
                await _post_draft_summary(bot, session, draft_chat_channel)
                
                # Clean up original message
                await _cleanup_original_message(bot, session)
                
                # Update deletion time
                session.deletion_time = datetime.now() + timedelta(days=7)
                
                await db_session.commit()
                
        # Post-commit actions
        await _handle_post_commit_actions(bot, guild, session, interaction)
        
        return True
        
    except Exception:
        logger.exception(f"Unhandled exception in create_rooms_pairings for session_id={session_id}")
        if interaction:
            await interaction.followup.send("An error occurred.", ephemeral=True)
        return False