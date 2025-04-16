import asyncio
from sqlalchemy import update
from database.db_session import db_session
from models.draft_session import DraftSession

async def update_draft_session():
    """
    Update the match_counter and draft_channel_id for the specified draft session.
    Uses direct SQL update to avoid session persistence issues.
    """
    session_id = "416632328715239434-1744767391"  # The correct session ID
    new_counter = 13     # The corrected match counter value
    new_channel_id = "1361901104304099441"  # The new draft channel ID
    
    try:
        # First get the current values to confirm what we're changing
        draft_session = await DraftSession.get_by_session_id(session_id)
        if draft_session:
            print(f"Found draft session: {draft_session}")
            print(f"Current match_counter: {draft_session.match_counter}")
            print(f"Current draft_channel_id: {draft_session.draft_channel_id}")
            
            # Use direct SQL update with a new session instead of the update() method
            async with db_session() as session:
                stmt = update(DraftSession).where(
                    DraftSession.session_id == session_id
                ).values(
                    match_counter=new_counter,
                    draft_channel_id=new_channel_id
                )
                
                result = await session.execute(stmt)
                await session.commit()
                
                if result.rowcount > 0:
                    print(f"Successfully updated match_counter to {new_counter}")
                    print(f"Successfully updated draft_channel_id to {new_channel_id}")
                    
                    # Verify the update with a fresh query
                    updated_session = await DraftSession.get_by_session_id(session_id)
                    print(f"Verified match_counter is now: {updated_session.match_counter}")
                    print(f"Verified draft_channel_id is now: {updated_session.draft_channel_id}")
                    return True
                else:
                    print(f"No rows updated. Check if session ID exists.")
                    return False
        else:
            print(f"Draft session with ID {session_id} not found.")
            return False
    except Exception as e:
        print(f"Error updating draft session: {e}")
        return False

async def main():
    success = await update_draft_session()
    if success:
        print("Successfully updated the draft session.")
    else:
        print("Failed to update the draft session.")

if __name__ == "__main__":
    asyncio.run(main())