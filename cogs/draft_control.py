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

# Store active unpause ready checks
ACTIVE_UNPAUSE_CHECKS = {}

class DraftMancerReadyCheckView(View):
    def __init__(self, draft_session_id, participants, timeout=90.0):
        super().__init__(timeout=timeout)
        self.draft_session_id = draft_session_id
        self.participants = {user_id: False for user_id in participants}  # False = not ready
        self.message = None
        self.timer_task = None
        self.complete = asyncio.Event()
        self._start_time = asyncio.get_event_loop().time()
        
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
        
        remaining = int(self.timeout - (asyncio.get_event_loop().time() - self._start_time))
        if remaining < 0:
            remaining = 0
            
        embed.set_footer(text=f"Ready check expires in {remaining} seconds")
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
            await ctx.channel.send(f"🔄 **Mutiny!** {ctx.author.mention} is taking control of the draft. Bot disconnecting...")
            
            # Set owner as player (required before transfer)
            await manager.sio.emit('setOwnerIsPlayer', True)
            await asyncio.sleep(1)
            
            # Transfer ownership
            await manager.sio.emit('setSessionOwner', target_user_id)
            await asyncio.sleep(1)
            
            # Disconnect
            await manager.sio.disconnect()
            
            # Remove from active managers
            if manager.session_id in ACTIVE_MANAGERS:
                del ACTIVE_MANAGERS[manager.session_id]
                
            await ctx.channel.send(f"✅ Ownership transferred to {requester_name}. Bot disconnected.")
                
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
            
            await ctx.followup.send("Draft paused successfully.", ephemeral=True)
            await ctx.channel.send(f"⏸️ **Draft paused** by {ctx.author.mention}. Use `/unpause` to resume when everyone is ready.")
                
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