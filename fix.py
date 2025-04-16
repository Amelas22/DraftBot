import asyncio
from sqlalchemy import select, update
from database.db_session import db_session
from models.match import MatchResult  # Adjust import path as needed

async def fix_null_session_ids():
    """
    Find all match results with NULL session_id and update them
    to have the correct session_id value.
    """
    correct_session_id = "416632328715239434-1744767391"
    
    async with db_session() as session:
        # First, find all match results with NULL session_id
        null_query = select(MatchResult).filter(MatchResult.session_id.is_(None))
        result = await session.execute(null_query)
        null_matches = result.scalars().all()
        
        if not null_matches:
            print("No match results found with NULL session_id.")
            return
            
        print(f"Found {len(null_matches)} match results with NULL session_id.")
        
        # Print details of the matches for verification
        for match in null_matches:
            print(f"Match #{match.match_number}, ID: {match.id}, Pairing Message: {match.pairing_message_id}")
        
        # Confirm before proceeding
        print(f"\nWill update these {len(null_matches)} matches to session_id: {correct_session_id}")
        
        # Update the matches
        updated_ids = [match.id for match in null_matches]
        update_stmt = update(MatchResult).where(
            MatchResult.id.in_(updated_ids)
        ).values(
            session_id=correct_session_id
        )
        
        result = await session.execute(update_stmt)
        await session.commit()
        
        print(f"Successfully updated {result.rowcount} match results.")
        
        # Verify the update
        verify_query = select(MatchResult).filter(MatchResult.id.in_(updated_ids))
        verify_result = await session.execute(verify_query)
        verified_matches = verify_result.scalars().all()
        
        all_updated = all(match.session_id == correct_session_id for match in verified_matches)
        if all_updated:
            print("All matches successfully verified with the new session_id.")
        else:
            print("Warning: Some matches may not have been updated correctly.")
            for match in verified_matches:
                if match.session_id != correct_session_id:
                    print(f"Match {match.id} still has session_id: {match.session_id}")

async def main():
    await fix_null_session_ids()

if __name__ == "__main__":
    asyncio.run(main())