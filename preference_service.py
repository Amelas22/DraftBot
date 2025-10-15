from sqlalchemy import select, update, insert
from datetime import datetime
from session import AsyncSessionLocal
from database.models_base import Base
from loguru import logger

from sqlalchemy import Column, String, Boolean, DateTime, Index, func, text, TIMESTAMP

class PlayerPreferences(Base):
    __tablename__ = 'player_preferences'

    id = Column(String(128), primary_key=True, nullable=True)  # Composite ID from player_id and guild_id
    player_id = Column(String(64), nullable=False)
    guild_id = Column(String(64), nullable=False)
    is_bet_capped = Column(Boolean, default=True, server_default=text('1'))
    dm_notifications = Column(Boolean, default=False, server_default=text('0'))
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
    Returns boolean: False for disabled (default), True for enabled.

    Args:
        player_id (str): Discord user ID
        guild_id (str): Discord server ID

    Returns:
        bool: True if DM notifications are enabled, False otherwise
    """
    async def _query(session):
        composite_id = get_composite_preference_key(player_id, guild_id)
        stmt = select(PlayerPreferences).where(PlayerPreferences.id == composite_id)
        result = await session.execute(stmt)
        preference = result.scalar_one_or_none()

        if preference:
            logger.debug(f"Found existing DM notification preference for player {player_id} in guild {guild_id}: {preference.dm_notifications}")
            return preference.dm_notifications
        else:
            logger.debug(f"No DM notification preference found for player {player_id} in guild {guild_id}, defaulting to disabled")
            return False

    return await execute_db_operation(_query, default_value=False, error_message="Error getting DM notification preference")

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

    # Set default preferences (all disabled)
    default_preferences = {player_id: False for player_id in player_ids}

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
        for pref in stored_preferences:
            preferences[pref.player_id] = pref.dm_notifications

        # Log which players don't have preferences set
        players_without_prefs = set(player_ids) - set(p.player_id for p in stored_preferences)
        if players_without_prefs:
            logger.debug(f"Players without stored preferences (defaulting to False): {players_without_prefs}")

        logger.info(f"Retrieved DM preferences for {len(player_ids)} players: {sum(preferences.values())} enabled")
        return preferences

    return await execute_db_operation(_batch_query, default_value=default_preferences, error_message="Error getting DM notification preferences")