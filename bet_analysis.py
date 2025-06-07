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
    Only counts users who are still in the sign_ups for that draft (didn't leave)
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
            return 0
        
        # Filter to only include users who are still in the sign_ups dictionary
        valid_records = []
        for draft_session, stake_info in records:
            # Check if the user is still in the sign_ups (didn't leave the draft)
            if (draft_session.sign_ups and 
                stake_info.player_id in draft_session.sign_ups):
                valid_records.append((draft_session, stake_info))
        
        if not valid_records:
            print("No users found who set max_stake to 10 tix AND stayed in completed drafts.")
            return 0
        
        # Get the last 10 valid records
        last_ten = valid_records[:10]
        
        print("Last 10 times someone set their max stake to 10 tix (and stayed in the draft):")
        print("=" * 80)
        
        dates = []
        for i, (draft_session, stake_info) in enumerate(last_ten, 1):
            # Get user name from sign_ups JSON field (we know it exists since we filtered for it)
            user_name = draft_session.sign_ups[stake_info.player_id]
            date_str = draft_session.draft_start_time.strftime("%Y-%m-%d %H:%M:%S")
            dates.append(draft_session.draft_start_time)
            
            print(f"{i}. {date_str} - {user_name} - {draft_session.draft_id}")
        
        print("\n" + "=" * 80)
        
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
            print(f"Users who set max stake to 10 tix (and stayed) in this period: {len(last_ten)}")
            if total_count > 0:
                print(f"Percentage of these instances vs total drafts: {(len(last_ten)/total_count)*100:.1f}%")
            
        else:
            print("Not enough data to calculate date range statistics.")
            print(f"Only found {len(last_ten)} records of users setting max stake to 10 tix and staying.")
        
        # Return the valid records count for use in additional stats if needed
        return len(valid_records)


async def get_additional_stats():
    """
    Get some additional statistics about users setting max_stake to 10 tix
    Only counts users who stayed in the draft (are in sign_ups)
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
        
        # Get all instances where users set max_stake to 10 tix and filter for those still in sign_ups
        all_10tix_stmt = select(DraftSession, StakeInfo).join(
            StakeInfo, DraftSession.session_id == StakeInfo.session_id
        ).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.victory_message_id_draft_chat.isnot(None),
                StakeInfo.max_stake == 10
            )
        )
        
        result = await session.execute(all_10tix_stmt)
        all_records = result.fetchall()
        
        # Filter to only include users who are still in the sign_ups dictionary
        valid_records = []
        unique_users = set()
        for draft_session, stake_info in all_records:
            if (draft_session.sign_ups and 
                stake_info.player_id in draft_session.sign_ups):
                valid_records.append((draft_session, stake_info))
                unique_users.add(stake_info.player_id)
        
        total_10tix_max = len(valid_records)
        unique_users_count = len(unique_users)
        
        print(f"\nADDITIONAL STATISTICS:")
        print("=" * 50)
        print(f"Total completed drafts in guild: {total_drafts}")
        print(f"Total instances of users setting max stake to 10 tix (and staying): {total_10tix_max}")
        print(f"Unique users who have set max stake to 10 tix (and stayed): {unique_users_count}")
        if total_drafts > 0:
            print(f"Average 10 tix max stakes per completed draft: {total_10tix_max/total_drafts:.2f}")
        if unique_users_count > 0:
            print(f"Average times each user sets max stake to 10 tix: {total_10tix_max/unique_users_count:.2f}")
        
        # Show some additional insights
        if len(all_records) > total_10tix_max:
            left_draft_count = len(all_records) - total_10tix_max
            print(f"\nUsers who set 10 tix but left before completion: {left_draft_count}")
            print(f"Percentage of 10 tix users who stayed in draft: {(total_10tix_max/len(all_records))*100:.1f}%")


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