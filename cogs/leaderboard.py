import discord
from discord.ext import commands
from discord.ui import View, Button
from datetime import datetime
from sqlalchemy import select, inspect
from database.db_session import db_session
from models.leaderboard_message import LeaderboardMessage
from services.leaderboard_service import get_timeframe_date
from services.leaderboard_formatter import create_leaderboard_embed
from loguru import logger
from leaderboard_config import ALL_CATEGORIES as LEADERBOARD_CATEGORIES

class TimeframeView(View):
    """Interactive view for selecting leaderboard timeframes"""
    def __init__(self, bot, guild_id, category, current_timeframe="lifetime"):
        super().__init__(timeout=None)  # No timeout - buttons work indefinitely
        self.bot = bot
        self.guild_id = guild_id
        self.category = category
        self.current_timeframe = current_timeframe
        
        # Add timeframe buttons
        if category in ["longest_win_streak", "perfect_streak"]:
            # Win streak category gets Active button + standard timeframes
            timeframes = [
                ("active", "Active"),
                ("30d", "30 Days"),
                ("90d", "90 Days"),
                ("lifetime", "Lifetime")
            ]
        else:
            # Standard timeframes for other categories
            timeframes = [
                ("14d", "14 Days"),
                ("30d", "30 Days"),
                ("90d", "90 Days"),
                ("lifetime", "Lifetime")
            ]

        for value, label in timeframes:
            # Make the current timeframe button appear selected
            button = Button(
                label=label,
                style=discord.ButtonStyle.primary if value == current_timeframe else discord.ButtonStyle.secondary,
                custom_id=f"timeframe_{value}_{category}"
            )
            button.callback = self.timeframe_callback
            self.add_item(button)
    
    async def timeframe_callback(self, interaction):
        # Extract timeframe from the button's custom_id
        custom_id = interaction.data["custom_id"]
        timeframe = custom_id.split("_")[1]
        category = self.category
        
        # Update the leaderboard with the new timeframe
        embed = await create_leaderboard_embed(self.guild_id, category, timeframe=timeframe)
        
        # Create a new view with the updated timeframe
        view = TimeframeView(self.bot, self.guild_id, category, current_timeframe=timeframe)
        
        # Update the message with the new embed and view
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the database to reflect the new timeframe
        async with db_session() as session:
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == self.guild_id)
            result = await session.execute(stmt)
            leaderboard_message = result.scalar_one_or_none()
            
            if leaderboard_message:
                # Update the timeframe for this category
                setattr(leaderboard_message, f"{category}_timeframe", timeframe)
                await session.commit()
                logger.info(f"Updated {category} timeframe to {timeframe} for guild {self.guild_id}")

class LeaderboardCog(commands.Cog):
    """Cog for managing leaderboard commands and functionality"""
    def __init__(self, bot):
        self.bot = bot
        logger.info("Leaderboard commands registered")
        
        # Register the button handler
        bot.add_listener(self.on_interaction, "on_interaction")
    
    async def on_interaction(self, interaction):
        """Handle button interactions"""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            if custom_id.startswith("timeframe_"):
                # This will be handled by the View callback
                pass
    
    @discord.slash_command(name="leaderboard", description="Show all player leaderboards for drafts")
    async def leaderboard(self, ctx):
        """Display all leaderboards of player statistics"""
        await ctx.defer()
        
        # Get the guild ID
        guild_id = str(ctx.guild.id)
        
        logger.info(f"Generating all leaderboards for guild {guild_id}")
        
        try:
            # Get or create leaderboard record, timeframes, and channel
            leaderboard_record, timeframes, channel = await self._get_leaderboard_setup(ctx, guild_id)
            if not channel:
                # If we couldn't get the channel, there's a deeper issue
                await ctx.respond("Error: Couldn't access any channels to post leaderboards.")
                return
            
            # Process each category
            for category in LEADERBOARD_CATEGORIES:
                await self._update_category_leaderboard(
                    category=category,
                    guild_id=guild_id,
                    channel=channel,
                    leaderboard_record=leaderboard_record,
                    timeframe=timeframes.get(category, 'lifetime')
                )
            
            # Update last_updated timestamp
            async with db_session() as session:
                leaderboard_record = await session.merge(leaderboard_record)
                leaderboard_record.last_updated = datetime.now()
                await session.commit()
            
            # Complete the interaction
            await ctx.respond("✅")
            
        except Exception as e:
            logger.error(f"Error processing leaderboards: {e}")
            await ctx.respond(f"Error creating leaderboards: {str(e)}")

    async def _get_leaderboard_setup(self, ctx, guild_id):
        """Get or create leaderboard record, timeframes, and channel for posting"""
        # Get the existing leaderboard message record
        async with db_session() as session:
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
            result = await session.execute(stmt)
            leaderboard_record = result.scalar_one_or_none()
            
            # Check if LeaderboardMessage has the column attributes we need
            if leaderboard_record:
                # Ensure the LeaderboardMessage object has our required columns
                missing_columns = await self._check_missing_columns(session, leaderboard_record)
                if missing_columns:
                    logger.warning(f"Some columns are missing from leaderboard_messages table: {missing_columns}")
            
            # Get timeframes for each category from database or defaults
            timeframes = {}
            for category in LEADERBOARD_CATEGORIES:
                if category == "hot_streak":
                    timeframes[category] = "7d"  # Hot streak is always 7 days
                else:
                    # Get stored timeframe or default to "lifetime"
                    if leaderboard_record and hasattr(leaderboard_record, f"{category}_timeframe"):
                        timeframes[category] = getattr(leaderboard_record, f"{category}_timeframe") or "lifetime"
                    else:
                        timeframes[category] = "lifetime"
        
        # Create a new record if needed
        if not leaderboard_record:
            leaderboard_record = await self._create_leaderboard_record(guild_id, ctx.channel.id)
        
        # Get the appropriate channel
        channel = await self._get_leaderboard_channel(ctx, leaderboard_record)
        
        # Update the channel ID if we're using the current channel
        if channel and channel.id != int(leaderboard_record.channel_id):
            await self._update_leaderboard_channel(leaderboard_record, channel.id)
            
        return leaderboard_record, timeframes, channel
    
    async def _create_leaderboard_record(self, guild_id, channel_id):
        """Create a new leaderboard record"""
        async with db_session() as session:
            leaderboard_record = LeaderboardMessage(
                guild_id=guild_id,
                channel_id=str(channel_id),
                message_id="placeholder",  # Will update this later
                last_updated=datetime.now()
            )
            session.add(leaderboard_record)
            await session.commit()
            
            # Refresh to get the ID
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
    
    async def _get_leaderboard_channel(self, ctx, leaderboard_record):
        """Get the channel to post leaderboards in"""
        # Try to get the original channel
        original_channel = None
        try:
            original_channel = ctx.guild.get_channel(int(leaderboard_record.channel_id))
            if original_channel:
                logger.info(f"Found original leaderboard channel: {original_channel.name} (ID: {original_channel.id})")
                
                # Check permissions in the original channel
                bot_member = original_channel.guild.get_member(self.bot.user.id)
                permissions = original_channel.permissions_for(bot_member)
                if not permissions.send_messages or not permissions.embed_links or not permissions.read_message_history:
                    logger.warning(f"Missing permissions in channel {original_channel.name}: send_messages={permissions.send_messages}, embed_links={permissions.embed_links}, read_message_history={permissions.read_message_history}")
                    original_channel = None  # Reset if we don't have permissions
                    
        except Exception as e:
            logger.error(f"Error getting original channel: {e}")
            original_channel = None

        # If we can't access the original channel, use the current one
        if not original_channel:
            logger.info(f"Using current channel for leaderboard: {ctx.channel.name} (ID: {ctx.channel.id})")
            return ctx.channel
        else:
            return original_channel
    
    async def _update_leaderboard_channel(self, leaderboard_record, new_channel_id):
        """Update the channel ID in the leaderboard record"""
        async with db_session() as session:
            leaderboard_record = await session.merge(leaderboard_record)
            leaderboard_record.channel_id = str(new_channel_id)

            # Reset all message IDs since we're changing channels
            leaderboard_record.message_id = "placeholder"  # Can't be None - column is NOT NULL
            leaderboard_record.draft_record_view_message_id = None
            leaderboard_record.match_win_view_message_id = None
            leaderboard_record.drafts_played_view_message_id = None
            leaderboard_record.time_vault_and_key_view_message_id = None
            await session.commit()
    
    async def _update_category_leaderboard(self, category, guild_id, channel, leaderboard_record, timeframe):
        """Update or create a leaderboard for a specific category"""
        logger.info(f"Processing {category} leaderboard")
        
        # Create the embed
        embed = await create_leaderboard_embed(guild_id, category, timeframe=timeframe)
        
        # Create view for categories except hot_streak
        view = None
        if category != "hot_streak":
            view = TimeframeView(self.bot, guild_id, category, current_timeframe=timeframe)
        
        # Get the message ID field name
        msg_id_field = f"{category}_view_message_id" if category != "hot_streak" else "message_id"
        
        # Try to update existing message
        message_updated = False
        if hasattr(leaderboard_record, msg_id_field) and getattr(leaderboard_record, msg_id_field):
            try:
                message_id = getattr(leaderboard_record, msg_id_field)
                existing_msg = await channel.fetch_message(int(message_id))
                if category != "hot_streak":
                    await existing_msg.edit(embed=embed, view=view)
                else:
                    await existing_msg.edit(embed=embed)
                message_updated = True
                logger.info(f"Updated existing {category} message {message_id}")
            except discord.NotFound:
                logger.warning(f"Message {message_id} for {category} not found, will create new one")
            except Exception as e:
                logger.error(f"Error updating {category} message: {e}")
        
        # Send new message if needed
        if not message_updated:
            try:
                if category != "hot_streak":
                    new_msg = await channel.send(embed=embed, view=view)
                    setattr(leaderboard_record, f"{category}_view_message_id", str(new_msg.id))
                else:
                    new_msg = await channel.send(embed=embed)
                    leaderboard_record.message_id = str(new_msg.id)

                async with db_session() as session:
                    leaderboard_record = await session.merge(leaderboard_record)
                    await session.commit()
                logger.info(f"Created new {category} message")
            except Exception as e:
                logger.error(f"Error creating new {category} message: {e}")
    
    async def _check_missing_columns(self, session, leaderboard_message):
        """Check if any required columns are missing from the leaderboard_messages table"""
        missing_columns = []
        
        # Required columns for the timeframe functionality
        required_columns = []
        for category in LEADERBOARD_CATEGORIES:
            if category != "hot_streak":  # hot_streak doesn't have these fields
                required_columns.extend([
                    f"{category}_view_message_id",
                    f"{category}_timeframe"
                ])
        
        # Check for each required column
        for column in required_columns:
            if not hasattr(leaderboard_message, column):
                missing_columns.append(column)
        
        return missing_columns

async def refresh_all_leaderboards(bot):
    """Refresh all leaderboards for all guilds on bot restart"""
    logger.info("Starting to refresh all leaderboards...")
    
    try:
        # Get all leaderboard records
        async with db_session() as session:
            stmt = select(LeaderboardMessage)
            result = await session.execute(stmt)
            all_leaderboards = result.scalars().all()
            
            if not all_leaderboards:
                logger.info("No leaderboards found to refresh")
                return
                
            logger.info(f"Found {len(all_leaderboards)} leaderboards to refresh")
            
            # Process each leaderboard
            for leaderboard in all_leaderboards:
                try:
                    guild_id = leaderboard.guild_id
                    guild = bot.get_guild(int(guild_id))
                    
                    if not guild:
                        logger.warning(f"Could not find guild with ID {guild_id}")
                        continue
                        
                    # Get the channel
                    channel = guild.get_channel(int(leaderboard.channel_id))
                    if not channel:
                        logger.warning(f"Could not find channel with ID {leaderboard.channel_id} in guild {guild.name}")
                        continue
                    
                    # Check permissions
                    bot_member = guild.get_member(bot.user.id)
                    permissions = channel.permissions_for(bot_member)
                    if not (permissions.send_messages and permissions.embed_links and permissions.read_message_history):
                        logger.warning(f"Missing permissions in channel {channel.name} (ID: {channel.id}) in guild {guild.name}")
                        continue
                    
                    # Get timeframes for each category
                    timeframes = {}
                    for category in LEADERBOARD_CATEGORIES:
                        if category == "hot_streak":
                            timeframes[category] = "7d"  # Hot streak is always 7 days
                        else:
                            # Get stored timeframe or default to "lifetime"
                            timeframe_field = f"{category}_timeframe"
                            if hasattr(leaderboard, timeframe_field):
                                timeframes[category] = getattr(leaderboard, timeframe_field) or "lifetime"
                            else:
                                timeframes[category] = "lifetime"
                    
                    # Process each category
                    for category in LEADERBOARD_CATEGORIES:
                        try:
                            # Create the embed
                            embed = await create_leaderboard_embed(guild_id, category, timeframe=timeframes[category])
                            
                            # Get the message ID field name
                            msg_id_field = f"{category}_view_message_id" if category != "hot_streak" else "message_id"
                            
                            # Try to update existing message
                            message_updated = False
                            if hasattr(leaderboard, msg_id_field) and getattr(leaderboard, msg_id_field):
                                try:
                                    message_id = getattr(leaderboard, msg_id_field)
                                    existing_msg = await channel.fetch_message(int(message_id))
                                    
                                    if category != "hot_streak":
                                        view = TimeframeView(bot, guild_id, category, current_timeframe=timeframes[category])
                                        await existing_msg.edit(embed=embed, view=view)
                                    else:
                                        await existing_msg.edit(embed=embed)
                                        
                                    message_updated = True
                                    logger.info(f"Updated {category} leaderboard for guild {guild.name}")
                                except discord.NotFound:
                                    logger.warning(f"Message {message_id} for {category} not found in guild {guild.name}")
                                except Exception as e:
                                    logger.error(f"Error updating {category} message in guild {guild.name}: {e}")
                            
                            # Skip creation of new messages during startup refresh
                            if not message_updated:
                                logger.info(f"Skipping creation of new {category} leaderboard message for guild {guild.name}")
                        
                        except Exception as e:
                            logger.error(f"Error processing {category} leaderboard for guild {guild.name}: {e}")
                    
                    # Update last_updated timestamp
                    leaderboard.last_updated = datetime.now()
                    await session.commit()
                    logger.info(f"Finished refreshing leaderboards for guild {guild.name}")
                    
                except Exception as e:
                    logger.error(f"Error refreshing leaderboards for guild ID {guild_id}: {e}")
        
    except Exception as e:
        logger.error(f"Error in refresh_all_leaderboards: {e}")
        
def setup(bot):
    bot.add_cog(LeaderboardCog(bot))