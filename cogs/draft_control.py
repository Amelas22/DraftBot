import discord
from discord.ext import commands
import asyncio
from loguru import logger
from models.draft_session import DraftSession
from services.draft_setup_manager import DraftSetupManager, ACTIVE_MANAGERS
from discord.ui import View, Button
from database.db_session import db_session
from models.draft_session import DraftSession
from services.draft_setup_manager import DraftSetupManager, ACTIVE_MANAGERS
from datetime import datetime
from sqlalchemy import select

# Store active unpause ready checks
ACTIVE_UNPAUSE_CHECKS = {}
ACTIVE_SCRAP_VOTES = {}
ACTIVE_LOG_RELEASE_VOTES = {}


class LogReleaseVoteView(View):
    def __init__(self, draft_session_id, participants, timeout=90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        # Initialize with all participants set to None (haven't voted)
        self.votes = {user_id: None for user_id in participants}  # None = not voted, True = yes, False = no
        self.message = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()
        
    async def start_timer(self):
        """Start the timeout timer for the vote"""
        try:
            await asyncio.sleep(self.timeout)
            if not self.complete.is_set():
                # Time's up, mark the vote as complete
                logger.info(f"Log release vote for session {self.draft_session_id} timed out")
                await self.on_timeout()
        except asyncio.CancelledError:
            logger.debug(f"Timer for log release vote {self.draft_session_id} was cancelled")
    
    def get_vote_result(self):
        """Check if the vote passed (more than half voted yes)"""
        yes_votes = sum(1 for vote in self.votes.values() if vote is True)
        total_participants = len(self.votes)
        
        # Vote passes if more than half vote yes
        needed_votes = (total_participants // 2) + 1
        return yes_votes >= needed_votes, yes_votes, total_participants
    
    @discord.ui.button(label="Yes, Release Logs", style=discord.ButtonStyle.primary)
    async def yes_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote yes to release logs"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = True
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Check if vote has passed
        passed, _, _ = self.get_vote_result()
        if passed:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
            # The on_complete callback will handle releasing the logs
        
    @discord.ui.button(label="No, Keep Logs Private", style=discord.ButtonStyle.secondary)
    async def no_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote no to keep logs private"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = False
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        # Check if vote cannot pass anymore
        no_votes = sum(1 for vote in self.votes.values() if vote is False)
        total_participants = len(self.votes)
        needed_votes = (total_participants // 2) + 1
        remaining_votes = sum(1 for vote in self.votes.values() if vote is None)
        max_possible_yes = sum(1 for vote in self.votes.values() if vote is True) + remaining_votes

        # If max possible yes votes can't reach threshold, end vote early
        if max_possible_yes < needed_votes:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
    
    async def generate_status_embed(self, guild):
        """Generate an embed showing the current vote status"""
        embed = discord.Embed(
            title="Draft Logs Release Vote",
            description="Vote to release the draft logs early.",
            color=discord.Color.blue()
        )
        
        # Add a field showing votes for each participant
        status_lines = []
        for user_id, vote in self.votes.items():
            # Try to get member name
            member = guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            
            # Add status emoji based on vote
            if vote is True:
                status = "✅ Voted to Release"
            elif vote is False:
                status = "❌ Voted to Keep Private"
            else:
                status = "⏳ Not Voted"
                
            status_lines.append(f"{name}: {status}")
        
        embed.add_field(
            name="Participants",
            value="\n".join(status_lines) or "No participants found",
            inline=False
        )
        
        # Add vote results field
        passed, yes_votes, total_participants = self.get_vote_result()
        needed_votes = (total_participants // 2) + 1
        
        embed.add_field(
            name="Vote Status",
            value=f"**{yes_votes}/{needed_votes}** votes needed to release logs\n" +
                  (f"**Vote will pass!**" if passed else f"**Vote will not pass yet**"),
            inline=False
        )
        
        # Calculate expiry timestamp correctly
        expiry_time = self._start_time.timestamp() + self.timeout
        expiry_timestamp = int(expiry_time)
        
        embed.add_field(
            name="Vote Ends",
            value=f"<t:{expiry_timestamp}:R>",
            inline=False
        )
        
        return embed
        
    async def on_timeout(self):
        """Called when the view times out"""
        # Make sure we set the complete event
        self.complete.set()
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
            
        # Update the message if it exists
        if self.message:
            embed = discord.Embed(
                title="Draft Logs Release Vote - Ended",
                description="The vote has ended.",
                color=discord.Color.blue()
            )
            
            passed, yes_votes, total_participants = self.get_vote_result()
            needed_votes = (total_participants // 2) + 1
            
            embed.add_field(
                name="Final Results",
                value=f"**{yes_votes}/{needed_votes}** votes needed to release logs\n" +
                      (f"**Vote passed!**" if passed else f"**Vote did not pass**"),
                inline=False
            )
            
            await self.message.edit(embed=embed, view=self)

ACTIVE_REPLACE_VOTES = {}

class ReplaceWithBotsVoteView(View):
    def __init__(self, draft_session_id, participants, timeout=90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        # Initialize with all participants set to None (haven't voted)
        self.votes = {user_id: None for user_id in participants}  # None = not voted, True = yes, False = no
        self.message = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()
        
    async def start_timer(self):
        """Start the timeout timer for the vote"""
        try:
            await asyncio.sleep(self.timeout)
            if not self.complete.is_set():
                # Time's up, mark the vote as complete
                logger.info(f"Replace with bots vote for session {self.draft_session_id} timed out")
                await self.on_timeout()
        except asyncio.CancelledError:
            logger.debug(f"Timer for replace with bots vote {self.draft_session_id} was cancelled")
    
    def get_vote_result(self):
        """Check if the vote passed (more than half voted yes)"""
        yes_votes = sum(1 for vote in self.votes.values() if vote is True)
        total_participants = len(self.votes)
        
        # Vote passes if more than half vote yes
        needed_votes = (total_participants // 2) + 1
        return yes_votes >= needed_votes, yes_votes, total_participants
    
    @discord.ui.button(label="Yes, Replace with Bots", style=discord.ButtonStyle.primary)
    async def yes_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote yes to replace disconnected users with bots"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = True
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Check if vote has passed
        passed, _, _ = self.get_vote_result()
        if passed:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
            # The on_complete callback will handle replacing disconnected users
        
    @discord.ui.button(label="No, Wait for Players", style=discord.ButtonStyle.secondary)
    async def no_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote no to continue waiting for disconnected users"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = False
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        # Check if vote cannot pass anymore
        no_votes = sum(1 for vote in self.votes.values() if vote is False)
        total_participants = len(self.votes)
        needed_votes = (total_participants // 2) + 1
        remaining_votes = sum(1 for vote in self.votes.values() if vote is None)
        max_possible_yes = sum(1 for vote in self.votes.values() if vote is True) + remaining_votes

        # If max possible yes votes can't reach threshold, end vote early
        if max_possible_yes < needed_votes:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
    
    async def generate_status_embed(self, guild):
        """Generate an embed showing the current vote status"""
        embed = discord.Embed(
            title="Replace Disconnected Players Vote",
            description="Vote to replace disconnected players with bots.",
            color=discord.Color.blue()
        )
        
        # Add a field showing votes for each participant
        status_lines = []
        for user_id, vote in self.votes.items():
            # Try to get member name
            member = guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            
            # Add status emoji based on vote
            if vote is True:
                status = "✅ Replace with Bots"
            elif vote is False:
                status = "❌ Wait for Players"
            else:
                status = "⏳ Not Voted"
                
            status_lines.append(f"{name}: {status}")
        
        embed.add_field(
            name="Participants",
            value="\n".join(status_lines) or "No participants found",
            inline=False
        )
        
        # Add vote results field with fixed needed votes calculation
        passed, yes_votes, total_participants = self.get_vote_result()
        needed_votes = (total_participants // 2) + 1
        
        embed.add_field(
            name="Vote Status",
            value=f"**{yes_votes}/{needed_votes}** votes needed to replace\n" +
                  (f"**Vote will pass!**" if passed else f"**Vote will not pass yet**"),
            inline=False
        )
        
        # Calculate expiry timestamp correctly
        expiry_time = self._start_time.timestamp() + self.timeout
        expiry_timestamp = int(expiry_time)
        
        embed.add_field(
            name="Vote Ends",
            value=f"<t:{expiry_timestamp}:R>",
            inline=False
        )
        
        return embed
        
    async def on_timeout(self):
        """Called when the view times out"""
        # Make sure we set the complete event
        self.complete.set()
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
            
        # Update the message if it exists
        if self.message:
            embed = discord.Embed(
                title="Replace Disconnected Players Vote - Ended",
                description="The vote has ended.",
                color=discord.Color.blue()
            )
            
            passed, yes_votes, total_participants = self.get_vote_result()
            needed_votes = (total_participants // 2) + 1
            
            embed.add_field(
                name="Final Results",
                value=f"**{yes_votes}/{needed_votes}** votes needed to replace\n" +
                      (f"**Vote passed!**" if passed else f"**Vote did not pass**"),
                inline=False
            )
            
            await self.message.edit(embed=embed, view=self)

class DraftMancerReadyCheckView(View):
    def __init__(self, draft_session_id, participants, timeout=90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        self.participants = {user_id: False for user_id in participants}  # False = not ready
        self.message = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()
        
    async def start_timer(self):
        """Start the timeout timer for the ready check"""
        try:
            await asyncio.sleep(self.timeout)
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
        user_id = str(interaction.user.id)
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
                child.disabled = True
                
            await self.message.edit(view=self)
            # The on_complete callback will handle the actual resuming
        
    @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red)
    async def not_ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Mark user as not ready"""
        user_id = str(interaction.user.id)
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
        
        expiry_time = self._start_time.timestamp() + self.timeout
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
            child.disabled = True
            
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

class ScrapVoteView(View):
    def __init__(self, draft_session_id, participants, timeout=90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        # Initialize with all participants set to None (haven't voted)
        self.votes = {user_id: None for user_id in participants}  # None = not voted, True = yes, False = no
        self.message = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = datetime.now()  # Use time.time() instead of asyncio.get_event_loop().time()
        
    async def start_timer(self):
        """Start the timeout timer for the vote"""
        try:
            await asyncio.sleep(self.timeout)
            if not self.complete.is_set():
                # Time's up, mark the vote as complete
                logger.info(f"Scrap vote for session {self.draft_session_id} timed out")
                await self.on_timeout()
        except asyncio.CancelledError:
            logger.debug(f"Timer for scrap vote {self.draft_session_id} was cancelled")
    
    def get_vote_result(self):
        """Check if the vote passed (more than half voted yes)"""
        yes_votes = sum(1 for vote in self.votes.values() if vote is True)
        total_participants = len(self.votes)
        
        # Vote passes if more than half vote yes
        needed_votes = (total_participants // 2) + 1
        return yes_votes >= needed_votes, yes_votes, total_participants
    
    @discord.ui.button(label="Yes, Cancel Draft", style=discord.ButtonStyle.danger)
    async def yes_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote yes to cancel the draft"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = True
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Check if vote has passed
        passed, _, _ = self.get_vote_result()
        if passed:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
            # The on_complete callback will handle canceling the draft
        
    @discord.ui.button(label="No, Continue Draft", style=discord.ButtonStyle.green)
    async def no_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Vote no to continue the draft"""
        user_id = str(interaction.user.id)
        if user_id not in self.votes:
            await interaction.response.send_message("You are not part of this draft.", ephemeral=True)
            return
            
        # Record vote
        self.votes[user_id] = False
        
        # Update the message
        embed = await self.generate_status_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

        # Check if vote cannot pass anymore
        no_votes = sum(1 for vote in self.votes.values() if vote is False)
        total_participants = len(self.votes)
        needed_votes = (total_participants // 2) + 1
        remaining_votes = sum(1 for vote in self.votes.values() if vote is None)
        max_possible_yes = sum(1 for vote in self.votes.values() if vote is True) + remaining_votes

        # If max possible yes votes can't reach threshold, end vote early
        if max_possible_yes < needed_votes:
            self.complete.set()
            if self.timer_task:
                self.timer_task.cancel()
            
            # Disable buttons
            for child in self.children:
                child.disabled = True
                
            await self.message.edit(view=self)
    
    async def generate_status_embed(self, guild):
        """Generate an embed showing the current vote status"""
        embed = discord.Embed(
            title="Draft Cancellation Vote",
            description="Vote to cancel the current draft.",
            color=discord.Color.red()
        )
        
        # Add a field showing votes for each participant
        status_lines = []
        for user_id, vote in self.votes.items():
            # Try to get member name
            member = guild.get_member(int(user_id))
            name = member.display_name if member else f"User {user_id}"
            
            # Add status emoji based on vote
            if vote is True:
                status = "✅ Voted to Cancel"
            elif vote is False:
                status = "❌ Voted to Continue"
            else:
                status = "⏳ Not Voted"
                
            status_lines.append(f"{name}: {status}")
        
        embed.add_field(
            name="Participants",
            value="\n".join(status_lines) or "No participants found",
            inline=False
        )
        
        # Add vote results field with fixed needed votes calculation
        passed, yes_votes, total_participants = self.get_vote_result()
        needed_votes = (total_participants // 2) + 1  # Fix: more than half means (n/2)+1
        
        embed.add_field(
            name="Vote Status",
            value=f"**{yes_votes}/{needed_votes}** votes needed to cancel\n" +
                  (f"**Vote will pass!**" if passed else f"**Vote will not pass yet**"),
            inline=False
        )
        
        # Calculate expiry timestamp correctly
        expiry_time = self._start_time.timestamp() + self.timeout
        expiry_timestamp = int(expiry_time)
        
        embed.add_field(
            name="Vote Ends",
            value=f"<t:{expiry_timestamp}:R>",
            inline=False
        )
        
        return embed
        
    async def on_timeout(self):
        """Called when the view times out"""
        # Make sure we set the complete event
        self.complete.set()
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
            
        # Update the message if it exists
        if self.message:
            embed = discord.Embed(
                title="Draft Cancellation Vote - Ended",
                description="The vote has ended.",
                color=discord.Color.red()
            )
            
            passed, yes_votes, total_participants = self.get_vote_result()
            needed_votes = (total_participants // 2) + 1
            
            embed.add_field(
                name="Final Results",
                value=f"**{yes_votes}/{needed_votes}** votes needed to cancel\n" +
                      (f"**Vote passed!**" if passed else f"**Vote did not pass**"),
                inline=False
            )
            
            await self.message.edit(embed=embed, view=self)

class DraftControlCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
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
            from sqlalchemy import select, and_, desc
            
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
                    await manager.sio.emit('replaceDisconnectedPlayers')
                    
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
                from sqlalchemy import select
                
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
                    await manager.sio.emit('stopDraft')
                    
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
            
    @discord.slash_command(name='mutiny', description='Take control of the Draftmancer session from the bot')
    async def mutiny_command(self, ctx):
        """Transfer draft control to a user and disconnect the bot"""
        await ctx.defer(ephemeral=True)
        
        try:
            result = await self._get_manager_for_channel(ctx)
            if not result:
                return
                
            manager, draft_session = result
            
            # Check if draft has already started
            if manager.drafting:
                await ctx.followup.send(
                    "Cannot take control after draft has started. Use `/pause` instead if needed.", 
                    ephemeral=True
                )
                return
                
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
            await manager.sio.emit('setOwnerIsPlayer', True)
            await asyncio.sleep(1)
            
            # Transfer ownership
            await manager.sio.emit('setSessionOwner', target_user_id)
            await asyncio.sleep(1)
            
            # Disconnect
            await manager.sio.disconnect()
            
            # Save the session ID before removing the manager
            session_id = manager.session_id
            
            # Remove from active managers
            if manager.session_id in ACTIVE_MANAGERS:
                del ACTIVE_MANAGERS[manager.session_id]
                
            await ctx.channel.send(f"✅ Ownership transferred to {requester_name}. Bot disconnected.")
            
            # Update the view to enable the button
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
            await manager.sio.emit('pauseDraft')
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
                        f"\{ping_text}n\n🎮 **Everyone is ready!** Draft resuming in 5 seconds..."
                    )
                    await asyncio.sleep(5)
                    
                    # Emit the resumeDraft event
                    await manager.sio.emit('resumeDraft')
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