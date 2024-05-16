import asyncio
import aiohttp
from sqlalchemy import select
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from session import AsyncSessionLocal, DraftSession

async def keep_draft_session_alive(session_id, draft_link, draft_id):
    while True:
        try:
            # Set up the Chrome driver
            service = ChromeService(ChromeDriverManager().install())
            options = webdriver.ChromeOptions()
            options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--remote-debugging-port=9222')

            # Open the browser
            driver = webdriver.Chrome(service=service, options=options)
            driver.get(draft_link)

            try:
                # Wait for the user input element to be present
                user_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "user-name"))
                )
                user_input.clear()
                user_input.send_keys("DraftBot")
                user_input.send_keys(Keys.RETURN)

                await asyncio.sleep(3 * 3600)  # 3 hours
                
                while True:
                    data_fetched = await fetch_draft_log_data(session_id, draft_id)
                    count = 1
                    if data_fetched:
                        print(f"Draft log data fetched and saved for {draft_id} after {count} attempts, closing connection.")
                        driver.quit()
                        return
                    else:
                        print(f"Draft log data for {draft_id} not available, attempt #{count}")
                        count += 1
                        await asyncio.sleep(300)  # Retry every 5 minutes

            except Exception as e:
                print(f"Exception occurred while interacting with the browser: {e}")
            finally:
                driver.quit()

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
            else:
                print(f"Draft session {draft_id} not found in the database")
