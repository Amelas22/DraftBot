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
    last_updated = Column(TIMESTAMP, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'))
    
    __table_args__ = (
        Index('idx_player_guild', 'player_id', 'guild_id'),
    )

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
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Create a composite key
            composite_id = f"{player_id}_{guild_id}"
            
            # Try to find the player's preference
            stmt = select(PlayerPreferences).where(
                PlayerPreferences.id == composite_id
            )
            result = await session.execute(stmt)
            preference = result.scalar_one_or_none()
            
            if preference:
                logger.debug(f"Found existing preference for player {player_id} in guild {guild_id}: {preference.is_bet_capped}")
                return preference.is_bet_capped
            else:
                # If no preference found, default to capped (True)
                logger.debug(f"No preference found for player {player_id} in guild {guild_id}, defaulting to capped")
                return True

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
    async with AsyncSessionLocal() as session:
        async with session.begin():
            try:
                # Create a composite key
                composite_id = f"{player_id}_{guild_id}"
                
                # Check if the player already has a preference
                stmt = select(PlayerPreferences).where(
                    PlayerPreferences.id == composite_id
                )
                result = await session.execute(stmt)
                preference = result.scalar_one_or_none()
                
                if preference:
                    # Update existing preference
                    logger.info(f"Updating preference for player {player_id} in guild {guild_id} to {is_capped}")
                    preference.is_bet_capped = is_capped
                    preference.last_updated = datetime.now()
                    session.add(preference)
                else:
                    # Create new preference
                    logger.info(f"Creating new preference for player {player_id} in guild {guild_id}: {is_capped}")
                    new_preference = PlayerPreferences(
                        id=composite_id,
                        player_id=player_id,
                        guild_id=guild_id,
                        is_bet_capped=is_capped,
                        last_updated=datetime.now()
                    )
                    session.add(new_preference)
                    
                await session.commit()
                return True
            except Exception as e:
                logger.error(f"Error updating player preference: {e}")
                await session.rollback()
                return False

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
    preferences = {}
    
    # Set default preferences (all capped)
    for player_id in player_ids:
        preferences[player_id] = True
    
    # If no player IDs, return default preferences
    if not player_ids:
        return preferences
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            try:
                # Create list of composite IDs
                composite_ids = [f"{player_id}_{guild_id}" for player_id in player_ids]
                
                # Query for all preferences in one go
                stmt = select(PlayerPreferences).where(
                    PlayerPreferences.id.in_(composite_ids)
                )
                result = await session.execute(stmt)
                stored_preferences = result.scalars().all()
                
                # Update the preferences dict with stored values
                for pref in stored_preferences:
                    preferences[pref.player_id] = pref.is_bet_capped
                
                return preferences
            except Exception as e:
                logger.error(f"Error getting player preferences: {e}")
                return preferences  # Return default preferences on error