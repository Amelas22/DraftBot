"""
Service for handling stake calculations and storage.
"""
from loguru import logger
from sqlalchemy import select, and_, delete
from session import AsyncSessionLocal, StakeInfo, StakePairing
from draft_organization.stake_calculator import calculate_stakes_with_strategy
from config import get_config

async def calculate_and_store_stakes(guild_id, draft_session, cap_info=None):
    """Calculate and store stakes for a staked draft session."""
    logger.info(f"Calculating stakes for session {draft_session.session_id}")
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            # Get all stake info records for this session
            stake_stmt = select(StakeInfo).where(StakeInfo.session_id == draft_session.session_id)
            results = await db_session.execute(stake_stmt)
            stake_info_records = results.scalars().all()
            
            # Build stakes dictionary
            stakes_dict = {record.player_id: record.max_stake for record in stake_info_records}
            
            # Build capping info dictionary - use passed cap_info if provided
            if cap_info is None:
                # Fall back to using the is_capped values from stake_info_records if no cap_info provided
                cap_info = {record.player_id: getattr(record, 'is_capped', True) for record in stake_info_records}
            
            # Get configuration
            config = get_config(guild_id)
            use_optimized = config.get("stakes", {}).get("use_optimized_algorithm", False)
            stake_multiple = config.get("stakes", {}).get("stake_multiple", 10)
            
            user_min_stake = draft_session.min_stake or 10
            
            # Use the router function with capping info
            stake_pairs = calculate_stakes_with_strategy(
                draft_session.team_a, 
                draft_session.team_b, 
                stakes_dict,
                min_stake=user_min_stake,  
                multiple=stake_multiple,
                use_optimized=use_optimized,
                cap_info=cap_info
            )
            
            # Delete existing pairings for this session (idempotency)
            await db_session.execute(
                delete(StakePairing).where(StakePairing.session_id == draft_session.session_id)
            )

            # Insert new pairings into stake_pairings table
            processed_pairs = set()  # Track which pairs we've handled to avoid duplicates

            for pair in stake_pairs:
                # Create a unique identifier for this pair
                # Sort the player IDs but keep the amount separate
                pair_id = (tuple(sorted([pair.player_a_id, pair.player_b_id])), pair.amount)

                # Skip if we've already processed this exact pairing
                if pair_id in processed_pairs:
                    continue
                processed_pairs.add(pair_id)

                # Insert the pairing
                pairing = StakePairing(
                    session_id=draft_session.session_id,
                    player_a_id=pair.player_a_id,
                    player_b_id=pair.player_b_id,
                    amount=pair.amount
                )
                db_session.add(pairing)
                logger.debug(f"Added pairing: {pair.player_a_id} ↔ {pair.player_b_id}: {pair.amount} tix")
            
            # Commit the changes
            await db_session.commit()
