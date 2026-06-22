import discord
from discord.ext import commands
import asyncio
from typing import cast
from loguru import logger
from models.draft_session import DraftSession
from models.match import MatchResult
from discord.ui import View, Button
from datetime import datetime, timedelta
from sqlalchemy import select, and_, desc, update
from database.db_session import db_session
from helpers.utils import not_none
from services.draft_setup_manager import DraftSetupManager, create_rooms_and_pairings_with_fallback

# Store active unpause ready checks
ACTIVE_UNPAUSE_CHECKS = {}
ACTIVE_SCRAP_VOTES = {}
ACTIVE_LOG_RELEASE_VOTES = {}
ACTIVE_ABANDON_VOTES = {}

# Hours until an abandoned draft's channels are cleaned up by the deletion task.
ABANDON_CLEANUP_HOURS = 2


async def abandon_draft_session(session_id, session_factory=None):
    """Void all match results for a draft and mark it abandoned.

    Sets every MatchResult back to unplayed (no winner, zero game wins,
    no submission time) and flags the draft ``session_stage='abandoned'`` with a
    ``deletion_time`` so the existing cleanup task removes its channels. Only
    intended for not-yet-completed drafts (the caller enforces that).
    """
    if session_factory is None:
        from session import AsyncSessionLocal
        session_factory = AsyncSessionLocal

    async with session_factory() as db:
        async with db.begin():
            await db.execute(
                update(MatchResult)
                .where(MatchResult.session_id == session_id)
                .values(winner_id=None, player1_wins=0, player2_wins=0, result_submitted_at=None)
            )
            await db.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id)
                .values(
                    session_stage="abandoned",
                    deletion_time=datetime.now() + timedelta(hours=ABANDON_CLEANUP_HOURS),
                )
            )


def _disable_all(view):
    """Disable every component on a view (so a settled prompt can't be re-clicked)."""
    for child in view.children:
        cast(discord.ui.Button, child).disabled = True


class BaseVoteView(View):
    """A majority yes/no vote among draft participants.

    Subclasses only set the appearance/wording class attributes below; all the
    voting, tallying, early-exit, timeout and embed logic lives here. The
    command that creates the view sends it, starts ``start_timer``, awaits
    ``complete``, then acts on ``get_vote_result``.
    """

    # --- per-vote appearance / wording (override in subclasses) ---
    embed_title = "Vote"
    embed_description = "Vote."
    embed_color = discord.Color.blurple()
    yes_label = "Yes"
    no_label = "No"
    yes_style = discord.ButtonStyle.primary
    no_style = discord.ButtonStyle.secondary
    yes_status = "✅ Voted Yes"
    no_status = "❌ Voted No"
    action_verb = "pass"   # used in "votes needed to {action_verb}"
    log_name = "vote"      # used in timeout log lines

    def __init__(self, draft_session_id, participants, timeout: float = 90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        self._timeout_seconds: float = timeout
        self.votes: dict[str, bool | None] = {user_id: None for user_id in participants}  # None=not voted, True=yes, False=no
        self.message: discord.Message | None = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()
        # Apply the subclass's appearance to the decorator-created buttons.
        self.yes_button.label, self.yes_button.style = self.yes_label, self.yes_style
        self.no_button.label, self.no_button.style = self.no_label, self.no_style

    async def start_timer(self):
        try:
            await asyncio.sleep(self._timeout_seconds)
            if not self.complete.is_set():
                logger.info(f"{self.log_name} for session {self.draft_session_id} timed out")
                await self.on_timeout()
        except asyncio.CancelledError:
            logger.debug(f"Timer for {self.log_name} {self.draft_session_id} was cancelled")

    def get_vote_result(self):
        """Return (passed, yes_votes, total): passes when more than half vote yes."""
        yes_votes = sum(1 for vote in self.votes.values() if vote is True)
        total_participants = len(self.votes)
        needed_votes = (total_participants // 2) + 1
        return yes_votes >= needed_votes, yes_votes, total_participants

    def can_still_pass(self):
        """True while the remaining unvoted participants could still form a majority."""
        _, yes_votes, total = self.get_vote_result()
        remaining = sum(1 for v in self.votes.values() if v is None)
        return yes_votes + remaining >= (total // 2) + 1

    def _finish(self):
        self.complete.set()
        if self.timer_task:
            self.timer_task.cancel()
        _disable_all(self)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.primary)
    async def yes_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        user_id = str(not_none(interaction.user).id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
        self.votes[user_id] = True
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        if self.get_vote_result()[0]:
            self._finish()
            if self.message:
                await self.message.edit(view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        user_id = str(not_none(interaction.user).id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
        self.votes[user_id] = False
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        # End early if the vote can no longer reach a majority.
        if not self.can_still_pass():
            self._finish()
            if self.message:
                await self.message.edit(view=self)

    def _participant_status(self, vote):
        if vote is True:
            return self.yes_status
        if vote is False:
            return self.no_status
        return "⏳ Not Voted"

    async def generate_status_embed(self, guild):
        embed = discord.Embed(
            title=self.embed_title, description=self.embed_description, color=self.embed_color
        )
        status_lines = []
        for user_id, vote in self.votes.items():
            member = guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            status_lines.append(f"{name}: {self._participant_status(vote)}")
        embed.add_field(name="Participants", value="\n".join(status_lines) or "No participants found", inline=False)

        passed, yes_votes, total_participants = self.get_vote_result()
        needed_votes = (total_participants // 2) + 1
        embed.add_field(
            name="Vote Status",
            value=f"**{yes_votes}/{needed_votes}** votes needed to {self.action_verb}\n" +
                  ("**Vote will pass!**" if passed else "**Vote will not pass yet**"),
            inline=False,
        )
        expiry_timestamp = int(self._start_time.timestamp() + self._timeout_seconds)
        embed.add_field(name="Vote Ends", value=f"<t:{expiry_timestamp}:R>", inline=False)
        return embed

    async def on_timeout(self):
        self.complete.set()
        _disable_all(self)
        if self.message:
            passed, yes_votes, total_participants = self.get_vote_result()
            needed_votes = (total_participants // 2) + 1
            embed = discord.Embed(
                title=f"{self.embed_title} - Ended",
                description="The vote has ended.",
                color=self.embed_color,
            )
            embed.add_field(
                name="Final Results",
                value=f"**{yes_votes}/{needed_votes}** votes needed to {self.action_verb}\n" +
                      ("**Vote passed!**" if passed else "**Vote did not pass**"),
                inline=False,
            )
            await self.message.edit(embed=embed, view=self)


class AbandonVoteView(BaseVoteView):
    """Majority vote to abandon a draft and void all its matches."""
    embed_title = "Draft Abandonment Vote"
    embed_description = "Vote to abandon the current draft. All match results will be voided."
    embed_color = discord.Color.red()
    yes_label = "Yes, Abandon Draft"
    yes_style = discord.ButtonStyle.danger
    no_label = "No, Keep Draft"
    no_style = discord.ButtonStyle.green
    yes_status = "✅ Voted to Abandon"
    no_status = "❌ Voted to Keep"
    action_verb = "abandon"
    log_name = "Abandon vote"


class AbandonConfirmView(View):
    """Ephemeral admin confirmation before an immediate abandon."""

    def __init__(self, session_id, channel, timeout=60.0):
        super().__init__(timeout=timeout)
        self.session_id = session_id
        self.channel = channel

    @discord.ui.button(label="Yes, Abandon", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        _disable_all(self)
        await abandon_draft_session(self.session_id)
        await interaction.response.edit_message(
            content="🛑 Draft abandoned. All match results have been voided.", view=self
        )
        await self.channel.send(
            "🛑 **This draft has been abandoned by an admin.** All match results have been voided."
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        _disable_all(self)
        await interaction.response.edit_message(content="Abandon cancelled.", view=self)


class LogReleaseVoteView(BaseVoteView):
    """Majority vote to release the draft logs early."""
    embed_title = "Draft Logs Release Vote"
    embed_description = "Vote to release the draft logs early."
    embed_color = discord.Color.blue()
    yes_label = "Yes, Release Logs"
    yes_style = discord.ButtonStyle.primary
    no_label = "No, Keep Logs Private"
    no_style = discord.ButtonStyle.secondary
    yes_status = "✅ Voted to Release"
    no_status = "❌ Voted to Keep Private"
    action_verb = "release logs"
    log_name = "Log release vote"

ACTIVE_REPLACE_VOTES = {}

class ReplaceWithBotsVoteView(BaseVoteView):
    """Majority vote to replace disconnected players with bots."""
    embed_title = "Replace Disconnected Players Vote"
    embed_description = "Vote to replace disconnected players with bots."
    embed_color = discord.Color.blue()
    yes_label = "Yes, Replace with Bots"
    yes_style = discord.ButtonStyle.primary
    no_label = "No, Wait for Players"
    no_style = discord.ButtonStyle.secondary
    yes_status = "✅ Replace with Bots"
    no_status = "❌ Wait for Players"
    action_verb = "replace"
    log_name = "Replace with bots vote"

class DraftMancerReadyCheckView(View):
    def __init__(self, draft_session_id, participants, timeout: float = 90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        self._timeout_seconds: float = timeout
        self.participants: dict[str, bool] = {user_id: False for user_id in participants}  # False = not ready
        self.message: discord.Message | None = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()
        
    async def start_timer(self):
        """Start the timeout timer for the ready check"""
        try:
            await asyncio.sleep(self._timeout_seconds)
            if not self.complete.is_set():
                # Time's up, mark the check as complete
                logger.info(f"Unpause ready check for session {self.draft_session_id} timed out")
                await self.on_timeout()
        except asyncio.CancelledError:
            logger.debug(f"Timer for unpause ready check {self.draft_session_id} was cancelled")
    
    def is_everyone_ready(self):
        """Check if all participants are ready"""
        return all(self.participants.values())
    
    @discord.ui.button(label="Ready", style=discord.ButtonStyle.green)
    async def ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Mark user as ready"""
        user_id = str(not_none(interaction.user).id)
        if user_id not in self.participants:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Mark user as ready
        self.participants[user_id] = True
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Check if everyone is ready
        if self.is_everyone_ready():
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                cast(discord.ui.Button, child).disabled = True
                
            if self.message:
                await self.message.edit(view=self)
            # The on_complete callback will handle the actual resuming
        
    @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red)
    async def not_ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Mark user as not ready"""
        user_id = str(not_none(interaction.user).id)
        if user_id not in self.participants:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Mark user as not ready
        self.participants[user_id] = False
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def generate_status_embed(self, guild):
        """Generate an embed showing the current ready status"""
        embed = discord.Embed(
            title="Draft Unpause - Ready Check",
            description="All players must click Ready to resume the draft.",
            color=discord.Color.blue()
        )
        
        # Add a field showing status for each participant
        status_lines = []
        for user_id, is_ready in self.participants.items():
            # Try to get member name
            member = guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            
            # Add status emoji
            status = "✅ Ready" if is_ready else "❌ Not Ready"
            status_lines.append(f"{name}: {status}")
        
        embed.add_field(
            name="Players",
            value="\n".join(status_lines) or "No players found",
            inline=False
        )
        
        expiry_time = self._start_time.timestamp() + self._timeout_seconds
        expiry_timestamp = int(expiry_time)

        embed.add_field(
            name="Time Remaining",
            value=f"Ready check expires: <t:{expiry_timestamp}:R>",
            inline=False
        )
        return embed
        
    async def on_timeout(self):
        """Called when the view times out"""
        self.complete.set()
        
        # Disable all buttons
        for child in self.children:
            cast(discord.ui.Button, child).disabled = True
            
        # Update the message if it exists
        if self.message:
            embed = discord.Embed(
                title="Draft Unpause - Timed Out",
                description="The ready check has expired.",
                color=discord.Color.red()
            )
            await self.message.edit(embed=embed, view=self)
        
        # Remove from active checks
        if self.draft_session_id in ACTIVE_UNPAUSE_CHECKS:
            del ACTIVE_UNPAUSE_CHECKS[self.draft_session_id]

class ScrapVoteView(BaseVoteView):
    """Majority vote to cancel the current draft."""
    embed_title = "Draft Cancellation Vote"
    embed_description = "Vote to cancel the current draft."
    embed_color = discord.Color.red()
    yes_label = "Yes, Cancel Draft"
    yes_style = discord.ButtonStyle.danger
    no_label = "No, Continue Draft"
    no_style = discord.ButtonStyle.green
    yes_status = "✅ Voted to Cancel"
    no_status = "❌ Voted to Continue"
    action_verb = "cancel"
    log_name = "Scrap vote"


class DraftControlCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logger
        logger.info("Draft control commands registered")

    async def _get_manager_for_channel(self, ctx):
        """
        Helper to get the draft manager for draft control commands
        This checks for any draft with a non-NULL session_stage
        """
        channel_id = str(ctx.channel.id)
        logger.info(f"Looking for active drafts in channel {channel_id}")
        
        # Get all active drafts in this channel (with non-NULL session_stage)
        async with db_session() as session:
            stmt = select(DraftSession).where(
                and_(
                    DraftSession.draft_channel_id == channel_id,
                    DraftSession.session_stage.isnot(None)  # Any non-NULL session_stage
                )
            ).order_by(desc(DraftSession.draft_start_time))
            
            result = await session.execute(stmt)
            draft_sessions = result.scalars().all()
        
        if not draft_sessions:
            logger.warning(f"No active drafts found in channel {channel_id}")
            await ctx.followup.send("No active drafts found in this channel.", ephemeral=True)
            return None
        
        # Take the most recent draft
        draft_session = draft_sessions[0]
        logger.info(f"Found most recent draft: {draft_session.session_id}")
        
        # Get the manager for this session
        manager = DraftSetupManager.get_active_manager(draft_session.session_id)
        if not manager:
            logger.warning(f"No active manager found for session ID: {draft_session.session_id}")
            await ctx.followup.send("Draft manager is not active. Please wait a moment or contact an admin.", ephemeral=True)
            return None
        
        return manager, draft_session

    @discord.slash_command(name='replace_with_bots', description='Start a vote to replace disconnected players with bots')
    async def replace_with_bots_command(self, ctx):
        """Start a vote to replace disconnected players with bots"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Check if draft has started
            if not manager.drafting:
                await ctx.followup.send("Draft hasn't started yet.", ephemeral=True)
                return
                
            # Check if draft is paused
            if not manager.draftPaused:
                await ctx.followup.send("The draft must be paused before disconnected players can be replaced. Use `/pause` first.", ephemeral=True)
                return
            
            # Check if there's already an active replace vote
            if draft_session.session_id in ACTIVE_REPLACE_VOTES:
                await ctx.followup.send("There's already an active vote to replace disconnected players.", ephemeral=True)
                return
                
            # Check if user is in sign_ups
            user_id = str(ctx.author.id)
            sign_ups = draft_session.sign_ups or {}
            
            is_participant = False
            for discord_id, display_name in sign_ups.items():
                if discord_id == user_id:
                    is_participant = True
                    break
                    
            if not is_participant:
                await ctx.followup.send("Only draft participants can initiate a vote to replace disconnected players.", ephemeral=True)
                return
                
            # Get all participants
            participants = list(sign_ups.keys())
            if not participants:
                await ctx.followup.send("No draft participants found.", ephemeral=True)
                return
            
            # Create replace vote view
            view = ReplaceWithBotsVoteView(draft_session.session_id, participants)
            
            # Format the pings for the message
            user_pings = []
            for player_id in sign_ups:
                try:
                    member = ctx.guild.get_member(int(player_id))
                    if member:
                        user_pings.append(member.mention)
                except:
                    pass
                    
            ping_text = " ".join(user_pings) if user_pings else "No players to ping."
            
            # Generate initial status embed
            embed = await view.generate_status_embed(ctx.guild)
            
            # Send message with pings and view
            message = await ctx.channel.send(
                f"🤖 **Vote to Replace Disconnected Players** initiated by {ctx.author.mention}\n\n"
                f"{ping_text}\n\n"
                f"Please vote on whether to replace disconnected players with bots.",
                embed=embed,
                view=view
            )
            
            # Store message reference
            view.message = message
            
            # Store in active votes
            ACTIVE_REPLACE_VOTES[draft_session.session_id] = view
            
            # Start timeout timer
            view.timer_task = asyncio.create_task(view.start_timer())
            
            # Acknowledge command
            await ctx.followup.send("Vote to replace disconnected players initiated.", ephemeral=True)
            
            # Wait for completion
            try:
                await view.complete.wait()
                
                # Check if vote passed
                passed, yes_votes, total_participants = view.get_vote_result()
                if passed:
                    # Vote passed, replace disconnected players
                    final_message = await ctx.channel.send("✅ **Vote passed!** Replacing disconnected players with bots...")
                    
                    # Send replaceDisconnectedPlayers command to Draftmancer
                    await manager.socket_client.emit('replaceDisconnectedPlayers')
                    
                    await final_message.edit(content="🤖 **Disconnected players replaced with bots!** The draft can now be resumed with `/unpause`.")
                else:
                    # Vote didn't pass
                    await ctx.channel.send("❌ **Vote to replace disconnected players did not pass.**\nPlayers will need to reconnect before the draft can continue.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_REPLACE_VOTES:
                    del ACTIVE_REPLACE_VOTES[draft_session.session_id]
                    
            except Exception as e:
                logger.exception(f"Error while waiting for replace vote completion: {e}")
                await ctx.channel.send("⚠️ An error occurred during the vote. Please try again.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_REPLACE_VOTES:
                    del ACTIVE_REPLACE_VOTES[draft_session.session_id]
                    
        except Exception as e:
            logger.exception(f"Error in replace_with_bots command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
            
    @discord.slash_command(name='ready', description='Start a ready check for the Draftmancer draft')
    async def ready_command(self, ctx):
        """Initiate a ready check for all players in the draft"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, _ = result
            
            # Check if there's already an active ready check
            if manager.ready_check_active:
                await ctx.followup.send("A ready check is already in progress.", ephemeral=True)
                return
                
            # Initiate ready check
            success = await manager.initiate_ready_check(self.bot)
            if success:
                await ctx.followup.send("Ready check initiated!", ephemeral=True)
            else:
                await ctx.followup.send("Failed to initiate ready check. Please try again later.", ephemeral=True)
                
        except Exception as e:
            logger.exception(f"Error in ready command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)


    @discord.slash_command(name='release_draft_logs', description='Start a vote to release draft logs early')
    async def releaselogs_command(self, ctx):
        """Start a vote to release draft logs early"""
        await ctx.defer(ephemeral=True)
        
        try:
            # Find the draft session by chat channel ID
            channel_id = str(ctx.channel.id)
            logger.info(f"Looking for active draft in chat channel {channel_id}")
            
            # Get the draft session for this chat channel
            async with db_session() as session:
                stmt = select(DraftSession).where(
                    DraftSession.draft_chat_channel == channel_id
                )
                
                result = await session.execute(stmt)
                draft_session = result.scalar_one_or_none()
            
            if not draft_session:
                logger.warning(f"No active draft found for chat channel {channel_id}")
                await ctx.followup.send("No active draft found for this channel.", ephemeral=True)
                return
                
            session_id = draft_session.session_id
            
            # Get the manager for this session
            manager = DraftSetupManager.get_active_manager(session_id)
            if not manager:
                logger.warning(f"No active manager found for session ID: {session_id}")
                await ctx.followup.send("Draft manager is not active. Please wait a moment or contact an admin.", ephemeral=True)
                return
                
            # Check if there's already an active log release vote
            if session_id in ACTIVE_LOG_RELEASE_VOTES:
                await ctx.followup.send("There's already an active vote to release logs for this draft.", ephemeral=True)
                return
                
            sign_ups = draft_session.sign_ups or {}
                
            # Get all participants
            participants = list(sign_ups.keys())
            if not participants:
                await ctx.followup.send("No draft participants found.", ephemeral=True)
                return
            
            # Create log release vote view
            view = LogReleaseVoteView(session_id, participants)
            
            # Format the pings for the message
            user_pings = []
            for player_id in sign_ups:
                try:
                    member = ctx.guild.get_member(int(player_id))
                    if member:
                        user_pings.append(member.mention)
                except:
                    pass
                    
            ping_text = " ".join(user_pings) if user_pings else "No players to ping."
            
            # Generate initial status embed
            embed = await view.generate_status_embed(ctx.guild)
            
            # Send message with pings and view
            message = await ctx.channel.send(
                f"📝 **Draft Log Release Vote** initiated by {ctx.author.mention}\n\n{ping_text}\n\n"
                f"Please vote on whether to release the draft logs early.",
                embed=embed,
                view=view
            )
            
            # Store message reference
            view.message = message
            
            # Store in active votes
            ACTIVE_LOG_RELEASE_VOTES[session_id] = view
            
            # Start timeout timer
            view.timer_task = asyncio.create_task(view.start_timer())
            
            # Acknowledge command
            await ctx.followup.send("Log release vote initiated.", ephemeral=True)
            
            # Wait for completion
            try:
                await view.complete.wait()
                
                # Check if vote passed
                passed, yes_votes, total_participants = view.get_vote_result()
                if passed:
                    # Vote passed, release the logs
                    final_message = await ctx.channel.send("✅ **Vote passed!** Releasing logs in 5 seconds...")
                    await asyncio.sleep(5)
                    
                    # Call the method to unlock the logs
                    success = await manager.manually_unlock_draft_logs()
                    
                    if success:
                        await final_message.edit(content="🔓 **Logs have been made public!**")
                    else:
                        await final_message.edit(content="❌ **Failed to release logs.** Please try again later.")
                else:
                    # Vote didn't pass
                    await ctx.channel.send("❌ **Vote to release logs did not pass.** Logs will remain private until the end of the draft.")
                
                # Clean up
                if session_id in ACTIVE_LOG_RELEASE_VOTES:
                    del ACTIVE_LOG_RELEASE_VOTES[session_id]
                    
            except Exception as e:
                logger.exception(f"Error while waiting for log release vote completion: {e}")
                await ctx.channel.send("⚠️ An error occurred during the log release vote. Please try again.")
                
                # Clean up
                if session_id in ACTIVE_LOG_RELEASE_VOTES:
                    del ACTIVE_LOG_RELEASE_VOTES[session_id]
                    
        except Exception as e:
            logger.exception(f"Error in releaselogs command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

            
    @discord.slash_command(name='scrap', description='Start a vote to cancel the current draft')
    async def scrap_command(self, ctx):
        """Start a vote to completely cancel the draft"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Check if draft has started
            if not manager.drafting:
                await ctx.followup.send("Draft hasn't started yet.", ephemeral=True)
                return
                
            # Check if draft is paused
            if not manager.draftPaused:
                await ctx.followup.send("The draft must be paused before it can be canceled. Use `/pause` first.", ephemeral=True)
                return
            
            # Check if there's already an active scrap vote
            if draft_session.session_id in ACTIVE_SCRAP_VOTES:
                await ctx.followup.send("There's already an active vote to cancel this draft.", ephemeral=True)
                return
                
            # Check if user is in sign_ups
            user_id = str(ctx.author.id)
            sign_ups = draft_session.sign_ups or {}
            
            is_participant = False
            for discord_id, display_name in sign_ups.items():
                if discord_id == user_id:
                    is_participant = True
                    break
                    
            if not is_participant:
                await ctx.followup.send("Only draft participants can initiate a vote to cancel.", ephemeral=True)
                return
                
            # Get all participants
            participants = list(sign_ups.keys())
            if not participants:
                await ctx.followup.send("No draft participants found.", ephemeral=True)
                return
            
            # Create scrap vote view
            view = ScrapVoteView(draft_session.session_id, participants)
            
            # Format the pings for the message
            user_pings = []
            for player_id in sign_ups:
                try:
                    member = ctx.guild.get_member(int(player_id))
                    if member:
                        user_pings.append(member.mention)
                except:
                    pass
                    
            ping_text = " ".join(user_pings) if user_pings else "No players to ping."
            
            # Generate initial status embed
            embed = await view.generate_status_embed(ctx.guild)
            
            # Send message with pings and view
            message = await ctx.channel.send(
                f"⚠️ **Draft Cancellation Vote** initiated by {ctx.author.mention}\n\n{ping_text}\n\n"
                f"Please vote on whether to cancel the current draft.",
                embed=embed,
                view=view
            )
            
            # Store message reference
            view.message = message
            
            # Store in active votes
            ACTIVE_SCRAP_VOTES[draft_session.session_id] = view
            
            # Start timeout timer
            view.timer_task = asyncio.create_task(view.start_timer())
            
            # Acknowledge command
            await ctx.followup.send("Cancellation vote initiated.", ephemeral=True)
            
            # Wait for completion
            try:
                await view.complete.wait()
                
                # Check if vote passed
                passed, yes_votes, total_participants = view.get_vote_result()
                if passed:
                    # Vote passed, cancel the draft
                    final_message = await ctx.channel.send("⚠️ **Vote passed!** Canceling draft in 5 seconds...")
                    
                    # Mark the draft as cancelled to skip log collection
                    await manager.mark_draft_cancelled()
                    
                    manager.drafting = False
                    await asyncio.sleep(5)
                    
                    # Send stopDraft command to Draftmancer
                    await manager.socket_client.emit('stopDraft')
                    
                    await final_message.edit(content=
                                            "🛑 **Draft canceled!** \n" \
                                            "Use `/ready` to begin a ready check for a new draft.\n" \
                                            "Use `/mutiny` to remove the bot and take control of the session."
                    )
                else:
                    # Vote didn't pass
                    await ctx.channel.send("🛑 **Vote to cancel draft did not pass.**\n Use `/unpause` to continue the draft.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_SCRAP_VOTES:
                    del ACTIVE_SCRAP_VOTES[draft_session.session_id]
                    
            except Exception as e:
                logger.exception(f"Error while waiting for scrap vote completion: {e}")
                await ctx.channel.send("⚠️ An error occurred during the cancellation vote. Please try again.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_SCRAP_VOTES:
                    del ACTIVE_SCRAP_VOTES[draft_session.session_id]
                    
        except Exception as e:
            logger.exception(f"Error in scrap command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)
            
    @discord.slash_command(
        name='abandon',
        description='Abandon this draft and void all matches (admins immediately; players by majority vote)'
    )
    async def abandon_command(self, ctx):
        """Abandon a draft from its match channel. Admin = immediate (with
        confirmation); a participant starts a majority vote."""
        await ctx.defer(ephemeral=True)

        try:
            channel_id = str(ctx.channel_id)
            draft_session = await DraftSession.get_by_channel_id(channel_id)
            if not draft_session:
                await ctx.followup.send(
                    "Run `/abandon` in the draft's match channel (where pairings are posted).",
                    ephemeral=True,
                )
                return

            if draft_session.session_stage == "completed":
                await ctx.followup.send(
                    "This draft is already completed and can't be abandoned.", ephemeral=True
                )
                return
            if draft_session.session_stage == "abandoned":
                await ctx.followup.send("This draft has already been abandoned.", ephemeral=True)
                return

            # Admin path: immediate (after a confirmation click).
            from helpers.permissions import is_bot_manager
            if await is_bot_manager(ctx):
                await ctx.followup.send(
                    "Abandon this draft? This voids **all** match results and can't be undone.",
                    view=AbandonConfirmView(draft_session.session_id, ctx.channel),
                    ephemeral=True,
                )
                return

            # Participant path: majority vote.
            sign_ups = draft_session.sign_ups or {}
            if str(ctx.author.id) not in sign_ups:
                await ctx.followup.send(
                    "Only draft participants or admins can abandon this draft.", ephemeral=True
                )
                return

            if draft_session.session_id in ACTIVE_ABANDON_VOTES:
                await ctx.followup.send(
                    "There's already an active vote to abandon this draft.", ephemeral=True
                )
                return

            participants = list(sign_ups.keys())
            view = AbandonVoteView(draft_session.session_id, participants)

            user_pings = []
            for player_id in sign_ups:
                member = ctx.guild.get_member(int(player_id))
                if member:
                    user_pings.append(member.mention)
            ping_text = " ".join(user_pings) if user_pings else "No players to ping."

            embed = await view.generate_status_embed(ctx.guild)
            message = await ctx.channel.send(
                f"⚠️ **Draft Abandonment Vote** initiated by {ctx.author.mention}\n\n{ping_text}\n\n"
                "A majority of participants must agree to abandon this draft (voids all matches).",
                embed=embed,
                view=view,
            )
            view.message = message
            ACTIVE_ABANDON_VOTES[draft_session.session_id] = view
            view.timer_task = asyncio.create_task(view.start_timer())
            await ctx.followup.send("Abandonment vote initiated.", ephemeral=True)

            try:
                await view.complete.wait()
                passed, _, _ = view.get_vote_result()
                if passed:
                    await abandon_draft_session(draft_session.session_id)
                    await ctx.channel.send(
                        "🛑 **Vote passed — draft abandoned.** All match results have been voided."
                    )
                else:
                    await ctx.channel.send("✅ **Vote to abandon the draft did not pass.**")
            finally:
                ACTIVE_ABANDON_VOTES.pop(draft_session.session_id, None)

        except Exception as e:
            logger.exception(f"Error in abandon command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

    @discord.slash_command(name='mutiny', description='Take control of the Draftmancer session from the bot')
    async def mutiny_command(self, ctx):
        """Transfer draft control to a user and disconnect the bot"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Determine if we should automatically advance to pairings
            # We do this if the draft has already started or if teams are already formed
            should_advance_to_pairings = manager.drafting or draft_session.session_stage == 'teams'
            
            if manager.drafting:
                self.logger.info(f"Mutiny during active draft for session {manager.session_id}. Will advance to pairings.")
            
            # Find a suitable user to transfer to (preferably the requester)
            target_user_id = None
            requester_name = ctx.author.display_name
            
            # Try to find the requester's username in the session
            for user in manager.session_users:
                if user.get('userName') == requester_name and user.get('userID') != 'DraftBot':
                    target_user_id = user.get('userID')
                    break
            
            # If requester not found, use any non-bot user
            if not target_user_id:
                non_bot_users = [u for u in manager.session_users if u.get('userName') != 'DraftBot']
                if non_bot_users:
                    target_user = non_bot_users[0]
                    target_user_id = target_user.get('userID')
                    requester_name = target_user.get('userName')
            
            if not target_user_id:
                await ctx.followup.send("Could not find a user to transfer control to.", ephemeral=True)
                return
                
            # Send confirmation message
            await ctx.followup.send(f"Transferring control to {requester_name} and disconnecting...", ephemeral=True)
            
            # Public message
            await ctx.channel.send(f"https://tenor.com/view/mutiny-jack-sparrow-pirates-pirates-of-the-caribbean-i-wish-to-report-a-mutiny-gif-26531174 \n\n🔄 **Mutiny!** {ctx.author.mention} is taking control of the draft. Bot disconnecting...")
            
            # Set owner as player (required before transfer)
            await manager.socket_client.emit('setOwnerIsPlayer', True)
            await asyncio.sleep(1)

            # Transfer ownership
            await manager.socket_client.emit('setSessionOwner', target_user_id)
            await asyncio.sleep(1)

            # Signal the keep_connection_alive loop to stop before disconnecting
            manager._should_disconnect = True

            # Save the session ID before cleanup
            session_id = manager.session_id

            # Disconnect and cleanup
            await manager._cleanup_and_disconnect("mutiny command")
                
            await ctx.channel.send(f"✅ Ownership transferred to {requester_name}. Bot disconnected.")

            if should_advance_to_pairings:
                # Advance to pairings stage
                success = await create_rooms_and_pairings_with_fallback(
                    ctx.bot, ctx.guild, ctx.channel, session_id, logger=self.logger
                )
                if success:
                    return  # create_rooms_pairings handles the rest (deletes original message, etc)
                # If failed, fall through to update the view so the button is enabled
            
            # Update the view to enable the button (only if NOT advancing to pairings)
            try:
                # Get the original draft message
                from session import AsyncSessionLocal
                async with AsyncSessionLocal() as db_session:
                    stmt = select(DraftSession).where(DraftSession.session_id == draft_session.session_id)
                    draft_session_db = await db_session.scalar(stmt)
                    
                    if draft_session_db and draft_session_db.draft_channel_id and draft_session_db.message_id:
                        # Get the channel and message
                        channel = ctx.bot.get_channel(int(draft_session_db.draft_channel_id))
                        if channel:
                            try:
                                message = await channel.fetch_message(int(draft_session_db.message_id))
                                
                                # Find the view for this message
                                # Create a new view with the same settings but updated button states
                                from views import PersistentView
                                updated_view = PersistentView(
                                    bot=ctx.bot,
                                    draft_session_id=draft_session_db.session_id,
                                    session_type=draft_session_db.session_type,
                                    team_a_name=draft_session_db.team_a_name,
                                    team_b_name=draft_session_db.team_b_name,
                                    session_stage=draft_session_db.session_stage
                                )
                                
                                # Apply button disabling logic - this will enable the button 
                                # since the manager is now gone
                                updated_view._apply_stage_button_disabling()
                                
                                # Update the message with the new view
                                await message.edit(view=updated_view)
                                logger.info(f"Updated view after mutiny for session {draft_session_db.session_id}")
                            except Exception as view_error:
                                logger.error(f"Error updating view after mutiny: {view_error}")
            except Exception as update_error:
                logger.error(f"Error finding message to update view: {update_error}")
                
        except Exception as e:
            logger.exception(f"Error in mutiny command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

    @discord.slash_command(name='pause', description='Pause the current draft (only for draft participants)')
    async def pause_command(self, ctx):
        """Pause the draft - only usable by participants"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Check if draft has started
            if not manager.drafting:
                await ctx.followup.send("Draft hasn't started yet.", ephemeral=True)
                return
                
            # Check if user is in sign_ups
            user_id = str(ctx.author.id)
            sign_ups = draft_session.sign_ups or {}
            
            is_participant = False
            for discord_id, display_name in sign_ups.items():
                if discord_id == user_id:
                    is_participant = True
                    break
                    
            if not is_participant:
                await ctx.followup.send("Only draft participants can pause the draft.", ephemeral=True)
                return
                
            # Pause the draft
            await manager.socket_client.emit('pauseDraft')
            manager.draftPaused = True  # Set pause state to True
            
            await ctx.followup.send(
                f"⏸️ **Draft paused** by {ctx.author.mention}.\n\n"
                f"• Use `/unpause` to resume when everyone is ready.\n"
                f"• Use `/replace_with_bots` to replace disconnected users with bots.\n"
                f"• Use `/scrap` to start a vote to cancel the draft."
            )
                
        except Exception as e:
            logger.exception(f"Error in pause command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

    @discord.slash_command(name='unpause', description='Unpause the draft and start a Discord ready check')
    async def unpause_command(self, ctx):
        """Unpause the draft after a Discord-based ready check"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Check if draft has started
            if not manager.drafting:
                await ctx.followup.send("Draft hasn't started yet.", ephemeral=True)
                return
                
            # Check if draft is actually paused
            if not manager.draftPaused:
                await ctx.followup.send("The draft is not currently paused.", ephemeral=True)
                return
                
            # Check if user is in sign_ups
            user_id = str(ctx.author.id)
            sign_ups = draft_session.sign_ups or {}
            
            is_participant = False
            for discord_id, display_name in sign_ups.items():
                if discord_id == user_id:
                    is_participant = True
                    break
                    
            if not is_participant:
                await ctx.followup.send("Only draft participants can unpause the draft.", ephemeral=True)
                return
            
            # Check if there's already an active unpause check
            if draft_session.session_id in ACTIVE_UNPAUSE_CHECKS:
                await ctx.followup.send("There's already an active unpause ready check.", ephemeral=True)
                return
                
            # Get all participants
            participants = list(sign_ups.keys())
            if not participants:
                await ctx.followup.send("No draft participants found.", ephemeral=True)
                return
            
            # Create ready check view
            view = DraftMancerReadyCheckView(draft_session.session_id, participants)
            
            # Format the pings for the message
            user_pings = []
            for player_id in sign_ups:
                try:
                    member = ctx.guild.get_member(int(player_id))
                    if member:
                        user_pings.append(member.mention)
                except:
                    pass
                    
            ping_text = " ".join(user_pings) if user_pings else "No players to ping."
            
            # Generate initial status embed
            embed = await view.generate_status_embed(ctx.guild)
            
            # Send message with pings and view
            message = await ctx.channel.send(
                f"⚠️ **Draft Unpause Ready Check** initiated by {ctx.author.mention}\n\n{ping_text}\n\n"
                f"Please click the Ready button below when you're ready to continue.",
                embed=embed,
                view=view
            )
            
            # Store message reference
            view.message = message
            
            # Store in active checks
            ACTIVE_UNPAUSE_CHECKS[draft_session.session_id] = view
            
            # Start timeout timer
            view.timer_task = asyncio.create_task(view.start_timer())
            
            # Acknowledge command
            await ctx.followup.send("Unpause ready check initiated.", ephemeral=True)
            
            # Wait for completion
            try:
                await view.complete.wait()
                
                # Check if everyone is ready
                if view.is_everyone_ready():
                    # Format pings again for the resume notification
                    user_pings = []
                    for player_id in sign_ups:
                        try:
                            member = ctx.guild.get_member(int(player_id))
                            if member:
                                user_pings.append(member.mention)
                        except:
                            pass
                            
                    ping_text = " ".join(user_pings) if user_pings else "No players to ping."
                    
                    # Everyone is ready, resume the draft with pings
                    resume_message = await ctx.channel.send(
                        f"{ping_text}\n\n🎮 **Everyone is ready!** Draft resuming in 5 seconds..."
                    )
                    await asyncio.sleep(5)
                    
                    # Emit the resumeDraft event
                    await manager.socket_client.emit('resumeDraft')
                    manager.draftPaused = False  # Reset pause state when resuming
                    
                    await resume_message.edit(content="▶️ **Draft resumed!** Good luck and have fun!")
                else:
                    # Not everyone was ready, send timeout message
                    await ctx.channel.send("⌛ The unpause ready check has expired. Please try again when everyone is ready.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_UNPAUSE_CHECKS:
                    del ACTIVE_UNPAUSE_CHECKS[draft_session.session_id]
                    
            except Exception as e:
                logger.exception(f"Error while waiting for unpause ready check completion: {e}")
                await ctx.channel.send("⚠️ An error occurred during the unpause ready check. Please try again.")
                
                # Clean up
                if draft_session.session_id in ACTIVE_UNPAUSE_CHECKS:
                    del ACTIVE_UNPAUSE_CHECKS[draft_session.session_id]
                    
        except Exception as e:
            logger.exception(f"Error in unpause command: {e}")
            await ctx.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

def setup(bot):
    bot.add_cog(DraftControlCog(bot))