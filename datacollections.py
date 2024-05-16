import socketio
import asyncio
import aiohttp
import aiobotocore
import json
import os
from dotenv import load_dotenv
from sqlalchemy.future import select
from session import AsyncSessionLocal, DraftSession

load_dotenv()

DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")

sio = socketio.AsyncClient()

@sio.event
async def connect():
    pass

@sio.event
async def disconnect():
    pass

async def keep_draft_session_alive(session_id, draft_link, draft_id, session_type):
    while True:
        try:
            await sio.connect(
            f'wss://draftmancer.com?userID=DRAFTLOGBOT&sessionID={session_id}&userName=DraftLogBot',
            transports='websocket',
            wait_timeout=10)
            print(f"Connected to {draft_link}")
            
            while True:
                data_fetched = await fetch_draft_log_data(session_id, draft_id, session_type)
                if data_fetched:
                    print(f"Draft log data fetched and saved for {draft_id}, closing connection.")
                    await sio.disconnect()
                    return
                else:
                    print(f"Draft log data not available, retrying in 5 minutes...")
                    await asyncio.sleep(300)  # Retry every 5 minutes

                try:
                    await sio.emit('ping')  # Send a ping to keep the connection alive
                    await asyncio.sleep(120)  # Send a ping every 2 minutes
                except socketio.exceptions.ConnectionError:
                    print(f"Connection to {draft_link} closed, retrying...")
                    break

        except Exception as e:
            print(f"Error connecting to {draft_link}: {e}")
        
        await asyncio.sleep(120)

async def fetch_draft_log_data(session_id, draft_id, session_type):
    url = f"https://draftmancer.com/getDraftLog/DB{draft_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    draft_data = await response.json()
                    await save_draft_log_data(session_id, draft_id, draft_data, session_type)
                    return True
                else:
                    print(f"Failed to fetch draft log data: status code {response.status}")
                    return False
        except Exception as e:
            print(f"Exception while fetching draft log data: {e}")
            return False

async def save_draft_log_data(session_id, draft_id, draft_data, session_type):
    upload_successful = await save_to_digitalocean_spaces(session_id, session_type, draft_data)
    
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(DraftSession).filter(DraftSession.session_id == session_id)
            draft_session = await db_session.scalar(stmt)
            if draft_session:
                if upload_successful:
                    draft_session.data_received = True
                else:
                    draft_session.draft_data = draft_data
                await db_session.commit()
                print(f"Draft log data processed for {draft_id}")
            else:
                print(f"Draft session {draft_id} not found in the database")

async def save_to_digitalocean_spaces(session_id, session_type, draft_data):
    aio_session = aiobotocore.get_session()
    async with aio_session.create_client(
        's3',
        region_name=DO_SPACES_REGION,
        endpoint_url=DO_SPACES_ENDPOINT,
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET
    ) as s3_client:
        try:
            folder = "swiss" if session_type == "swiss" else "team"
            object_name = f'{folder}/{session_id}.json'
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