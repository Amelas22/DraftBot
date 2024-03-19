import asyncio
import discord
from datetime import datetime

sessions = {}

def add_session(session_id, session):
    # Check if the sessions dictionary already contains 20 sessions
    if len(sessions) >= 20:
        # Sort sessions by the timestamp in their ID (assuming session_id format includes a timestamp) and remove the oldest
        oldest_session_id = sorted(sessions.keys(), key=lambda x: int(x.split('-')[-1]))[0]
        oldest_session = sessions.pop(oldest_session_id)
        # Delete associated chat channels if they still exist
        for channel_id in oldest_session.channel_ids:
            channel = session.bot.get_channel(channel_id)
            if channel:  # Check if channel was found and still exists
                asyncio.create_task(channel.delete(reason="Session expired due to session cap."))
                print(f"Deleting channel: {channel.name} for session {oldest_session_id}")

    # Add the new session
    sessions[session_id] = session
    print(f"Added new session: {session_id}")

async def cleanup_sessions_task():
    while True:
        current_time = datetime.now()
        for session_id, session in list(sessions.items()):  
            if current_time >= session.deletion_time:
                # Attempt to delete each channel associated with the session
                for channel_id in session.channel_ids:
                    channel = session.bot.get_channel(channel_id)
                    if channel:  # Check if channel was found
                        try:
                            await channel.delete(reason="Session expired.")
                            print(f"Deleted channel: {channel.name}")
                        except discord.HTTPException as e:
                            print(f"Failed to delete channel: {channel.name}. Reason: {e}")
                
                # Once all associated channels are handled, remove the session from the dictionary
                del sessions[session_id]
                print(f"Session {session_id} has been removed due to time.")

        # run function every hour
        await asyncio.sleep(3600)  # Sleep for 1 hour