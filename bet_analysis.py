"""
Script to analyze users who set their maximum stake to 10 tix in completed drafts.

This script identifies users who chose the conservative approach of setting
their max_stake to only 10 tix (the minimum) in staked drafts that went to completion.
"""

import asyncio
from sqlalchemy import select, and_, func
from datetime import datetime
from models.draft_session import DraftSession
from models.stake import StakeInfo
from database.db_session import db_session


async def get_last_ten_10tix_max_stakes():
    """
    Analyze the last 10 times someone set their max_stake to 10 tix in completed drafts
    for guild 1355718878298116096
    """
    guild_id = "1355718878298116096"
    
    async with db_session() as session:
        # Query for completed drafts where users set max_stake to 10 tix, ordered by most recent first
        stmt = select(DraftSession, StakeInfo).join(
            StakeInfo, DraftSession.session_id == StakeInfo.session_id
        ).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.victory_message_id_draft_chat.isnot(None),  # Completed drafts
                StakeInfo.max_stake == 10  # Users who set max_stake to 10 tix
            )
        ).order_by(DraftSession.draft_start_time.desc())  # Most recent first
        
        result = await session.execute(stmt)
        records = result.fetchall()
        
        if not records:
            print("No users found who set max_stake to 10 tix in completed drafts for this guild.")
            return
        
        # Get the last 10 records
        last_ten = records[:10]
        
        print("Last 10 times someone set their max stake to 10 tix:")
        print("=" * 70)
        
        dates = []
        for i, (draft_session, stake_info) in enumerate(last_ten, 1):
            # Get user name from sign_ups JSON field
            user_name = draft_session.sign_ups.get(stake_info.player_id, "Unknown User")
            date_str = draft_session.draft_start_time.strftime("%Y-%m-%d %H:%M:%S")
            dates.append(draft_session.draft_start_time)
            
            print(f"{i}. {date_str} - {user_name} - {draft_session.draft_id}")
        
        print("\n" + "=" * 70)
        
        if len(dates) >= 2:
            # Get date range from oldest to newest of these 10
            start_date = min(dates)
            end_date = max(dates)
            
            print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
            
            # Count total completed drafts in this date range
            count_stmt = select(func.count(DraftSession.session_id)).where(
                and_(
                    DraftSession.guild_id == guild_id,
                    DraftSession.victory_message_id_draft_chat.isnot(None),
                    DraftSession.draft_start_time >= start_date,
                    DraftSession.draft_start_time <= end_date
                )
            )
            
            total_count = await session.scalar(count_stmt)
            
            print(f"Total completed drafts in this period: {total_count}")
            
            # Also show the breakdown of 10 tix max stakes vs total
            print(f"Users who set max stake to 10 tix in this period: {len(last_ten)}")
            if total_count > 0:
                print(f"Percentage of these instances vs total drafts: {(len(last_ten)/total_count)*100:.1f}%")
            
        else:
            print("Not enough data to calculate date range statistics.")
            print(f"Only found {len(last_ten)} records of users setting max stake to 10 tix.")


async def get_additional_stats():
    """
    Get some additional statistics about users setting max_stake to 10 tix
    """
    guild_id = "1355718878298116096"
    
    async with db_session() as session:
        # Get total number of completed drafts
        total_drafts_stmt = select(func.count(DraftSession.session_id)).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.victory_message_id_draft_chat.isnot(None)
            )
        )
        total_drafts = await session.scalar(total_drafts_stmt)
        
        # Get total number of instances where users set max_stake to 10 tix
        total_10tix_max_stmt = select(func.count(StakeInfo.id)).join(
            DraftSession, StakeInfo.session_id == DraftSession.session_id
        ).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.victory_message_id_draft_chat.isnot(None),
                StakeInfo.max_stake == 10
            )
        )
        total_10tix_max = await session.scalar(total_10tix_max_stmt)
        
        # Get count of unique users who have set max_stake to 10 tix
        unique_users_stmt = select(func.count(func.distinct(StakeInfo.player_id))).join(
            DraftSession, StakeInfo.session_id == DraftSession.session_id
        ).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.victory_message_id_draft_chat.isnot(None),
                StakeInfo.max_stake == 10
            )
        )
        unique_users = await session.scalar(unique_users_stmt)
        
        print(f"\nADDITIONAL STATISTICS:")
        print("=" * 40)
        print(f"Total completed drafts in guild: {total_drafts}")
        print(f"Total instances of users setting max stake to 10 tix: {total_10tix_max}")
        print(f"Unique users who have set max stake to 10 tix: {unique_users}")
        if total_drafts > 0:
            print(f"Average 10 tix max stakes per completed draft: {total_10tix_max/total_drafts:.2f}")
        if unique_users > 0:
            print(f"Average times each user sets max stake to 10 tix: {total_10tix_max/unique_users:.2f}")


async def main():
    """Main function to run the analysis"""
    print("Analyzing users who set their max stake to 10 tix...")
    print(f"Guild ID: 1355718878298116096")
    print(f"Analysis run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    try:
        await get_last_ten_10tix_max_stakes()
        await get_additional_stats()
        
    except Exception as e:
        print(f"Error occurred during analysis: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())