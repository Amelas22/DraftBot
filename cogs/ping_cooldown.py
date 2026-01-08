from discord.ext import commands
import discord
import time
import asyncio
from loguru import logger
from sqlalchemy import Column, String, Float, Boolean, select, text
from database.models_base import Base
from session import db_session, AsyncSessionLocal
from datetime import datetime

from helpers.permissions import has_bot_manager_role


# Define the model to store role cooldown info
class RolePingCooldown(Base):
    __tablename__ = 'role_ping_cooldowns'
    
    id = Column(String(64), primary_key=True, nullable=True)  # Composite of guild_id and role_id
    role_id = Column(String(64), nullable=False)
    guild_id = Column(String(64), nullable=False)
    last_ping_time = Column(Float, default=0.0, server_default=text('0.0'))
    cooldown_period = Column(Float, default=3600.0, server_default=text('3600.0'))  # Default 1 hour in seconds
    is_managed = Column(Boolean, default=True, server_default=text('1'))  # Whether we should control mentionable permission

class PingCooldownManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldown_check_task = None
        logger.info("PingCooldownManager cog initialized")
        self.start_cooldown_checker()

    def cog_unload(self):
        if self.cooldown_check_task:
            self.cooldown_check_task.cancel()

    def start_cooldown_checker(self):
        """Start the background task that checks for expired cooldowns"""
        self.cooldown_check_task = self.bot.loop.create_task(self.check_cooldowns_task())
        
    async def set_role_mentionable(self, guild_id, role_id, mentionable):
        """Set a role's mentionable status"""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            logger.error(f"Guild {guild_id} not found")
            return False
            
        role = guild.get_role(int(role_id))
        if not role:
            logger.error(f"Role {role_id} not found in guild {guild.name}")
            return False
            
        # Check if role is already in the desired state
        if role.mentionable == mentionable:
            logger.info(f"Role {role.name} in {guild.name} is already {'mentionable' if mentionable else 'unmentionable'}")
            return True
            
        # Check if the bot has the necessary permissions
        if not guild.me.guild_permissions.manage_roles:
            logger.error(f"Bot lacks 'Manage Roles' permission in {guild.name}")
            return False
            
        # Check if the bot's role is higher than the target role
        if guild.me.top_role <= role:
            logger.error(f"Bot's highest role is not above {role.name} in hierarchy in {guild.name}")
            return False
        
        try:
            logger.info(f"Setting role {role.name} in {guild.name} to {'mentionable' if mentionable else 'unmentionable'}")
            await role.edit(mentionable=mentionable, reason="Role ping cooldown management")
            action = "enabled" if mentionable else "disabled"
            logger.info(f"Role pings {action} for {role.name} in {guild.name}")
            
            # Double-check that the change took effect
            updated_role = guild.get_role(int(role_id))
            if updated_role.mentionable != mentionable:
                logger.error(f"Failed to set role {role.name} mentionable status to {mentionable}")
                return False
                
            return True
        except discord.Forbidden:
            logger.error(f"Bot lacks permission to edit role {role.name} in {guild.name}")
            return False
        except Exception as e:
            logger.exception(f"Error setting role mentionable status: {e}")
            return False
            
    async def check_cooldowns_task(self):
        """Background task that periodically checks for expired cooldowns and makes roles mentionable again"""
        await self.bot.wait_until_ready()
        logger.info("Starting background cooldown check task")
        
        while not self.bot.is_closed():
            try:
                current_time = time.time()
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        stmt = select(RolePingCooldown).where(
                            RolePingCooldown.is_managed == True
                        )
                        result = await session.execute(stmt)
                        records = result.scalars().all()
                        
                        for record in records:
                            # Check if cooldown has expired
                            if record.last_ping_time + record.cooldown_period <= current_time:
                                # Make role mentionable again
                                success = await self.set_role_mentionable(
                                    record.guild_id, 
                                    record.role_id, 
                                    True
                                )
                                if success:
                                    logger.info(f"Cooldown expired for role {record.role_id} in guild {record.guild_id}")
                                    # Update is_managed to False since we're no longer managing it
                                    record.is_managed = False
                                    session.add(record)
                                else:
                                    logger.warning(f"Failed to make role {record.role_id} mentionable after cooldown expiry")
                    
                    await session.commit()
            except Exception as e:
                logger.exception(f"Error in cooldown check task: {e}")
                
            # Check every 15 seconds instead of every minute to be more responsive
            await asyncio.sleep(15)

    async def get_active_draft_session_count(self, channel_id):
        """Get the count of signups in the most recent draft session for a channel"""
        from session import DraftSession
        
        try:
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    # Query for the most recent draft session in this channel
                    stmt = select(DraftSession).where(
                        DraftSession.draft_channel_id == channel_id
                    ).order_by(
                        DraftSession.draft_start_time.desc()
                    )
                    result = await db_session.execute(stmt)
                    
                    # Fetch all matching sessions and use the most recent one
                    draft_sessions = result.scalars().all()
                    
                    if draft_sessions and len(draft_sessions) > 0:
                        # Use the first result (most recent due to ordering)
                        most_recent_session = draft_sessions[0]
                        if most_recent_session.sign_ups:
                            logger.info(f"Found active draft session with {len(most_recent_session.sign_ups)} signups")
                            return len(most_recent_session.sign_ups)
        except Exception as e:
            logger.exception(f"Error getting active draft session count: {e}")
            
        return 0  # Default to 0 if no session or error

    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignore bot messages
        if message.author.bot:
            return
            
        # First check if any mentioned roles are on cooldown
        async with db_session() as session:
            for role in message.role_mentions:
                record_id = f"{message.guild.id}_{role.id}"
                stmt = select(RolePingCooldown).where(
                    RolePingCooldown.id == record_id
                )
                result = await session.execute(stmt)
                cooldown_record = result.scalar_one_or_none()
                
                if not cooldown_record:
                    continue  # Role not monitored, skip
                
                # Check if the role is currently on cooldown
                now = time.time()
                if cooldown_record.is_managed and cooldown_record.last_ping_time + cooldown_record.cooldown_period > now:
                    # This role is on cooldown - DELETE THE MESSAGE to prevent notifications
                    try:
                        await message.delete()
                        # Calculate time remaining
                        cooldown_end = cooldown_record.last_ping_time + cooldown_record.cooldown_period
                        remaining = int(cooldown_end - now)
                        minutes = remaining // 60
                        seconds = remaining % 60
                        
                        # Send warning
                        warning = await message.channel.send(
                            f"{message.author.mention}, the role **{role.name}** is on cooldown and cannot be pinged. "
                            f"Please wait **{minutes}m {seconds}s** before pinging this role again.",
                            delete_after=10
                        )
                        logger.info(f"Deleted role ping from {message.author} - Role {role.name} is on cooldown")
                        return  # Exit after handling the message
                    except Exception as e:
                        logger.error(f"Error deleting cooldown violation message: {e}")
            
            # If we get here, no roles were on cooldown - process normal role ping
            for role in message.role_mentions:
                record_id = f"{message.guild.id}_{role.id}"
                stmt = select(RolePingCooldown).where(
                    RolePingCooldown.id == record_id
                )
                result = await session.execute(stmt)
                cooldown_record = result.scalar_one_or_none()
                
                if not cooldown_record:
                    continue  # Role not monitored, skip
                
                # Check queue size and adjust cooldown if needed
                signup_count = await self.get_active_draft_session_count(str(message.channel.id))
                logger.info(f"Found {signup_count} signups in active draft session")
                
                normal_cooldown = cooldown_record.cooldown_period
                
                # Apply reduced cooldown if enough players are in queue
                if signup_count >= 5:
                    adjusted_cooldown = cooldown_record.reduced_cooldown_period
                    cooldown_message = f"(Reduced cooldown: Queue has {signup_count} players)"
                else:
                    adjusted_cooldown = cooldown_record.cooldown_period
                    adjusted_cooldown_minutes = int(adjusted_cooldown / 60)
                    cooldown_message = f"(Cooldown will be reduced to 10 minutes when 5+ players in queue: Queue has {signup_count} players)"
                
                # Update the last ping time and set is_managed to True
                now = time.time()
                cooldown_record.last_ping_time = now
                cooldown_record.is_managed = True
                cooldown_record.cooldown_period = adjusted_cooldown
                session.add(cooldown_record)
                
                # Make the role not mentionable - with additional logging
                success = await self.set_role_mentionable(
                    str(message.guild.id),
                    str(role.id),
                    False
                )
                
                if not success:
                    # If we couldn't make the role unmentionable, tell the user
                    await message.channel.send(
                        f"⚠️ I couldn't make the {role.name} role unmentionable. Please check my permissions.",
                        delete_after=30
                    )
                    continue
                
                # Get formatted time when role will be mentionable again
                next_available_time = int(now + adjusted_cooldown)
                formatted_time = f"<t:{next_available_time}:R>"
                
                # Send informational message
                await message.channel.send(
                    f"**{role.name}** has been pinged by {message.author.mention}. " +
                    f"This role will be available to ping again {formatted_time}. {cooldown_message}"
                )
                
                logger.info(f"Role {role.name} pinged by {message.author} in {message.guild.name} - Cooldown: {adjusted_cooldown/60} minutes")

    @discord.slash_command(
        name='setup_ping_cooldown', 
        description='Set a cooldown period for pinging a specific role (reduced when 5+ players in queue)'
    )
    @has_bot_manager_role()
    async def setup_ping_cooldown(self, ctx, role: discord.Role, cooldown_minutes: int = 60, reduced_cooldown_minutes: int = 10):
        await ctx.defer(ephemeral=True)
        
        if cooldown_minutes <= 0 or reduced_cooldown_minutes <= 0:
            await ctx.followup.send("Cooldown times must be greater than 0 minutes.", ephemeral=True)
            return
            
        # Check if the bot has manage roles permission
        if not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.followup.send(
                "I don't have the 'Manage Roles' permission, which is required for role ping cooldowns.",
                ephemeral=True
            )
            return
            
        # Check if the bot's role is higher than the target role
        if ctx.guild.me.top_role <= role:
            await ctx.followup.send(
                "I can't manage this role because it's higher than or equal to my highest role. " +
                "Please move my role above this one in the server settings.",
                ephemeral=True
            )
            return
            
        record_id = f"{ctx.guild.id}_{role.id}"
        
        async with db_session() as session:
            # Check if record already exists
            stmt = select(RolePingCooldown).where(
                RolePingCooldown.id == record_id
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            
            if record:
                # Update existing record
                record.cooldown_period = cooldown_minutes * 60
                record.is_managed = False  # Reset management state
                session.add(record)
                logger.info(f"Updated ping cooldown for role {role.name} in guild {ctx.guild.name}")
            else:
                # Create new record
                new_record = RolePingCooldown(
                    id=record_id,
                    role_id=str(role.id),
                    guild_id=str(ctx.guild.id),
                    last_ping_time=0.0,
                    cooldown_period=cooldown_minutes * 60,
                    is_managed=False
                )
                session.add(new_record)
                logger.info(f"Created new ping cooldown for role {role.name} in guild {ctx.guild.name}")
            
            # Make sure the role is mentionable now
            await self.set_role_mentionable(str(ctx.guild.id), str(role.id), True)
            
        await ctx.followup.send(
            f"Role {role.mention} now has a ping cooldown of {cooldown_minutes} minutes. " +
            f"This will be reduced to {reduced_cooldown_minutes} minutes when 5+ players are in a queue. " +
            f"The role will become unmentionable after being pinged until the cooldown expires.",
            ephemeral=True
        )
        
    @discord.slash_command(
        name='remove_ping_cooldown',
        description='Remove the cooldown period for pinging a specific role'
    )
    @has_bot_manager_role()
    async def remove_ping_cooldown(self, ctx, role: discord.Role):
        await ctx.defer(ephemeral=True)
        
        record_id = f"{ctx.guild.id}_{role.id}"
        
        async with db_session() as session:
            stmt = select(RolePingCooldown).where(
                RolePingCooldown.id == record_id
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            
            if record:
                await session.delete(record)
                logger.info(f"Removed ping cooldown for role {role.name} in guild {ctx.guild.name}")
                
                # Make sure the role is mentionable again
                await self.set_role_mentionable(str(ctx.guild.id), str(role.id), True)
                
                await ctx.followup.send(
                    f"Removed ping cooldown for role {role.mention}.",
                    ephemeral=True
                )
            else:
                await ctx.followup.send(
                    f"Role {role.mention} does not have a ping cooldown set.",
                    ephemeral=True
                )
                
    @discord.slash_command(
        name='list_ping_cooldowns',
        description='List all roles with ping cooldowns in this server'
    )
    @has_bot_manager_role()
    async def list_ping_cooldowns(self, ctx):
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            stmt = select(RolePingCooldown).where(
                RolePingCooldown.guild_id == str(ctx.guild.id)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            
            if not records:
                await ctx.followup.send("No roles have ping cooldowns set in this server.", ephemeral=True)
                return
                
            embed = discord.Embed(
                title="Role Ping Cooldowns",
                description="The following roles have ping cooldowns set:",
                color=discord.Color.blue()
            )
            
            current_time = time.time()
            
            for record in records:
                role = ctx.guild.get_role(int(record.role_id))
                if not role:
                    continue
                    
                cooldown_minutes = int(record.cooldown_period / 60)
                
                # Check if role is currently on cooldown
                if record.is_managed:
                    next_available = record.last_ping_time + record.cooldown_period
                    if next_available > current_time:
                        time_remaining = int(next_available - current_time)
                        minutes = time_remaining // 60
                        seconds = time_remaining % 60
                        status = f"On cooldown - available again in {minutes}m {seconds}s"
                    else:
                        status = "Available now"
                else:
                    status = "Available now"
                    
                embed.add_field(
                    name=role.name,
                    value=f"Cooldown: {cooldown_minutes} minutes\nStatus: {status}",
                    inline=False
                )
            
            await ctx.followup.send(embed=embed, ephemeral=True)

    @discord.slash_command(
        name='reset_role_cooldown',
        description='Reset a role cooldown and make it mentionable again'
    )
    @has_bot_manager_role()
    async def reset_role_cooldown(self, ctx, role: discord.Role):
        await ctx.defer(ephemeral=True)
        
        record_id = f"{ctx.guild.id}_{role.id}"
        
        async with db_session() as session:
            stmt = select(RolePingCooldown).where(
                RolePingCooldown.id == record_id
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            
            if not record:
                await ctx.followup.send(
                    f"Role {role.mention} does not have a ping cooldown set.",
                    ephemeral=True
                )
                return
                
            # Reset the cooldown state
            record.is_managed = False
            record.last_ping_time = 0
            session.add(record)
            
            # Make the role mentionable again
            await self.set_role_mentionable(str(ctx.guild.id), str(role.id), True)
            
            await ctx.followup.send(
                f"Reset cooldown for role {role.mention}. The role is now mentionable.",
                ephemeral=True
            )

def setup(bot):
    bot.add_cog(PingCooldownManager(bot))