from sqlalchemy import select, update, insert
from datetime import datetime
from session import AsyncSessionLocal
from database.models_base import Base
from loguru import logger
from config import get_dm_notifications_default

from sqlalchemy import Column, String, Boolean, DateTime, Index, func, text, TIMESTAMP

class PlayerPreferences(Base):
    __tablename__ = 'player_preferences'

    id = Column(String(128), primary_key=True, nullable=True)  # Composite ID from player_id and guild_id
    player_id = Column(String(64), nullable=False)
    guild_id = Column(String(64), nullable=False)
    is_bet_capped = Column(Boolean, default=True, server_default=text('1'))
    # NULL until the player toggles it: the row is shared with unrelated answers,
    # and creating it for one of those must not decide DMs. Unset follows the guild default.
    dm_notifications = Column(Boolean, nullable=True)
    # Whether this player borrows their deck from the server's rental library.
    # NULL means they have not been asked, which is what decides whether the
    # sign-up flow asks them (services/library_prompt).
    uses_library = Column(Boolean, nullable=True)
    # Whether this player's donation-day credit shows as "Anonymous". NULL is
    # "named", the default; nullable with no default because the row is shared.
    donation_anonymous = Column(Boolean, nullable=True)
    last_updated = Column(TIMESTAMP, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'))

    __table_args__ = (
        Index('idx_player_guild', 'player_id', 'guild_id'),
    )

def get_composite_preference_key(player_id, guild_id):
    """
    Create a composite key for player preferences.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID

    Returns:
        str: Composite key in format "{player_id}_{guild_id}"

    Raises:
        ValueError: If player_id or guild_id contains an underscore
    """
    if '_' in str(player_id):
        raise ValueError(f"player_id cannot contain underscore: {player_id}")
    if '_' in str(guild_id):
        raise ValueError(f"guild_id cannot contain underscore: {guild_id}")

    return f"{player_id}_{guild_id}"

async def execute_db_operation(operation_func, default_value=None, commit=False, error_message="Database operation failed"):
    """
    Generic wrapper for database operations to eliminate boilerplate.

    Args:
        operation_func: Async function that takes a session and performs the database operation
        default_value: Value to return on error (default: None)
        commit: Whether to commit the transaction (default: False for reads, True for writes)
        error_message: Error message to log on failure

    Returns:
        Result from operation_func on success, default_value on error
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            try:
                result = await operation_func(session)
                if commit:
                    await session.commit()
                return result
            except Exception as e:
                logger.error(f"{error_message}: {e}")
                if commit:
                    await session.rollback()
                return default_value

async def get_player_bet_capping_preference(player_id, guild_id):
    """
    Get a player's bet capping preference.
    Returns boolean: True for capped (default), False for uncapped.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID

    Returns:
        bool: True if bets should be capped, False otherwise
    """
    async def _query(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        stmt = select(PlayerPreferences).where(PlayerPreferences.id == composite_id)
        result = await session.execute(stmt)
        preference = result.scalar_one_or_none()

        if preference:
            logger.debug(f"Found existing preference for player {player_id} in guild {guild_id}: {preference.is_bet_capped}")
            return preference.is_bet_capped
        else:
            logger.debug(f"No preference found for player {player_id} in guild {guild_id}, defaulting to capped")
            return True

    return await execute_db_operation(_query, default_value=True, error_message="Error getting bet capping preference")

async def update_player_bet_capping_preference(player_id, guild_id, is_capped):
    """
    Update a player's bet capping preference.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID
        is_capped (bool): Whether bets should be capped

    Returns:
        bool: True if update was successful
    """
    async def _update(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        stmt = select(PlayerPreferences).where(PlayerPreferences.id == composite_id)
        result = await session.execute(stmt)
        preference = result.scalar_one_or_none()

        if preference:
            logger.info(f"Updating preference for player {player_id} in guild {guild_id} to {is_capped}")
            preference.is_bet_capped = is_capped
            preference.last_updated = datetime.now()
            session.add(preference)
        else:
            logger.info(f"Creating new preference for player {player_id} in guild {guild_id}: {is_capped}")
            new_preference = PlayerPreferences(
                id=composite_id,
                player_id=player_id,
                guild_id=guild_id,
                is_bet_capped=is_capped,
                last_updated=datetime.now()
            )
            session.add(new_preference)

        return True

    return await execute_db_operation(_update, default_value=False, commit=True, error_message="Error updating bet capping preference")

# Utility function to get preferences for multiple players at once
async def get_players_bet_capping_preferences(player_ids, guild_id):
    """
    Get bet capping preferences for multiple players at once.

    Args:
        player_ids (list): List of player Discord IDs
        guild_id (str): Discord server ID

    Returns:
        dict: Mapping of player IDs to their capping preferences
    """
    # Set default preferences (all capped)
    default_preferences = {player_id: True for player_id in player_ids}

    # If no player IDs, return default preferences
    if not player_ids:
        return default_preferences

    async def _batch_query(session):
        composite_ids = [get_composite_preference_key(player_id, guild_id) for player_id in player_ids]
        stmt = select(PlayerPreferences).where(PlayerPreferences.id.in_(composite_ids))
        result = await session.execute(stmt)
        stored_preferences = result.scalars().all()

        # Update the preferences dict with stored values
        preferences = default_preferences.copy()
        for pref in stored_preferences:
            preferences[pref.player_id] = pref.is_bet_capped

        return preferences

    return await execute_db_operation(_batch_query, default_value=default_preferences, error_message="Error getting bet capping preferences")

async def get_player_dm_notification_preference(player_id, guild_id):
    """
    Get a player's DM notification preference.
    Returns boolean: the player's own choice, else the guild default.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID

    Returns:
        bool: True if DM notifications are enabled, False otherwise
    """
    # One rule for both readers: the batch read decides what an unset row means.
    # Stored ids are strings, and the batch result keeps the caller's key type.
    player_id = str(player_id)
    return (await get_players_dm_notification_preferences([player_id], guild_id))[player_id]

async def update_player_dm_notification_preference(player_id, guild_id, enabled):
    """
    Update a player's DM notification preference.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID
        enabled (bool): Whether DM notifications should be enabled

    Returns:
        bool: True if update was successful
    """
    logger.debug(f"update_player_dm_notification_preference called: player_id={player_id}, guild_id={guild_id}, enabled={enabled}")

    async def _update(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        stmt = select(PlayerPreferences).where(PlayerPreferences.id == composite_id)
        result = await session.execute(stmt)
        preference = result.scalar_one_or_none()

        if preference:
            logger.info(f"Updating existing DM notification preference for player {player_id} in guild {guild_id} from {preference.dm_notifications} to {enabled}")
            preference.dm_notifications = enabled
            preference.last_updated = datetime.now()
            session.add(preference)
        else:
            logger.info(f"Creating new preference entry with DM notifications for player {player_id} in guild {guild_id}: {enabled}")
            new_preference = PlayerPreferences(
                id=composite_id,
                player_id=player_id,
                guild_id=guild_id,
                dm_notifications=enabled,
                last_updated=datetime.now()
            )
            session.add(new_preference)

        logger.info(f"Successfully committed DM notification preference update for player {player_id}")
        return True

    return await execute_db_operation(_update, default_value=False, commit=True, error_message="Error updating DM notification preference")

async def get_players_dm_notification_preferences(player_ids, guild_id):
    """
    Get DM notification preferences for multiple players at once.

    Args:
        player_ids (list): List of player Discord IDs
        guild_id (str): Discord server ID

    Returns:
        dict: Mapping of player IDs to their DM notification preferences (True/False)
    """
    logger.debug(f"get_players_dm_notification_preferences called for {len(player_ids)} players in guild {guild_id}")

    # Set default preferences based on guild configuration
    guild_default = get_dm_notifications_default(guild_id)
    default_preferences = {player_id: guild_default for player_id in player_ids}

    # If no player IDs, return default preferences
    if not player_ids:
        logger.debug("No player IDs provided, returning empty preferences")
        return default_preferences

    async def _batch_query(session):
        composite_ids = [get_composite_preference_key(player_id, guild_id) for player_id in player_ids]
        stmt = select(PlayerPreferences).where(PlayerPreferences.id.in_(composite_ids))
        result = await session.execute(stmt)
        stored_preferences = result.scalars().all()

        # Update the preferences dict with stored values
        preferences = default_preferences.copy()
        chosen = {pref.player_id: pref.dm_notifications for pref in stored_preferences
                  if pref.dm_notifications is not None}
        preferences.update(chosen)

        # Log which players haven't chosen
        players_without_prefs = set(player_ids) - chosen.keys()
        if players_without_prefs:
            logger.debug(f"Players without a DM preference (using guild default {guild_default}): {players_without_prefs}")

        logger.info(f"Retrieved DM preferences for {len(player_ids)} players: {sum(preferences.values())} enabled")
        return preferences

    return await execute_db_operation(_batch_query, default_value=default_preferences, error_message="Error getting DM notification preferences")

async def get_uses_library(player_id, guild_id):
    """Whether this player borrows from the library here: True, False, or None
    if they have not been asked.

    None is the point. Unlike the getters above there is no default to fall
    back to, because "not asked" is what decides whether the sign-up flow asks
    (services/library_prompt) -- a default would mean nobody is ever asked.

    Raises on a database error rather than answering None, which would read as
    "not asked" and flip a switch the wrong way.
    """
    async with AsyncSessionLocal() as session:
        preference = await session.get(
            PlayerPreferences, get_composite_preference_key(player_id, guild_id))
    return None if preference is None else preference.uses_library


async def set_uses_library(player_id, guild_id, uses_library):
    """Save whether this player borrows from the library here. True on success."""
    async def _update(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        preference = await session.get(PlayerPreferences, composite_id)
        if preference is None:
            session.add(PlayerPreferences(
                id=composite_id, player_id=str(player_id), guild_id=str(guild_id),
                uses_library=uses_library, last_updated=datetime.now()))
        else:
            preference.uses_library = uses_library
            preference.last_updated = datetime.now()
        logger.info(f"Library preference for player {player_id} in guild {guild_id}: {uses_library}")
        return True

    return await execute_db_operation(_update, default_value=False, commit=True,
                                      error_message="Error updating library preference")


async def get_players_using_library(player_ids, guild_id):
    """The players among these who said they borrow from the library here.

    Unset and False both leave a player out: a deck is offered only to somebody
    who asked for one. Raises on a database error rather than answering "nobody",
    which deck assignment would act on as a whole pod declining.
    """
    if not player_ids:
        return set()
    composite_ids = [get_composite_preference_key(p, guild_id) for p in player_ids]
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(PlayerPreferences.player_id).where(
                PlayerPreferences.id.in_(composite_ids),
                PlayerPreferences.uses_library.is_(True)))).scalars().all()
    return {str(p) for p in rows}


async def set_donation_anonymous(player_id, guild_id, anonymous):
    """Save whether this player shows as Anonymous on the donation page. True on success."""
    async def _update(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        preference = await session.get(PlayerPreferences, composite_id)
        if preference is None:
            session.add(PlayerPreferences(
                id=composite_id, player_id=str(player_id), guild_id=str(guild_id),
                donation_anonymous=anonymous, last_updated=datetime.now()))
        else:
            preference.donation_anonymous = anonymous
            preference.last_updated = datetime.now()
        logger.info(f"Donation anonymity for player {player_id} in guild {guild_id}: {anonymous}")
        return True

    return await execute_db_operation(_update, default_value=False, commit=True,
                                      error_message="Error updating donation anonymity")


async def get_anonymous_donors(player_ids, guild_id):
    """The players among these who asked to show as Anonymous on the donation page.

    Raises on a database error rather than answering "nobody", which would
    publish names people asked to hide.
    """
    if not player_ids:
        return set()
    composite_ids = [get_composite_preference_key(p, guild_id) for p in player_ids]
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(PlayerPreferences.player_id).where(
                PlayerPreferences.id.in_(composite_ids),
                PlayerPreferences.donation_anonymous.is_(True)))).scalars().all()
    return {str(p) for p in rows}
