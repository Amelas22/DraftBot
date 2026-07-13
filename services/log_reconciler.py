"""Backup retry loop for draft-log capture/publish. Push is primary; this poll
re-fires idempotent steps the push path left pending. Pure-DB actions
(team-pool retry, delayed public embed) live here; the socket capture-retry is
added in reconcile_capture (Task 6)."""
import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from services.draft_log_store import post_team_logs
from services.draft_setup_manager import DraftSetupManager

RECONCILE_INTERVAL_SECONDS: int = 60


async def reconcile_publish_and_team_logs(bot) -> None:
    """Retry pending team-pool posts, and publish the public embed for captured
    drafts whose unlock_at has passed. Both actions are idempotent."""
    # Pending team-pool posts: captured but not yet posted.
    async with db_session() as session:
        pending_team = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.isnot(None),
                DraftSession.team_logs_posted_at.is_(None),
            )
        )).scalars().all()
    for ds in pending_team:
        try:
            await post_team_logs(ds.session_id, bot)
        except Exception as e:
            logger.error(f"[reconciler] team-pool retry failed for {ds.session_id}: {e}")

    # Due public embeds: captured, unlock passed, not yet published.
    now = datetime.now()
    async with db_session() as session:
        due_publish = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.isnot(None),
                DraftSession.data_received == False,   # noqa: E712
                DraftSession.unlock_at.isnot(None),
                DraftSession.unlock_at <= now,
            )
        )).scalars().all()
    for ds in due_publish:
        try:
            manager = DraftSetupManager(
                session_id=ds.session_id, draft_id=ds.draft_id, cube_id=ds.cube,
                guild_id=ds.guild_id,
            )
            manager.session_type = ds.session_type or "team"
            manager.set_bot_instance(bot)
            await manager.publish_draft_log()   # release=False: no socket used
        except Exception as e:
            logger.error(f"[reconciler] publish retry failed for {ds.session_id}: {e}")


async def run_log_reconciler(bot) -> None:
    """Periodic backup loop. Runs forever; each tick is best-effort."""
    logger.info("[reconciler] starting draft-log reconciler loop")
    while True:
        try:
            await reconcile_publish_and_team_logs(bot)
        except Exception as e:
            logger.exception(f"[reconciler] tick failed: {e}")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
