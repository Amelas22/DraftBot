from enum import Enum
from typing import Optional, Dict, Any, Tuple
import discord
from sqlalchemy import JSON, Column, Integer, String, Boolean, Float, select, text, REAL
from sqlalchemy.ext.asyncio import AsyncSession
from views import PersistentView
from database.models_base import Base
from session import AsyncSessionLocal, get_draft_session
from quiz_views_module.quiz_views import QuizPublicView
from loguru import logger
import time
import asyncio


class StickyUpdateResult(Enum):
    """Result codes for sticky message update operations."""
    SUCCESS = "success"              # Message was successfully updated
    SKIPPED = "skipped"              # Update skipped (e.g., not enough messages)
    CLEANED_UP = "cleaned_up"        # Invalid state, sticky record was removed
    FAILED = "failed"                # Update failed but record preserved (transient error)


# Constants
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


async def _get_guild(bot: discord.Client, guild_id: str) -> Optional[discord.Guild]:
    """Get guild by ID, with fallback to fetch if not cached."""
    guild = bot.get_guild(int(guild_id))
    if not guild:
        try:
            guild = await bot.fetch_guild(int(guild_id))
        except Exception:
            return None
    return guild


async def find_notification_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Find the notification channel in the guild if it exists."""
    for channel in guild.channels:
        if isinstance(channel, discord.TextChannel) and channel.name.lower() == DRAFT_NOTIFICATION_CHANNEL:
            return channel
    return None


async def find_session_role(guild: discord.Guild, session_type: str) -> Optional[discord.Role]:
    """Find the appropriate role for the given session type in the guild."""
    from config import get_config
    config = get_config(guild.id)

    roles_config = config.get("roles", {})
    session_roles = roles_config.get("session_roles", {})
    default_drafter = roles_config.get("drafter")

    role_name = session_roles.get(session_type, default_drafter)
    if not role_name:
        return None

    for role in guild.roles:
        if role.name.lower() == role_name.lower():
            return role

    return None


# --- Sticky Strategy Interface & Implementations ---

class StickyStrategy:
    """Abstract base class for sticky message strategies."""
    
    async def should_update(self, sticky_message: Message) -> bool:
        """Determine if update is needed beyond standard inactivity/volume checks.
        Default is True since the manager handles the basic checks.
        """
        return True

    async def validate_state(self, sticky_message: Message, session: AsyncSession) -> bool:
        """Check if the underlying state (e.g. draft session) still exists.
        If False, the sticky message will be removed.
        """
        return True

    async def generate_content(
        self, sticky_message: Message, bot: discord.Client, session: AsyncSession
    ) -> Tuple[str, Optional[discord.Embed], Optional[discord.ui.View], Dict[str, Any]]:
        """Generate the content, embed, view, and updated metadata for the new message.
        
        Returns:
            (content, embed, view, updated_view_metadata)
        """
        raise NotImplementedError

    async def on_update_success(
        self, sticky_message: Message, new_message: discord.Message, bot: discord.Client, session: AsyncSession
    ) -> None:
        """Hook for side effects (notifications, specific DB updates) after successful pin.

        Args:
            sticky_message: The sticky message record being updated
            new_message: The newly posted Discord message
            bot: The Discord bot client instance
            session: The active database session (caller handles commit)
        """
        pass


class DraftStickyStrategy(StickyStrategy):
    """Strategy for Draft Session sticky messages."""

    async def validate_state(self, sticky_message: Message, session: AsyncSession) -> bool:
        draft_session_id = sticky_message.view_metadata.get("draft_session_id")
        if not draft_session_id:
            logger.warning(f"Missing draft_session_id in view_metadata for channel {sticky_message.channel_id}")
            return False
        
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session:
            logger.warning(f"DraftSession {draft_session_id} not found for channel {sticky_message.channel_id}")
            return False
            
        return True

    async def generate_content(
        self, sticky_message: Message, bot: discord.Client, session: AsyncSession
    ) -> Tuple[str, Optional[discord.Embed], Optional[discord.ui.View], Dict[str, Any]]:
        draft_session_id = sticky_message.view_metadata.get("draft_session_id")
        draft_session = await get_draft_session(draft_session_id)

        # Update metadata with current stage
        updated_metadata = sticky_message.view_metadata.copy()
        updated_metadata["session_stage"] = draft_session.session_stage

        # Get existing embed from old message, or regenerate if not found
        channel = await bot.fetch_channel(int(sticky_message.channel_id))
        try:
            old_message = await channel.fetch_message(int(sticky_message.message_id))
            embed = old_message.embeds[0] if old_message.embeds else None
        except discord.NotFound:
            logger.warning(f"Old draft message not found, regenerating embed for session {draft_session_id}")
            embed = None

        # If embed is missing (old message deleted or had no embed), regenerate it
        if embed is None:
            from utils import generate_draft_summary_embed
            embed = await generate_draft_summary_embed(bot, draft_session_id)
            if embed is None:
                logger.error(f"Failed to regenerate embed for draft session {draft_session_id}")
                raise ValueError(f"Cannot generate content without embed for session {draft_session_id}")

        view_type = updated_metadata.get("view_type", "draft")
        if view_type == "quiz":
            view = await QuizPublicView.from_metadata(bot, updated_metadata)
        else:
            view = PersistentView.from_metadata(bot, updated_metadata)

        content = sticky_message.content
        return content, embed, view, updated_metadata

    async def on_update_success(
        self, sticky_message: Message, new_message: discord.Message, bot: discord.Client, session: AsyncSession
    ) -> None:
        """Update DraftSession record and post/update notification in wheres-the-draft channel."""
        draft_session_id = sticky_message.view_metadata.get("draft_session_id")
        draft_session = await get_draft_session(draft_session_id)
        if draft_session:
            # get_draft_session creates its own session, so we need to merge into current session
            draft_session = await session.merge(draft_session)
            draft_session.message_id = str(new_message.id)

        # Post/Update Notification in wheres-the-draft
        new_notification_id = await self._post_or_update_notification(bot, sticky_message)
        if new_notification_id:
            sticky_message.notification_message_id = new_notification_id

    async def _post_or_update_notification(self, bot: discord.Client, sticky_message: Message) -> Optional[str]:
        """Internal helper to manage the notification in wheres-the-draft channel."""
        try:
            guild_id = sticky_message.guild_id
            guild = await _get_guild(bot, guild_id)
            if not guild:
                return None

            notification_channel = await find_notification_channel(guild)
            if not notification_channel:
                return None
            
            message_link = f"https://discord.com/channels/{guild_id}/{sticky_message.channel_id}/{sticky_message.message_id}"
            content = f"{message_link}: Looking for Drafters"

            # Add role mention only on first notification (not on updates)
            # This prevents spamming the role on every sticky message refresh
            if not sticky_message.notification_message_id:
                draft_session_id = sticky_message.view_metadata.get("draft_session_id")
                draft_session = await get_draft_session(draft_session_id)
                if draft_session:
                    session_role = await find_session_role(guild, draft_session.session_type)
                    if session_role:
                        content = f"{session_role.mention} {content}"

            if sticky_message.notification_message_id:
                try:
                    notification_message = await notification_channel.fetch_message(int(sticky_message.notification_message_id))
                    await notification_message.edit(content=content)
                    return sticky_message.notification_message_id
                except discord.NotFound:
                    sticky_message.notification_message_id = None # Logic to fall through to send new
            
            # Send new if didn't exist or wasn't found
            new_notification = await notification_channel.send(content=content)
            return str(new_notification.id)
            
        except Exception as e:
            logger.error(f"Error posting draft notification: {e}")
            return None


class DebtSummaryStickyStrategy(StickyStrategy):
    """Strategy for Debt Summary sticky messages."""

    async def validate_state(self, sticky_message: Message, session: AsyncSession) -> bool:
        # Debt summaries are always valid as long as the channel/guild exists
        return True

    async def generate_content(
        self, sticky_message: Message, bot: discord.Client, session: AsyncSession
    ) -> Tuple[str, Optional[discord.Embed], Optional[discord.ui.View], Dict[str, Any]]:
        # Function-local imports to avoid circular dependencies
        from services.debt_service import get_guild_debt_rows
        from debt_views.helpers import build_guild_debt_embed
        from debt_views.settle_views import PublicSettleDebtsView

        guild_id = sticky_message.guild_id
        guild = bot.get_guild(int(guild_id))
        
        # Re-fetch debt data to allow the sticky update to refresh content
        rows = await get_guild_debt_rows(guild_id)
        embed = build_guild_debt_embed(guild, rows)
        
        view = PublicSettleDebtsView()
        
        # Metadata doesn't change much for debt summary
        return sticky_message.content, embed, view, sticky_message.view_metadata

    async def on_update_success(
        self, sticky_message: Message, new_message: discord.Message, bot: discord.Client, session: AsyncSession
    ) -> None:
        # No notifications for debt summaries
        pass


def get_sticky_strategy(view_metadata: Dict[str, Any]) -> StickyStrategy:
    """Factory method to get the appropriate strategy."""
    view_type = view_metadata.get("view_type", "draft")
    
    if view_type == "debt_summary":
        return DebtSummaryStickyStrategy()
    elif view_type == "draft" or view_type == "quiz":
        return DraftStickyStrategy()
    else:
        # Default fallback
        logger.warning(f"Unknown view_type '{view_type}', falling back to DraftStickyStrategy")
        return DraftStickyStrategy()


# --- Sticky Message Management Functions ---

async def delete_sticky_message_record(sticky_message: Message, session: AsyncSession) -> None:
    """Safely deletes a sticky message record and its associated notification."""
    try:        
        await session.delete(sticky_message)
        logger.info(f"Deleted sticky message record for channel {sticky_message.channel_id}")
    except Exception as e:
        logger.error(f"Error deleting sticky message record: {e}")


async def handle_sticky_message_update(
    sticky_message: Message, bot: discord.Client, session: AsyncSession, force: bool = False
) -> StickyUpdateResult:
    """Handles the process of updating and pinning the sticky message in Discord.

    Args:
        sticky_message: The sticky message record to update
        bot: The Discord bot client
        session: The active database session
        force: If True, skip message count threshold check

    Returns:
        StickyUpdateResult indicating the outcome of the operation
    """
    # Check if message_count threshold is met before doing anything
    if not force and sticky_message.message_count < MESSAGES_BEFORE_REGULAR_UPDATE:
        logger.info(f"Not enough messages ({sticky_message.message_count}/{MESSAGES_BEFORE_REGULAR_UPDATE}) to update sticky message in channel {sticky_message.channel_id}")
        return StickyUpdateResult.SKIPPED

    # Get Strategy
    if not sticky_message.view_metadata:
        logger.warning(f"Missing view_metadata for channel {sticky_message.channel_id}. Removing sticky message.")
        await delete_sticky_message_record(sticky_message, session)
        await session.commit()
        return StickyUpdateResult.CLEANED_UP

    strategy = get_sticky_strategy(sticky_message.view_metadata)

    # Validate state
    if not await strategy.validate_state(sticky_message, session):
        logger.warning(f"Sticky message validation failed for channel {sticky_message.channel_id}. Removing.")
        await delete_sticky_message_record(sticky_message, session)
        await session.commit()
        return StickyUpdateResult.CLEANED_UP

    channel = await bot.fetch_channel(int(sticky_message.channel_id))

    # Generate new content
    try:
        content, embed, view, updated_metadata = await strategy.generate_content(sticky_message, bot, session)
    except Exception as e:
        logger.error(f"Failed to generate content for sticky message in {sticky_message.channel_id}: {e}")
        return StickyUpdateResult.FAILED

    old_message_id = sticky_message.message_id

    # Send new message
    try:
        new_message = await channel.send(content=content, embed=embed, view=view)
        await new_message.pin()
        logger.info(f"Pinned new sticky message with ID {new_message.id} in channel {channel.id}")
    except discord.Forbidden:
        logger.error(f"Missing permissions to pin in {channel.id}")
        return StickyUpdateResult.FAILED
    except discord.HTTPException as e:
        logger.error(f"HTTP error sending sticky message: {e}")
        return StickyUpdateResult.FAILED

    # Update DB Record
    sticky_message.message_id = str(new_message.id)
    sticky_message.view_metadata = updated_metadata
    sticky_message.message_count = 0
    sticky_message.last_activity = time.time()
    sticky_message.last_update_time = time.time()

    # Run side effects
    await strategy.on_update_success(sticky_message, new_message, bot, session)

    # Commit all changes
    await session.commit()

    # Delete old message
    try:
        old_message = await channel.fetch_message(int(old_message_id))
        await old_message.delete()
        logger.info(f"Deleted old sticky message with ID {old_message_id}")
    except discord.NotFound:
        logger.info(f"Old message {old_message_id} was already deleted")
    except Exception as e:
        logger.warning(f"Failed to delete old message: {e}")

    return StickyUpdateResult.SUCCESS


async def check_channels_for_inactivity(bot: discord.Client) -> None:
    """Background task that periodically checks all channels with sticky messages for inactivity."""
    await bot.wait_until_ready()
    logger.info("Starting background task to check for inactive channels")

    failure_tracker: Dict[str, int] = {}
    MAX_CONSECUTIVE_FAILURES = 3

    while not bot.is_closed():
        current_time = time.time()
        async with AsyncSessionLocal() as session:
            sticky_messages = await fetch_all_sticky_messages(session)

            for sticky_message in sticky_messages:
                sticky_key = f"{sticky_message.channel_id}-{sticky_message.id}"

                if failure_tracker.get(sticky_key, 0) >= MAX_CONSECUTIVE_FAILURES:
                    continue

                elapsed_time = current_time - sticky_message.last_activity
                time_since_last_update = current_time - (sticky_message.last_update_time or 0)

                should_update = False

                # Inactivity check: channel quiet for a while with pending messages
                if elapsed_time >= INACTIVITY_THRESHOLD and sticky_message.message_count >= MESSAGES_BEFORE_REGULAR_UPDATE:
                    should_update = True
                # Volume check: many messages, respecting anti-spam cooldown
                elif (sticky_message.message_count >= MESSAGES_BEFORE_VOLUME_UPDATE and
                      time_since_last_update >= ANTI_SPAM_COOLDOWN_SECONDS):
                    should_update = True

                if should_update:
                    try:
                        result = await handle_sticky_message_update(sticky_message, bot, session)
                        if result == StickyUpdateResult.SUCCESS or result == StickyUpdateResult.CLEANED_UP:
                            failure_tracker[sticky_key] = 0
                        elif result == StickyUpdateResult.FAILED:
                            failure_tracker[sticky_key] = failure_tracker.get(sticky_key, 0) + 1
                        # SKIPPED doesn't affect failure count
                    except Exception as e:
                        logger.error(f"Error in sticky update loop: {e}")
                        failure_tracker[sticky_key] = failure_tracker.get(sticky_key, 0) + 1

        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL)


async def setup_sticky_handler(bot: discord.Client) -> None:
    """Sets up event handlers for managing sticky messages in Discord."""
    logger.info("Setting up sticky message handler")
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

            sticky_message.last_activity = current_time
            sticky_message.message_count += 1
            await session.commit()

    @bot.event
    async def on_message_unpin(message: discord.Message) -> None:
        await remove_sticky_message(message)

    @bot.event
    async def on_message_delete(message: discord.Message) -> None:
        await remove_sticky_message(message)


async def make_message_sticky(
    guild_id: str, channel_id: str, message: discord.Message, view, bot: discord.Client
) -> None:
    """Pins a message in a channel and saves it as sticky in the database.

    Args:
        guild_id: The Discord guild ID
        channel_id: The Discord channel ID
        message: The Discord message to make sticky
        view: The view associated with the message (must have to_metadata() method)
        bot: The Discord bot client instance
    """
    async with AsyncSessionLocal() as session:
        existing_sticky = await fetch_sticky_message(channel_id, session)

        if hasattr(view, "to_metadata"):
            view_metadata = view.to_metadata()
        else:
            view_metadata = {}

        if not message.pinned:
            await message.pin()

        current_time = time.time()

        if existing_sticky:
            existing_sticky.message_id = str(message.id)
            existing_sticky.content = message.content
            existing_sticky.view_metadata = view_metadata
            existing_sticky.message_count = 0
            existing_sticky.last_activity = current_time
            existing_sticky.last_update_time = current_time
            sticky_message = existing_sticky
        else:
            sticky_message = Message(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=str(message.id),
                content=message.content,
                view_metadata=view_metadata,
                is_sticky=True,
                message_count=0,
                last_activity=current_time,
                last_update_time=current_time
            )
            session.add(sticky_message)

        # Trigger side effects (e.g., notifications for draft messages)
        strategy = get_sticky_strategy(view_metadata)
        await strategy.on_update_success(sticky_message, message, bot, session)

        await session.commit()
        logger.info(f"Sticky message ID {message.id} committed for channel {channel_id}")


async def remove_sticky_message(message: discord.Message) -> None:
    """Removes a sticky message from the database if it matches the given message."""
    async with AsyncSessionLocal() as session:
        sticky_message = await fetch_sticky_message(str(message.channel.id), session)
        if not sticky_message or sticky_message.message_id != str(message.id):
            return
        
        # If notification exists, try delete
        if sticky_message.notification_message_id:
            try:
                guild = message.guild
                notification_channel = await find_notification_channel(guild)
                if notification_channel:
                    notification_msg = await notification_channel.fetch_message(int(sticky_message.notification_message_id))
                    await notification_msg.delete()
            except Exception as e:
                logger.error(f"Error deleting notification: {e}")

        await session.delete(sticky_message)
        await session.commit()