import asyncio
from sqlalchemy import select, update
from database.db_session import db_session
from models.draft_session import DraftSession

async def update_draft_chat_channel():
    """
    Update the draft_chat_channel for the specified draft session.
    Uses direct SQL update to avoid session persistence issues.
    """
    session_id = "416632328715239434-1744767391"  # The correct session ID
    new_chat_channel_id = "1361901104304099441"  # The correct channel ID
    
    try:
        # First get the current value to confirm what we're changing
        async with db_session() as session:
            query = select(DraftSession).filter_by(session_id=session_id)
            result = await session.execute(query)
            draft_session = result.scalar_one_or_none()
            
            if draft_session:
                print(f"Found draft session: {draft_session}")
                print(f"Current draft_chat_channel: {draft_session.draft_chat_channel}")
                
                # Use direct SQL update
                stmt = update(DraftSession).where(
                    DraftSession.session_id == session_id
                ).values(
                    draft_chat_channel=new_chat_channel_id
                )
                
                result = await session.execute(stmt)
                await session.commit()
                
                if result.rowcount > 0:
                    print(f"Successfully updated draft_chat_channel to {new_chat_channel_id}")
                    
                    # Verify the update with a fresh query
                    verify_query = select(DraftSession).filter_by(session_id=session_id)
                    verify_result = await session.execute(verify_query)
                    updated_session = verify_result.scalar_one_or_none()
                    
                    if updated_session:
                        print(f"Verified draft_chat_channel is now: {updated_session.draft_chat_channel}")
                        return True
                    else:
                        print("Failed to verify update.")
                        return False
                else:
                    print(f"No rows updated. Check if session ID exists.")
                    return False
            else:
                print(f"Draft session with ID {session_id} not found.")
                return False
    except Exception as e:
        print(f"Error updating draft_chat_channel: {e}")
        return False

async def main():
    success = await update_draft_chat_channel()
    if success:
        print("Successfully updated the draft session's draft_chat_channel.")
    else:
        print("Failed to update the draft session's draft_chat_channel.")

if __name__ == "__main__":
    asyncio.run(main())