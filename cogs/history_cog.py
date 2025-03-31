import discord
from discord import ButtonStyle
from discord.ui import View
from sqlalchemy import and_, or_, func, select
from datetime import datetime
from discord.ext import commands
from loguru import logger
from session import AsyncSessionLocal, DraftSession
from models.match import MatchResult

SEATING_ORDER_FIX = 1742144400

class HistoryView(View):
    def __init__(self, pages, author_id):
        super().__init__(timeout=None)
        self.pages = pages
        self.author_id = author_id
        self.current_page = 0
    
    async def interaction_check(self, interaction):
        # Only the command author can use these buttons
        return interaction.user.id == self.author_id
    
    @discord.ui.button(label="◀️ Previous", style=ButtonStyle.blurple)
    async def previous(self, button, interaction):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.pages[self.current_page])
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="▶️ Next", style=ButtonStyle.blurple)
    async def next(self, button, interaction):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.pages[self.current_page])
        else:
            await interaction.response.defer()
    
    # @discord.ui.button(label="🏆 Wins", style=ButtonStyle.green)
    # async def show_wins(self, button, interaction):
    #     await interaction.response.send_message("Filtering for wins coming soon!", ephemeral=True)
    
    # @discord.ui.button(label="❌ Losses", style=ButtonStyle.red)
    # async def show_losses(self, button, interaction):
    #     await interaction.response.send_message("Filtering for losses coming soon!", ephemeral=True)

def format_first_picks(pack_picks):
    """Format the first picks from each pack into a string."""
    if not pack_picks or len(pack_picks) == 0:
        return "First picks unavailable"
    
    formatted_picks = []
    # Sort by pack number (pack numbers are stored as strings in JSON)
    for pack_num in sorted([int(pn) for pn in pack_picks.keys()]):
        pick = pack_picks.get(str(pack_num), "Unknown")
        formatted_picks.append(f"P{int(pack_num)+1}P1: {pick}")
    
    return " | ".join(formatted_picks)

class HistoryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("History cog loaded")
    
    @discord.slash_command(name="draft_history", description="View your draft history")
    async def draft_history(self, ctx):
        """Display draft history for the user."""
        await ctx.defer()  # Defer reply since this might take a while
        
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        limit = 5  # Hardcoded to show 5 per page
        
        async with AsyncSessionLocal() as db_session:
            # Query for drafts where this user participated
            query = select(DraftSession).where(
                and_(
                    DraftSession.guild_id == guild_id,
                    DraftSession.victory_message_id_draft_chat.isnot(None),  # Has victory message
                    or_(
                        DraftSession.session_type == "random",
                        DraftSession.session_type == "staked"
                    ),
                    # Check if user is in sign_ups
                    func.json_extract(DraftSession.sign_ups, f'$."{user_id}"').isnot(None)
                )
            ).order_by(DraftSession.teams_start_time.desc())
            
            result = await db_session.execute(query)
            all_drafts = result.scalars().all()
            
            if not all_drafts:
                return await ctx.followup.send("No draft history found for you in this server.")
            
            # Create pages for pagination
            pages = []
            total_pages = (len(all_drafts) + limit - 1) // limit  # Ceiling division for total pages
            
            for i in range(0, len(all_drafts), limit):
                page_drafts = all_drafts[i:i+limit]
                current_page = (i // limit) + 1  # Calculate current page number (1-based)
                
                embed = discord.Embed(
                    title=f"Draft History for {ctx.author.display_name}",
                    color=0x3498db
                )
                
                # Set the footer with page information
                embed.set_footer(text=f"Showing Page {current_page} of {total_pages} ({len(all_drafts)} Total Drafts)")
                
                for draft in page_drafts:
                    try:
                        # Get match results for this draft
                        match_results_stmt = select(MatchResult).filter(MatchResult.session_id == draft.session_id)
                        match_results = await db_session.execute(match_results_stmt)
                        match_results = match_results.scalars().all()
                        
                        # Calculate user's record
                        wins = 0
                        losses = 0
                        for match in match_results:
                            if match.winner_id == user_id:
                                wins += 1
                            elif match.winner_id and (match.player1_id == user_id or match.player2_id == user_id):
                                losses += 1
                        
                        # Determine if user was team A or team B
                        team_a = draft.team_a or {}
                        team_b = draft.team_b or {}
                        user_team = "A" if user_id in team_a else "B"
                        opposing_team = "B" if user_team == "A" else "A"
                        
                        # Get team members and opponents
                        teammates = []
                        opponents = []
                        sign_ups = draft.sign_ups or {}
                        
                        for member_id, member_info in sign_ups.items():
                            # Extract member name from sign_ups (handling both string and dict formats)
                            if isinstance(member_info, dict) and "name" in member_info:
                                member_name = member_info["name"]
                            else:
                                member_name = member_info
                            
                            # Determine records for teammates and opponents
                            member_wins = 0
                            member_losses = 0
                            for match in match_results:
                                if match.winner_id == member_id:
                                    member_wins += 1
                                elif match.winner_id and (match.player1_id == member_id or match.player2_id == member_id):
                                    member_losses += 1
                            
                            record_str = f" ({member_wins}-{member_losses})"
                            trophy = " 🏆" if member_wins == 3 else ""
                            
                            # Add to teammates or opponents list
                            if (user_team == "A" and member_id in team_a) or (user_team == "B" and member_id in team_b):
                                if member_id != user_id:  # Don't include the user in teammates
                                    teammates.append(f"{trophy}{member_name}{record_str}")
                            else:
                                opponents.append(f"{trophy}{member_name}{record_str}")
                        
                        # Determine team scores
                        team_a_score = sum(1 for m in match_results if m.winner_id in team_a)
                        team_b_score = sum(1 for m in match_results if m.winner_id in team_b)

                        should_show_seating = draft.teams_start_time and draft.teams_start_time.timestamp() > SEATING_ORDER_FIX
                        if should_show_seating:  
                            # Get the ordered list of players
                            all_player_ids = list(sign_ups.keys())
                            total_players = len(all_player_ids)
                            
                            # Find the user's position in the list
                            user_position = None
                            for idx, player_id in enumerate(all_player_ids):
                                if player_id == user_id:
                                    user_position = idx
                                    break
                            
                            # Get players to the left and right
                            left_position = (user_position - 1) % total_players if user_position is not None else None
                            right_position = (user_position + 1) % total_players if user_position is not None else None
                            
                            left_player_id = all_player_ids[left_position] if left_position is not None else None
                            right_player_id = all_player_ids[right_position] if right_position is not None else None
                            
                            # Get player names
                            left_player_name = "Unknown"
                            if left_player_id in sign_ups:
                                left_player_info = sign_ups[left_player_id]
                                if isinstance(left_player_info, dict) and "name" in left_player_info:
                                    left_player_name = left_player_info["name"]
                                else:
                                    left_player_name = left_player_info
                            
                            right_player_name = "Unknown"
                            if right_player_id in sign_ups:
                                right_player_info = sign_ups[right_player_id]
                                if isinstance(right_player_info, dict) and "name" in right_player_info:
                                    right_player_name = right_player_info["name"]
                                else:
                                    right_player_name = right_player_info
                            
                            user_name = ctx.author.display_name
                            if user_id in sign_ups:
                                user_info = sign_ups[user_id]
                                if isinstance(user_info, dict) and "name" in user_info:
                                    user_name = user_info["name"]
                                else:
                                    user_name = user_info
                            seating_line = f"Draft Seat: {left_player_name} -> **{user_name}** -> {right_player_name}\n"
                        else:
                            seating_line = ""
                            
                        # Determine user's team score and opponent's team score
                        user_team_score = team_a_score if user_team == "A" else team_b_score
                        opponent_team_score = team_b_score if user_team == "A" else team_a_score

                        # Determine outcome with emoji
                        if user_team_score > opponent_team_score:
                            outcome = "✅ **Win**"
                        elif user_team_score == opponent_team_score:
                            outcome = "🔄 **Draw**"
                        else:
                            outcome = "❌ **Loss**"

                        # Get first picks information
                        user_first_picks = {}
                        if draft.pack_first_picks and user_id in draft.pack_first_picks:
                            user_first_picks = draft.pack_first_picks[user_id]

                        first_picks_text = format_first_picks(user_first_picks)

                        # Format date
                        draft_date = draft.teams_start_time.strftime('%m/%d/%Y') if draft.teams_start_time else "Unknown date"

                        # Add trophy emoji if user went 3-0
                        trophy_emoji = " 🏆" if wins == 3 else ""

                        # Get MagicProTools link if available
                        mpt_link = ""
                        if draft.magicprotools_links and user_id in draft.magicprotools_links:
                            link_info = draft.magicprotools_links[user_id]
                            if "link" in link_info:
                                mpt_link = f"\n[View Draft in MagicProTools]({link_info['link']})"

                        # Determine draft type
                        draft_type = "Money" if draft.session_type.lower() == "staked" else "Team"

                        # Create field for this draft
                        field_title = f"[{draft_date}] {draft.cube} {draft_type} Draft"
                        field_value = (
                            f"{outcome}: {user_team_score}-{opponent_team_score} | Personal Record: {wins}-{losses}{trophy_emoji}\n"
                            f"{seating_line}"
                            f"{first_picks_text}\n"
                            f"👥 Teammates: {', '.join(teammates) if teammates else 'None'}\n"
                            f"⚔️ Opponents: {', '.join(opponents)}"
                            f"{mpt_link}"
                        )
                        
                        embed.add_field(name=field_title, value=field_value, inline=False)
                    except Exception as e:
                        logger.error(f"Error processing draft {draft.session_id}: {e}")
                        # Add a simple error message for this draft instead of skipping it entirely
                        embed.add_field(
                            name=f"[Error] Draft {draft.session_id}",
                            value=f"There was an error processing this draft entry.",
                            inline=False
                        )
                
                pages.append(embed)
            
            # Send the paginated message
            view = HistoryView(pages=pages, author_id=ctx.author.id)
            await ctx.followup.send(embed=pages[0], view=view)

def setup(bot):
    bot.add_cog(HistoryCog(bot))