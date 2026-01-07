import discord
import asyncio
from datetime import datetime
from sqlalchemy import select, not_
from session import AsyncSessionLocal, DraftSession, MatchResult, get_draft_session
from utils import calculate_team_wins
from loguru import logger

async def manage_live_drafts_channel(bot, guild):
    """Create or get the live-drafts channel and ensure it exists"""
    live_drafts_channel = discord.utils.get(guild.text_channels, name="live-drafts")
    
    if not live_drafts_channel:
        # Get the category from config
        from config import get_config
        config = get_config(guild.id)
        draft_category_name = config["categories"].get("draft", "Drafts")
        draft_category = discord.utils.get(guild.categories, name=draft_category_name)
        
        # Create the channel in the appropriate category
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, read_messages=True, embed_links=True)
        }
        live_drafts_channel = await guild.create_text_channel(
            "live-drafts", 
            category=draft_category, 
            topic="Current ongoing drafts", 
            overwrites=overwrites
        )
        
        # Send an initial message
        await live_drafts_channel.send("# Live Draft Updates\nThis channel shows all currently active drafts.")
    else:
        # Make sure the bot has the necessary permissions in existing channels
        current_perms = live_drafts_channel.overwrites_for(guild.me)
        if not current_perms.send_messages or not current_perms.embed_links:
            try:
                await live_drafts_channel.set_permissions(
                    guild.me,
                    send_messages=True,
                    read_messages=True,
                    embed_links=True
                )
            except Exception as e:
                print(f"Could not update bot permissions in live-drafts channel: {e}")
    
    return live_drafts_channel


async def create_live_draft_summary(bot, draft_session_id):
    """Create a summary of a draft in the live-drafts channel"""
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session or draft_session.session_stage != "pairings":
            return None  # Only show drafts that have started pairings
            
        guild = bot.get_guild(int(draft_session.guild_id))
        if not guild:
            return None
        
        try:    
            live_drafts_channel = await manage_live_drafts_channel(bot, guild)
            
            # Generate embed similar to the draft summary but with more details
            embed = await generate_live_draft_embed(bot, draft_session)
            
            # Send the message and store its ID in the draft session for updates
            message = await live_drafts_channel.send(embed=embed)
            
            # Store the message ID in the draft session
            draft_session.live_draft_message_id = str(message.id)
            session.add(draft_session)
            await session.commit()
            
            return message
        except discord.Forbidden as e:
            logger.error(f"Permission error creating live draft summary for {draft_session_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error creating live draft summary for {draft_session_id}: {e}")
            return None
    

async def generate_live_draft_embed(bot, draft_session):
    """Generate an embed for the live draft summary"""
    guild = bot.get_guild(int(draft_session.guild_id))
    if not guild:
        return None
        
    # Get team names and players
    team_a_names = [getattr(guild.get_member(int(user_id)), 'display_name', "Unknown User") for user_id in draft_session.team_a]
    team_b_names = [getattr(guild.get_member(int(user_id)), 'display_name', "Unknown User") for user_id in draft_session.team_b]
    
    # Calculate team wins
    team_a_wins, team_b_wins = await calculate_team_wins(draft_session.session_id)
    
    # Create the embed
    cube_choice = draft_session.cube
    embed = discord.Embed(
        title=f"Live {cube_choice} Draft",
        description=f"Started <t:{int(draft_session.teams_start_time.timestamp())}:R>",
        color=discord.Color.blue()
    )
    
    # Add team fields
    embed.add_field(name="🔴 Team Red", value="\n".join(team_a_names) or "No players", inline=True)
    embed.add_field(name="🔵 Team Blue", value="\n".join(team_b_names) or "No players", inline=True)
    
    # Add scoreboard
    embed.add_field(
        name="**Current Score**",
        value=f"🔴 Team Red: {team_a_wins}\n🔵 Team Blue: {team_b_wins}\n",
        inline=False
    )
    
    # Add match results
    matches_text = ""

    async with AsyncSessionLocal() as session:
        # Get all match results for this draft
        stmt = select(MatchResult).filter_by(session_id=draft_session.session_id).order_by(MatchResult.match_number)
        result = await session.execute(stmt)
        match_results = result.scalars().all()
        
        for match in match_results:
            player1 = guild.get_member(int(match.player1_id))
            player2 = guild.get_member(int(match.player2_id))
            player1_name = getattr(player1, 'display_name', 'Unknown User')
            player2_name = getattr(player2, 'display_name', 'Unknown User')
            
            # Determine if there's a winner
            if match.winner_id:
                # Determine which team won
                winner_emoji = ""
                if match.winner_id in draft_session.team_a:
                    winner_emoji = "🔴 "
                elif match.winner_id in draft_session.team_b:
                    winner_emoji = "🔵 "
                
                # Determine winner and loser names
                if match.winner_id == match.player1_id:
                    winner_name = player1_name
                    loser_name = player2_name
                    score = f"{match.player1_wins}-{match.player2_wins}"
                else:
                    winner_name = player2_name
                    loser_name = player1_name
                    score = f"{match.player2_wins}-{match.player1_wins}"
                    
                match_text = f"{winner_emoji}**Match {match.match_number}**: {winner_name} defeats {loser_name} ({score})"
            else:
                match_text = f"**⚫ Match {match.match_number}**: {player1_name} v. {player2_name}"
                
            matches_text += match_text + "\n"
    
    if matches_text:
        embed.add_field(name="Matches\n", value=matches_text, inline=False)
        
    # Add bet information if this is a staked draft
    if draft_session.session_type == "staked":
        from utils import get_formatted_stake_pairs
        stake_lines, total_stakes = await get_formatted_stake_pairs(
            draft_session.session_id, 
            draft_session.sign_ups
        )
        
        # Add the bet field to the embed
        if stake_lines:
            embed.add_field(
                name=f"**Total Bets: {total_stakes} tix**",
                value="\n".join(stake_lines),
                inline=False
            )

    return embed


async def update_live_draft_summary(bot, draft_session_id):
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session or not draft_session.live_draft_message_id:
            # If this draft doesn't have a live summary yet, create one
            await create_live_draft_summary(bot, draft_session_id)
            return
            
        # Get the live-drafts channel
        guild = bot.get_guild(int(draft_session.guild_id))
        if not guild:
            return
            
        try:
            live_drafts_channel = await manage_live_drafts_channel(bot, guild)
            
            try:
                # Get the message
                message = await live_drafts_channel.fetch_message(int(draft_session.live_draft_message_id))
                
                # Generate updated embed
                updated_embed = await generate_live_draft_embed(bot, draft_session)
                
                # Update the message
                await message.edit(embed=updated_embed)
            except discord.NotFound:
                # If message was deleted, create a new one (if needed)
                # Uncomment the line below if you want to recreate deleted messages
                # await create_live_draft_summary(bot, draft_session_id)
                logger.info(f"Live draft message not found for {draft_session_id}, skipping update")
            except discord.Forbidden as e:
                logger.error(f"Permission error updating live draft summary for {draft_session_id}: {e}")
                # Clear the stored message ID to prevent further errors
                draft_session.live_draft_message_id = None
                session.add(draft_session)
                await session.commit()
            except Exception as e:
                logger.error(f"Failed to update live draft summary: {e}")
        except Exception as e:
            logger.error(f"Error accessing live-drafts channel: {e}")


async def remove_live_draft_summary_after_delay(bot, draft_session_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session or not draft_session.live_draft_message_id:
            return
            
        guild = bot.get_guild(int(draft_session.guild_id))
        if not guild:
            return
            
        live_drafts_channel = discord.utils.get(guild.text_channels, name="live-drafts")
        if not live_drafts_channel:
            return
            
        try:
            message = await live_drafts_channel.fetch_message(int(draft_session.live_draft_message_id))
            await message.delete()
        except discord.NotFound:
            pass  # Message already deleted
        except Exception as e:
            print(f"Failed to delete live draft summary: {e}")
            
        # Clear the message ID from the session
        draft_session.live_draft_message_id = None
        session.add(draft_session)
        await session.commit()


async def re_register_live_drafts(bot):
    """Re-register all live draft summaries on bot restart"""
    current_time = datetime.now()

    async with AsyncSessionLocal() as db_session:
        # First, clean up live draft messages for completed drafts
        # (these may have been left behind if the bot restarted before the cleanup task ran)
        cleanup_stmt = select(DraftSession).where(
            DraftSession.session_stage == "completed",
            DraftSession.live_draft_message_id.isnot(None)
        )
        cleanup_result = await db_session.execute(cleanup_stmt)
        completed_drafts = cleanup_result.scalars().all()

        for draft_session in completed_drafts:
            logger.info(f"Cleaning up live draft message for completed draft {draft_session.session_id}")
            guild = bot.get_guild(int(draft_session.guild_id))
            if guild:
                live_drafts_channel = discord.utils.get(guild.text_channels, name="live-drafts")
                if live_drafts_channel:
                    try:
                        message = await live_drafts_channel.fetch_message(int(draft_session.live_draft_message_id))
                        await message.delete()
                    except discord.NotFound:
                        pass  # Already deleted
                    except Exception as e:
                        logger.error(f"Failed to delete live draft message: {e}")

            # Clear the message ID
            draft_session.live_draft_message_id = None
            db_session.add(draft_session)

        await db_session.commit()

        # Now handle active drafts that are still in pairings
        stmt = select(DraftSession).where(
            DraftSession.deletion_time > current_time,
            DraftSession.session_stage == "pairings",
            DraftSession.live_draft_message_id.isnot(None)
        )
        result = await db_session.execute(stmt)
        draft_sessions = result.scalars().all()

        for draft_session in draft_sessions:
            # Update the live draft summary
            await update_live_draft_summary(bot, draft_session.session_id)