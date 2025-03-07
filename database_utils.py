import asyncio
from sqlalchemy.exc import OperationalError
from loguru import logger

async def execute_with_retry(db_func, max_attempts=5, retry_delay=0.5):
    """
    Execute a database function with retries for SQLite lock errors.
    
    Parameters:
    - db_func: An async function that performs database operations
    - max_attempts: Maximum number of retry attempts
    - retry_delay: Delay between retries in seconds
    
    Returns:
    - The result of the database function
    """
    attempt = 1
    while attempt <= max_attempts:
        try:
            return await db_func()
        except OperationalError as e:
            if "database is locked" in str(e) and attempt < max_attempts:
                logger.warning(f"Database locked, retrying (attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(retry_delay * attempt)  # Exponential backoff
                attempt += 1
            else:
                logger.error(f"Database error after {attempt} attempts: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error during database operation: {e}")
            raise