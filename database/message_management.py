from typing import Optional, Union
import discord
from sqlalchemy import JSON, Column, Integer, String, Boolean, Float, select, text, REAL
from sqlalchemy.ext.asyncio import AsyncSession
from views import PersistentView
from database.models_base import Base
from session import AsyncSessionLocal, DraftSession, get_draft_session
from quiz_views_module.quiz_views import QuizPublicView
from loguru import logger
import time
import asyncio

MESSAGES_BEFORE_REGULAR_UPDATE = 8
INACTIVITY_THRESHOLD = 120  # 120 seconds (2 minutes) of inactivity
INACTIVITY_CHECK_INTERVAL = 60  # Check for inactive channels every 60 seconds
DRAFT_NOTIFICATION_CHANNEL = "wheres-the-draft"  # Name of the channel to post draft links
MESSAGES_BEFORE_VOLUME_UPDATE = 20  # Higher threshold for the message-only trigger
ANTI_SPAM_COOLDOWN_SECONDS = 180  # Minimum seconds between updates to prevent spam (3 minutes)

class Message(Base):
    """Represents a message stored in the database, potentially a sticky message."""
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    message_id = Column(String(64), nullable=False)
    content = Column(String, nullable=False)
    view_metadata = Column(JSON, nullable=True)
    is_sticky = Column(Boolean, default=False)
    message_count = Column(Integer, default=0)
    last_activity = Column(Float, nullable=False, default=0.0, server_default=text('0.0'))  # Timestamp of last message in channel
    notification_message_id = Column(String(64), nullable=True)  # ID of the notification message in wheres-the-draft channel
    last_update_time = Column(REAL, default=0.0, server_default=text('0.0'))  # Timestamp of when we last updated the sticky message

    def __repr__(self) -> str:
        return (
            f"<Message(guild_id={self.guild_id}, channel_id={self.channel_id}, "
            f"message_id={self.message_id}, is_sticky={self.is_sticky})>"
        )


async def fetch_sticky_message(channel_id: str, session: AsyncSession) -> Optional[Message]:
    """Fetches the sticky message for a given channel from the database."""
    result = await session.execute(
        select(Message).filter_by(channel_id=channel_id, is_sticky=True)
    )
    return result.scalars().first()


async def fetch_all_sticky_messages(session: AsyncSession) -> list[Message]:
    """Fetches all sticky messages from the database."""
    result = await session.execute(
        select(Message).filter_by(is_sticky=True)
    )
    return result.scalars().all()


async def update_draft_session_message(draft_session_id: str, message_id: str, session: AsyncSession) -> None:
    """Updates the draft session with a new sticky message ID."""
    draft_session = await get_draft_session(draft_session_id)
    if not draft_session:
        logger.error(f"DraftSession with ID {draft_session_id} not found in database.")
        return

    # Merge the draft_session into the provided session (it was fetched in a different session)
    draft_session = session.merge(draft_session)
    draft_session.message_id = message_id
    await session.commit()


async def find_notification_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Find the notification channel in the guild if it exists."""
    for channel in guild.channels:
        if isinstance(channel, discord.TextChannel) and channel.name.lower() == DRAFT_NOTIFICATION_CHANNEL:
            return channel
    return None


async def find_session_role(guild: discord.Guild, session_type: str) -> Optional[discord.Role]:
    """Find the appropriate role for the given session type in the guild."""
    logger.debug(f"Finding session role for session_type '{session_type}' in guild '{guild.name}' (ID: {guild.id})")
    
    # Get the guild-specific configuration
    from config import get_config
    config = get_config(guild.id)
    logger.debug(f"Retrieved config for guild {guild.id}: {config.get('roles', {})}")
    
    # Get the roles section first
    roles_config = config.get("roles", {})
    logger.debug(f"Full roles config: {roles_config}")
    
    # Get both values using the same pattern
    session_roles = roles_config.get("session_roles", {})
    default_drafter = roles_config.get("drafter")
    
    logger.debug(f"Session roles config: {session_roles}")
    logger.debug(f"Default drafter role: {default_drafter}")
    
    # Get the role name, falling back to default drafter if not specified
    role_name = session_roles.get(session_type, default_drafter)
    logger.debug(f"Selected role name for {session_type}: {role_name}")
    
    # Find the role with matching name
    guild_roles = [role.name for role in guild.roles]
    logger.debug(f"Available guild roles: {guild_roles}")
    
    for role in guild.roles:
        if role.name.lower() == role_name.lower():
            logger.info(f"Found matching role '{role.name}' for session type '{session_type}'")
            return role
    
    logger.warning(f"No matching role found for name '{role_name}' in guild '{guild.name}'")
    return None


async def post_or_update_notification(
    bot: discord.Client, 
    guild_id: str, 
    draft_channel_id: str, 
    sticky_message_id: str, 
    notification_message_id: Optional[str], 
    session: AsyncSession
) -> Optional[str]:
    """Post or update a notification message in the notification channel."""
    try:
        # Fetch the guild and check if notification channel exists
        guild = bot.get_guild(int(guild_id))
        if not guild:
            guild = await bot.fetch_guild(int(guild_id))
        
        notification_channel = await find_notification_channel(guild)
        if not notification_channel:
            logger.info(f"Notification channel '{DRAFT_NOTIFICATION_CHANNEL}' not found in guild {guild_id}")
            return None
        
        # Create message content with link to draft
        draft_channel = await bot.fetch_channel(int(draft_channel_id))
        # Use Discord's native message link format
        message_link = f"https://discord.com/channels/{guild_id}/{draft_channel_id}/{sticky_message_id}"
        
        content = f"{message_link}: Looking for Drafters"
        
        # If this is the first notification, add a mention to the appropriate role
        if not notification_message_id:
            # Get the sticky message to access its metadata
            sticky_message = await fetch_sticky_message(draft_channel_id, session)
            if sticky_message and sticky_message.view_metadata:
                logger.debug(f"Found sticky message with metadata: {sticky_message.view_metadata}")
                session_id = sticky_message.view_metadata.get("draft_session_id")
                if session_id:
                    logger.debug(f"Found session ID: {session_id}")
                    draft_session = await get_draft_session(session_id)
                    if draft_session:
                        logger.debug(f"Found draft session with type: {draft_session.session_type}")
                        session_role = await find_session_role(guild, draft_session.session_type)
                        if session_role:
                            content = f"{session_role.mention} {content}"
                        else:
                            logger.warning(f"No role found for session type {draft_session.session_type}")
                    else:
                        logger.warning(f"No draft session found for ID {session_id}")
                else:
                    logger.warning("No draft_session_id found in view_metadata")
            else:
                logger.warning(f"No sticky message or metadata found for channel {draft_channel_id}")
        
        # Either update existing notification or create a new one
        if notification_message_id:
            try:
                notification_message = await notification_channel.fetch_message(int(notification_message_id))
                await notification_message.edit(content=content)
                logger.info(f"Updated notification message in channel '{DRAFT_NOTIFICATION_CHANNEL}'")
                return notification_message_id
            except discord.NotFound:
                logger.info(f"Previous notification message not found. Creating a new one.")
                notification_message_id = None
        
        if not notification_message_id:
            new_notification = await notification_channel.send(content=content)
            logger.info(f"Posted new notification message in channel '{DRAFT_NOTIFICATION_CHANNEL}'")
            return str(new_notification.id)
            
    except Exception as e:
        logger.error(f"Error posting/updating notification: {str(e)}")
        logger.exception(e)  # This will log the full stack trace
    
    return None


async def handle_sticky_message_update(sticky_message: Message, bot: discord.Client, session: AsyncSession) -> None:
    """Handles the process of updating and pinning the sticky message in Discord."""
    # Check if message_count threshold is met before doing anything
    if sticky_message.message_count < MESSAGES_BEFORE_REGULAR_UPDATE:
        logger.info(f"Not enough messages ({sticky_message.message_count}/{MESSAGES_BEFORE_REGULAR_UPDATE}) to update sticky message in channel {sticky_message.channel_id}")
        return

    draft_session_id = sticky_message.view_metadata.get("draft_session_id")
    if not draft_session_id:
        logger.error("Missing draft_session_id in view_metadata.")
        return

    # Fetch the current draft session to get its current state
    draft_session = await get_draft_session(draft_session_id)
    if not draft_session:
        logger.error(f"DraftSession with ID {draft_session_id} not found.")
        return
    
    # Update the view metadata with the current session stage
    view_metadata = sticky_message.view_metadata.copy()
    view_metadata["session_stage"] = draft_session.session_stage
    
    channel = await bot.fetch_channel(int(sticky_message.channel_id))
    try:
        old_message = await channel.fetch_message(int(sticky_message.message_id))
        embed = old_message.embeds[0] if old_message.embeds else None
    except discord.NotFound:
        logger.warning(f"Sticky message with ID {sticky_message.message_id} not found.")
        return

    # Create view with updated metadata including the current session stage
    # Support both sync and async from_metadata methods
    view_type = view_metadata.get("view_type", "draft")  # Default to draft for backward compatibility

    if view_type == "quiz":
        # Quiz views require async recreation
        view = await QuizPublicView.from_metadata(bot, view_metadata)
    else:
        # Draft views use sync recreation
        view = PersistentView.from_metadata(bot, view_metadata)

    new_message = await channel.send(content=sticky_message.content, embed=embed, view=view)
    await new_message.pin()
    logger.info(f"Pinned new sticky message with ID {new_message.id} in channel {channel.id}")

    # Save the new message ID to the sticky_message record
    old_message_id = sticky_message.message_id
    sticky_message.message_id = str(new_message.id)
    sticky_message.view_metadata = view_metadata  # Save the updated metadata
    sticky_message.message_count = 0  # Reset message count after update
    sticky_message.last_activity = time.time()  # Reset last activity timestamp
    sticky_message.last_update_time = time.time()  # Record when we did this update
    
    # Update the notification message in the wheres-the-draft channel
    new_notification_id = await post_or_update_notification(
        bot, 
        sticky_message.guild_id, 
        sticky_message.channel_id, 
        sticky_message.message_id, 
        sticky_message.notification_message_id,
        session
    )
    if new_notification_id:
        sticky_message.notification_message_id = new_notification_id
    
    # Update the draft session directly without calling update_draft_session_message
    if draft_session:
        draft_session.message_id = str(new_message.id)
        session.add(draft_session)
    else:
        logger.error(f"DraftSession with ID {draft_session_id} not found in database.")
    
    # Commit all changes at once
    await session.commit()

    # Only after all database changes are committed, delete the old message
    try:
        await old_message.delete()
        logger.info(f"Deleted old sticky message with ID {old_message_id}")
    except discord.NotFound:
        logger.info(f"Old message {old_message_id} was already deleted")

    return True

async def check_channels_for_inactivity(bot: discord.Client) -> None:
    """Background task that periodically checks all channels with sticky messages for inactivity."""
    await bot.wait_until_ready()
    logger.info("Starting background task to check for inactive channels")
    
    failure_tracker = {}
    MAX_CONSECUTIVE_FAILURES = 3
    
    while not bot.is_closed():
        current_time = time.time()
        async with AsyncSessionLocal() as session:
            sticky_messages = await fetch_all_sticky_messages(session)
            
            for sticky_message in sticky_messages:
                # Create a unique key for this sticky message
                sticky_key = f"{sticky_message.channel_id}-{sticky_message.view_metadata.get('draft_session_id')}"
                
                # Skip if this sticky message has failed too many times
                if failure_tracker.get(sticky_key, 0) >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"Skipping update for sticky message in channel {sticky_message.channel_id} - too many consecutive failures")
                    continue
                
                elapsed_time = current_time - sticky_message.last_activity
                time_since_last_update = current_time - (sticky_message.last_update_time or 0)
                
                # Check if update is needed due to inactivity or high message volume
                should_update = False
                
                # Inactivity check
                if elapsed_time >= INACTIVITY_THRESHOLD and sticky_message.message_count >= MESSAGES_BEFORE_REGULAR_UPDATE:
                    logger.info(f"Channel {sticky_message.channel_id} has been inactive for {elapsed_time:.2f}s with {sticky_message.message_count} messages. Updating sticky message.")
                    should_update = True
                
                # Message volume check with anti-spam protection
                elif (sticky_message.message_count >= MESSAGES_BEFORE_VOLUME_UPDATE and 
                      time_since_last_update >= ANTI_SPAM_COOLDOWN_SECONDS):
                    logger.info(f"High message volume detected ({sticky_message.message_count} messages) and anti-spam cooldown passed. Updating sticky message.")
                    should_update = True
                
                if should_update:
                    try:
                        success = await handle_sticky_message_update(sticky_message, bot, session)
                        if success:
                            # Reset failure counter on success
                            failure_tracker[sticky_key] = 0
                        else:
                            # Increment failure counter
                            failure_tracker[sticky_key] = failure_tracker.get(sticky_key, 0) + 1
                            logger.warning(f"Failed to update sticky message in channel {sticky_message.channel_id}. Consecutive failures: {failure_tracker[sticky_key]}")
                    except Exception as e:
                        # Log and count any exceptions as failures
                        logger.error(f"Exception during sticky message update: {str(e)}")
                        failure_tracker[sticky_key] = failure_tracker.get(sticky_key, 0) + 1
        
        # Wait before checking again
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL)


async def setup_sticky_handler(bot: discord.Client) -> None:
    """Sets up event handlers for managing sticky messages in Discord."""
    logger.info("Setting up sticky message handler")
    
    # Start the background task for checking inactive channels
    bot.loop.create_task(check_channels_for_inactivity(bot))

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        current_time = time.time()
        async with AsyncSessionLocal() as session:
            sticky_message = await fetch_sticky_message(str(message.channel.id), session)
            if not sticky_message:
                return

            # Update the last activity timestamp for this channel
            sticky_message.last_activity = current_time
            
            # Increment message count
            sticky_message.message_count += 1
            
            logger.info(f"Updated channel {message.channel.id} activity. Count: {sticky_message.message_count}/{MESSAGES_BEFORE_REGULAR_UPDATE}")
            
            # Just save the changes - actual updates happen in the background check
            await session.commit()

    @bot.event
    async def on_message_unpin(message: discord.Message) -> None:
        await remove_sticky_message(message)

    @bot.event
    async def on_message_delete(message: discord.Message) -> None:
        await remove_sticky_message(message)


async def make_message_sticky(
    guild_id: str, channel_id: str, message: discord.Message, view: PersistentView
) -> None:
    """Pins a message in a channel and saves it as sticky in the database."""
    async with AsyncSessionLocal() as session:
        existing_sticky = await fetch_sticky_message(channel_id, session)
        view_metadata = view.to_metadata()
        if not message.pinned:
            await message.pin()
            logger.info(f"Pinned message ID {message.id} in channel {channel_id} as sticky.")

        current_time = time.time()
        
        # Prepare the sticky message record
        if existing_sticky:
            existing_sticky.message_id = str(message.id)
            existing_sticky.content = message.content
            existing_sticky.view_metadata = view_metadata
            existing_sticky.message_count = 0  # Reset counter on update
            existing_sticky.last_activity = current_time  # Initialize activity timestamp
            existing_sticky.last_update_time = current_time  # Initialize update timestamp
            sticky_message = existing_sticky
            logger.info(f"Updated sticky message in database for channel {channel_id}.")
        else:
            sticky_message = Message(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=str(message.id),
                content=message.content,
                view_metadata=view_metadata,
                is_sticky=True,
                message_count=0,  # Initialize counter
                last_activity=current_time,  # Initialize activity timestamp
                last_update_time=current_time  # Initialize update timestamp
            )
            session.add(sticky_message)
            logger.info(f"Created new sticky message entry for channel {channel_id}.")
        
        # Post notification in wheres-the-draft channel if it exists
        bot = message.guild._state._get_client()
        notification_message_id = await post_or_update_notification(
            bot,
            guild_id,
            channel_id,
            str(message.id),
            None,  # First post, no existing notification message
            session
        )
        if notification_message_id:
            sticky_message.notification_message_id = notification_message_id
        
        await session.commit()
        logger.info(f"Sticky message ID {message.id} committed for channel {channel_id}")


async def remove_sticky_message(message: discord.Message) -> None:
    """Removes a sticky message from the database if it matches the given message."""
    async with AsyncSessionLocal() as session:
        sticky_message = await fetch_sticky_message(str(message.channel.id), session)
        if not sticky_message or sticky_message.message_id != str(message.id):
            return
        
        # If there's a notification message, try to delete it
        if sticky_message.notification_message_id:
            try:
                guild = message.guild
                notification_channel = await find_notification_channel(guild)
                if notification_channel:
                    notification_msg = await notification_channel.fetch_message(int(sticky_message.notification_message_id))
                    await notification_msg.delete()
                    logger.info(f"Deleted notification message with ID {sticky_message.notification_message_id}")
            except Exception as e:
                logger.error(f"Error deleting notification message: {str(e)}")

        await session.delete(sticky_message)
        logger.info(f"Removed sticky message with ID {message.id} from channel {message.channel.id} in database.")
        await session.commit()