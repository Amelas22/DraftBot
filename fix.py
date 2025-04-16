import asyncio
from sqlalchemy import update
from database.db_session import db_session
from models.draft_session import DraftSession

async def update_match_counter():
    """
    Update the match_counter to 13 for the specified draft session.
    Uses direct SQL update to avoid session persistence issues.
    """
    session_id = "416632328715239434-1744767391"  # The correct session ID
    new_counter = 13     # The corrected match counter value
    
    try:
        # First get the current value to confirm what we're changing
        draft_session = await DraftSession.get_by_session_id(session_id)
        if draft_session:
            print(f"Found draft session: {draft_session}")
            print(f"Current match_counter: {draft_session.match_counter}")
            
            # Use direct SQL update with a new session instead of the update() method
            async with db_session() as session:
                stmt = update(DraftSession).where(
                    DraftSession.session_id == session_id
                ).values(match_counter=new_counter)
                
                result = await session.execute(stmt)
                await session.commit()
                
                if result.rowcount > 0:
                    print(f"Successfully updated match_counter to {new_counter}")
                    
                    # Verify the update with a fresh query
                    updated_session = await DraftSession.get_by_session_id(session_id)
                    print(f"Verified match_counter is now: {updated_session.match_counter}")
                    return True
                else:
                    print(f"No rows updated. Check if session ID exists.")
                    return False
        else:
            print(f"Draft session with ID {session_id} not found.")
            return False
    except Exception as e:
        print(f"Error updating match_counter: {e}")
        return False

async def main():
    success = await update_match_counter()
    if success:
        print("Successfully updated the draft session's match_counter.")
    else:
        print("Failed to update the draft session's match_counter.")

if __name__ == "__main__":
    asyncio.run(main())