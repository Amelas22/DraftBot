import socketio
import asyncio
import aiohttp
import aiobotocore
import json
import os
from dotenv import load_dotenv
from sqlalchemy.future import select
from aiobotocore.session import get_session
from session import AsyncSessionLocal, DraftSession

load_dotenv()

sio = socketio.AsyncClient()

class DraftLogManager:
    def __init__(self, session_id, draft_link, draft_id, session_type, cube):
        self.session_id = session_id
        self.draft_link = draft_link
        self.draft_id = draft_id
        self.session_type = session_type
        self.cube = cube
        self.delay_handled = False

    async def keep_draft_session_alive(self):
        sio.manager = self  # Store the DraftLogManager instance in the sio client
        while True:
            try:
                await sio.connect(
                    f'wss://draftmancer.com?userID=DraftBot&sessionID=DB{self.draft_id}&userName=DraftBot',
                    transports='websocket',
                    wait_timeout=10)
                
                while True:
                    data_fetched = await self.fetch_draft_log_data()
                    if data_fetched:
                        print(f"Draft log data fetched and saved for {self.draft_id}, closing connection.")
                        await sio.disconnect()
                        return
                    else:
                        print(f"Draft log data not available, retrying in 5 minutes...")
                        await asyncio.sleep(300)  # Retry every 5 minutes

                    try:
                        await sio.emit('ping')  # Send a ping to keep the connection alive
                        await asyncio.sleep(120)  # Send a ping every 2 minutes
                    except socketio.exceptions.ConnectionError:
                        print(f"Connection to {self.draft_link} closed, retrying...")
                        break

            except Exception as e:
                print(f"Error connecting to {self.draft_link}: {e}")
            
            await asyncio.sleep(120)

    async def fetch_draft_log_data(self):
        url = f"https://draftmancer.com/getDraftLog/DB{self.draft_id}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        draft_data = await response.json()
                        if draft_data.get("delayed") is True and not self.delay_handled:
                            print(f"Draft log data for {self.draft_id} is delayed, retrying in 3 hours and 15 minutes...")
                            await asyncio.sleep(11700)  # Wait for 3 hours and 15 minutes
                            self.delay_handled = True  # Set the flag to True after handling the delay
                            return await self.fetch_draft_log_data()  # Retry fetching the data
                        else:
                            await self.save_draft_log_data(draft_data)
                            return True
                    else:
                        print(f"Failed to fetch draft log data: status code {response.status}")
                        return False
            except Exception as e:
                print(f"Exception while fetching draft log data: {e}")
                return False

    async def save_draft_log_data(self, draft_data):    
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                upload_successful = await self.save_to_digitalocean_spaces(draft_data)
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                draft_session = await db_session.scalar(stmt)
                if upload_successful and draft_session:
                    draft_session.data_received = True
                elif draft_session:
                    draft_session.draft_data = draft_data
                    print(f"Draft log data saved in database for {self.draft_id}; SessionID: {self.session_id}")
                else:
                    print(f"Draft session {self.session_id} not found in the database")
            await db_session.commit()

    async def save_to_digitalocean_spaces(self, draft_data):
        DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
        DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
        DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
        DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
        DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
        
        start_time = draft_data.get("time")
        draft_id = draft_data.get("sessionID")
        
        session = get_session()
        async with session.create_client(
            's3',
            region_name=DO_SPACES_REGION,
            endpoint_url=DO_SPACES_ENDPOINT,
            aws_access_key_id=DO_SPACES_KEY,
            aws_secret_access_key=DO_SPACES_SECRET
        ) as s3_client:
            try:
                folder = "swiss" if self.session_type == "swiss" else "team"
                object_name = f'{folder}/{self.cube}-{start_time}-{draft_id}.json'
                await s3_client.put_object(
                    Bucket=DO_SPACES_BUCKET,
                    Key=object_name,
                    Body=json.dumps(draft_data),
                    ContentType='application/json',
                    ACL='public-read'
                )
                print(f"Draft log data uploaded to DigitalOcean Space: {object_name}")
                return True
            except Exception as e:
                print(f"Error uploading to DigitalOcean Space: {e}")
                return False

@sio.event
async def connect():
    manager = sio.manager  # Retrieve the DraftLogManager instance
    print(f"Successfully connected to the websocket for draft_id: DB{manager.draft_id}")

@sio.event
async def connect_error(data):
    manager = sio.manager  # Retrieve the DraftLogManager instance
    print(f"Connection to the websocket failed for draft_id: DB{manager.draft_id}")

@sio.event
async def disconnect():
    manager = sio.manager  # Retrieve the DraftLogManager instance
    print(f"Disconnected from the websocket for draft_id: DB{manager.draft_id}")