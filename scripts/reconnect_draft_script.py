# reconnect_drafts_script.py
import asyncio
import discord
from dotenv import load_dotenv
import os
from reconnect_drafts import reconnect_recent_draft_sessions
from loguru import logger

async def main():
    # Load environment variables
    load_dotenv()
    
    # Setup a temporary client just for this operation
    intents = discord.Intents.default()
    intents.message_content = True
    
    bot = discord.Client(intents=intents)
    
    @bot.event
    async def on_ready():
        logger.info(f"Temporary client {bot.user.name} is connected")
        
        try:
            # Get the reconnection tasks
            tasks = await reconnect_recent_draft_sessions(bot)
            
            if tasks:
                # Wait for all tasks to complete
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("All draft session reconnection tasks have completed")
            else:
                logger.info("No draft sessions to reconnect")
                
        except Exception as e:
            logger.error(f"Error in reconnection process: {e}")
        finally:
            # Always close the bot when done
            await bot.close()
    
    # Run the temporary bot
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("No BOT_TOKEN found in .env file")
        return
        
    try:
        await bot.start(token)
    except Exception as e:
        logger.error(f"Error starting temporary bot: {e}")

if __name__ == "__main__":
    asyncio.run(main())