import websockets
import asyncio
import aiohttp
from sqlalchemy.future import select
from session import AsyncSessionLocal, DraftSession

async def keep_draft_session_alive(session_id, draft_link, draft_id):

    websocket_url = f"wss://draftmancer.com/socket.io/?session=DB{draft_id}&transport=websocket"
    
    while True:
        try:
            async with websockets.connect(websocket_url) as websocket:
                print(f"Connected to {draft_link}")
                while True:
                    data_fetched = await fetch_draft_log_data(draft_id)
                    if data_fetched:
                        print(f"Draft log data fetched and saved for {draft_id}, closing connection.")
                        await websocket.close()
                        return
                    else:
                        print(f"Draft log data not available, retrying in 5 minutes...")
                        await asyncio.sleep(300)  # Retry every 5 minutes
                    try:
                        await websocket.send('2')  # Send a ping to keep the connection alive
                        await asyncio.sleep(120)  # Send a ping every 2 minutes
                    except websockets.ConnectionClosed:
                        print(f"Connection to {draft_link} closed, retrying...")
                        break
        except Exception as e:
            print(f"Error connecting to {draft_link}: {e}")
        
        await asyncio.sleep(120)

async def fetch_draft_log_data(session_id, draft_id):
    url = f"https://draftmancer.com/getDraftLog/DB{draft_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    draft_data = await response.json()
                    await save_draft_log_data(session_id, draft_id, draft_data)
                    return True
                else:
                    print(f"Failed to fetch draft log data: status code {response.status}")
                    return False
        except Exception as e:
            print(f"Exception while fetching draft log data: {e}")
            return False

async def save_draft_log_data(session_id, draft_id, draft_data):
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(DraftSession).filter(DraftSession.session_id == session_id)
            session = await db_session.scalar(stmt)
            if session:
                session.draft_data = draft_data
                session.data_received = True  
                await db_session.commit()
                print(f"Draft log data saved for {draft_id}")
            else:
                print(f"Draft session {draft_id} not found in the database")
