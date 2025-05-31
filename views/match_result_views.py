"""
Match result related views and functionality for the draft bot.
"""

import discord
from discord.ui import View, Button, Select
from typing import Optional
from loguru import logger
from sqlalchemy import select

from views.view_helpers import BaseView, ResponseHelper, DatabaseHelper
from session import AsyncSessionLocal, MatchResult, DraftSession
from utils import fetch_match_details, update_draft_summary_message, check_and_post_victory_or_draw
from utils import update_player_stats_and_elo
from livedrafts import update_live_draft_summary
from views.stake_views import StakeCalculationButton


class MatchResultButton(Button):
    """Button for reporting match results."""
    
    def __init__(self, bot, session_id: str, match_id: int, match_number: int, 
                 label: str, *args, **kwargs):
        super().__init__(label=label, *args, **kwargs)
        self.bot = bot
        self.session_id = session_id
        self.match_id = match_id
        self.match_number = match_number

    async def callback(self, interaction: discord.Interaction):
        """Handle match result button click."""
        await interaction.response.defer()

        # Fetch player names
        player1_name, player2_name = await fetch_match_details(
            self.bot, self.session_id, self.match_number
        )
        
        # Create select menu for reporting result
        match_result_select = MatchResultSelect(
            match_number=self.match_number,
            bot=self.bot,
            session_id=self.session_id,
            player1_name=player1_name,
            player2_name=player2_name
        )

        # Create and send view with select menu
        view = View(timeout=None)
        view.add_item(match_result_select)
        await interaction.followup.send(
            "Please select the match result:", 
            view=view, 
            ephemeral=True
        )


class MatchResultSelect(Select):
    """Select menu for choosing match results."""
    
    def __init__(self, bot, match_number: int, session_id: str, 
                 player1_name: str, player2_name: str, *args, **kwargs):
        self.bot = bot
        self.match_number = match_number
        self.session_id = session_id

        options = [
            discord.SelectOption(label=f"{player1_name} wins: 2-0", value="2-0-1"),
            discord.SelectOption(label=f"{player1_name} wins: 2-1", value="2-1-1"),
            discord.SelectOption(label=f"{player2_name} wins: 2-0", value="0-2-2"),
            discord.SelectOption(label=f"{player2_name} wins: 2-1", value="1-2-2"),
            discord.SelectOption(label="No Match Played", value="0-0-0"),
        ]
        
        super().__init__(
            placeholder=f"{player1_name} v. {player2_name}", 
            min_values=1, 
            max_values=1, 
            options=options, 
            *args, 
            **kwargs
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle match result selection."""
        await interaction.response.defer()
        
        # Parse the selected value
        player1_wins, player2_wins, winner_indicator = self.values[0].split('-')
        player1_wins = int(player1_wins)
        player2_wins = int(player2_wins)
        winner_id = None

        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Fetch match result and draft session
                stmt = select(MatchResult, DraftSession).join(DraftSession).where(
                    MatchResult.session_id == self.session_id,
                    MatchResult.match_number == self.match_number
                )
                result = await session.execute(stmt)
                match_result, draft_session = result.first()
                
                if match_result:
                    # Update match result
                    match_result.player1_wins = player1_wins
                    match_result.player2_wins = player2_wins
                    
                    if winner_indicator != '0':
                        winner_id = (match_result.player1_id if winner_indicator == '1' 
                                   else match_result.player2_id)
                    match_result.winner_id = winner_id

                    await session.commit()
                    
                    # Update player stats for random/staked drafts
                    if draft_session and draft_session.session_type in ("random", "staked"):
                        await update_player_stats_and_elo(match_result)
                   
        # Update various messages
        await update_draft_summary_message(self.bot, self.session_id)
        await update_live_draft_summary(self.bot, self.session_id)
        
        if draft_session.session_type != "test":
            await check_and_post_victory_or_draw(self.bot, self.session_id)
            
        await self._update_pairings_posting(interaction, self.bot, self.session_id, self.match_number)

    async def _update_pairings_posting(self, interaction: discord.Interaction, bot, 
                                     draft_session_id: str, match_number: int):
        """Update the pairings message with match results."""
        guild = bot.get_guild(int(interaction.guild_id))
        if not guild:
            logger.error("Guild not found")
            return

        async with AsyncSessionLocal() as session:
            # Fetch match result
            stmt = select(MatchResult).where(
                MatchResult.session_id == draft_session_id,
                MatchResult.match_number == match_number
            )
            result = await session.execute(stmt)
            match_result = result.scalar_one_or_none()

            if not match_result or not match_result.pairing_message_id:
                logger.error(f"Match result or pairing message not found for match {match_number}")
                return

            # Fetch draft session
            draft_session = await DatabaseHelper.get_draft_session_safe(draft_session_id)
            if not draft_session:
                logger.error("Draft session not found")
                return

            channel = guild.get_channel(int(draft_session.draft_chat_channel))
            if not channel:
                logger.error("Channel not found")
                return

            # Fetch and update the message
            try:
                message = await channel.fetch_message(int(match_result.pairing_message_id))
                embed = message.embeds[0] if message.embeds else None
                if not embed:
                    logger.error("No embed found in pairings message")
                    return

                # Get player names
                player1 = guild.get_member(int(match_result.player1_id))
                player2 = guild.get_member(int(match_result.player2_id))
                player1_name = player1.display_name if player1 else 'Unknown'
                player2_name = player2.display_name if player2 else 'Unknown'

                # Determine winning team emoji
                winning_team_emoji = "⚫ "
                if match_result.winner_id:
                    if match_result.winner_id in draft_session.team_a:
                        winning_team_emoji = "🔴 "
                    elif match_result.winner_id in draft_session.team_b:
                        winning_team_emoji = "🔵 "
                
                # Update embed field
                updated_value = (f"{winning_team_emoji}**Match {match_result.match_number}**\n"
                               f"{player1_name}: {match_result.player1_wins} wins\n"
                               f"{player2_name}: {match_result.player2_wins} wins")
                
                # Find and update the correct field
                for i, field in enumerate(embed.fields):
                    if (f"Match {match_result.match_number}" in field.value and 
                        player1_name in field.value and player2_name in field.value):
                        embed.set_field_at(i, name=field.name, value=updated_value, inline=field.inline)
                        break
                
                # Create updated view
                new_view = await self._create_updated_pairings_view(
                    bot, guild.id, draft_session_id, match_result.pairing_message_id
                )
                
                await message.edit(embed=embed, view=new_view)
                
            except Exception as e:
                logger.error(f"Error updating pairings message: {e}")

    async def _create_updated_pairings_view(self, bot, guild_id: int, 
                                          draft_session_id: str, pairing_message_id: str) -> View:
        """Create an updated view for the pairings message with correct button styles."""
        view = View(timeout=None)
        
        async with AsyncSessionLocal() as session:
            # Fetch draft session
            draft_session = await DatabaseHelper.get_draft_session_safe(draft_session_id)
            
            # Fetch match results for this pairing message
            stmt = select(MatchResult).where(
                MatchResult.session_id == draft_session_id,
                MatchResult.pairing_message_id == pairing_message_id
            )
            result = await session.execute(stmt)
            match_results = result.scalars().all()

            for match_result in match_results:
                # Determine button style based on winner
                button_style = discord.ButtonStyle.secondary
                
                if match_result.winner_id and draft_session:
                    if match_result.winner_id in draft_session.team_a:
                        button_style = discord.ButtonStyle.danger  # Red
                    elif match_result.winner_id in draft_session.team_b:
                        button_style = discord.ButtonStyle.blurple  # Blue
                
                # Create button with appropriate style
                button = MatchResultButton(
                    bot=bot,
                    session_id=draft_session_id,
                    match_id=match_result.id,
                    match_number=match_result.match_number,
                    label=f"Match {match_result.match_number} Results",
                    style=button_style
                )
                view.add_item(button)

        return view


async def create_pairings_view(bot, guild, session_id: str, match_results) -> View:
    """Create a view with buttons for each match result."""
    view = View(timeout=None)
    
    for match_result in match_results:
        button = MatchResultButton(
            bot=bot,
            session_id=session_id,
            match_id=match_result.id,
            match_number=match_result.match_number,
            label=f"Match {match_result.match_number} Results",
            style=discord.ButtonStyle.secondary,
            row=None
        )
        view.add_item(button)
        
    return view