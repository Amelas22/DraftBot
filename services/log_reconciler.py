"""Backup retry loop for draft-log capture/publish. Push is primary; this poll
re-fires idempotent steps the push path left pending. Pure-DB actions
(team-pool retry, delayed public embed) live here; the socket capture-retry is
added in reconcile_capture (Task 6)."""
import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from services.draft_log_store import post_team_logs
from services.draft_setup_manager import ACTIVE_MANAGERS, DraftSetupManager

RECONCILE_INTERVAL_SECONDS: int = 60
CAPTURE_RETRY_WINDOW_HOURS: int = 12   # only chase recently-active drafts
TEAM_POST_RETRY_WINDOW_HOURS: int = 72  # 3 days: long enough to recover a real
# post failure (bot/Discord down; league matches span days and a sub may need
# a teammate's pool a day or two later) while still bounded so pre-existing/
# historical captured rows aren't swept and re-posted forever.
PUBLISH_RETRY_WINDOW_HOURS: int = 72  # 3 days: same rationale as
# TEAM_POST_RETRY_WINDOW_HOURS above. publish_draft_log only stamps
# data_received on a real send, so a guild with no draft-logs channel (or a
# persistent send failure) would otherwise leave data_received False forever,
# causing the row to be re-selected -- and a transient manager rebuilt -- on
# every tick indefinitely.
CAPTURE_LOG_WAIT_ATTEMPTS: int = 20    # ~10s waiting for the join-delivered log
CAPTURE_LOG_WAIT_INTERVAL: float = 0.5

_RECONCILER_RUNNING: bool = False  # guards against on_ready firing on every gateway reconnect


async def reconcile_capture(bot) -> None:
    """Backup for a missed endDraft push: reconnect the owner socket for
    uncaptured, recently-active drafts and capture the log the session
    re-delivers on join. Bounded by Draftmancer's ~28-min retention."""
    cutoff = datetime.now() - timedelta(hours=CAPTURE_RETRY_WINDOW_HOURS)
    async with db_session() as session:
        uncaptured = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.is_(None),
                DraftSession.session_stage.in_(["teams", "pairings"]),
                DraftSession.teams_start_time.isnot(None),
                DraftSession.teams_start_time >= cutoff,
                DraftSession.session_type != "winston",
            )
        )).scalars().all()

    for ds in uncaptured:
        session_id = ds.session_id
        try:
            manager = await DraftSetupManager.spawn_for_existing_session(session_id, bot)
            if manager is None:
                continue
            for _ in range(CAPTURE_LOG_WAIT_ATTEMPTS):
                if getattr(manager, "current_draft_log", None):
                    break
                await asyncio.sleep(CAPTURE_LOG_WAIT_INTERVAL)
            draft_log = getattr(manager, "current_draft_log", None)
            if draft_log:
                await manager.capture_draft_log(draft_log)
            else:
                logger.info(f"[reconciler] no log yet for {session_id}; will retry next tick")
        except Exception as e:
            logger.error(f"[reconciler] capture retry failed for {session_id}: {e}")


async def reconcile_publish_and_team_logs(bot) -> None:
    """Retry pending team-pool posts, and publish the public embed for captured
    drafts whose unlock_at has passed. Both actions are idempotent."""
    # Pending team-pool posts: captured but not yet posted. Bounded to recently
    # captured drafts of session types that actually have Red-Team/Blue-Team
    # channels -- otherwise every historical captured draft (team_logs_posted_at
    # is a new column, NULL on all pre-existing rows) and every swiss/winston
    # draft (which structurally can't be team-posted) would be re-selected on
    # every tick forever.
    team_post_cutoff: datetime = datetime.now() - timedelta(hours=TEAM_POST_RETRY_WINDOW_HOURS)
    async with db_session() as session:
        pending_team = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.isnot(None),
                DraftSession.team_logs_posted_at.is_(None),
                DraftSession.logs_captured_at >= team_post_cutoff,
                DraftSession.session_type.notin_(["winston", "swiss"]),
            )
        )).scalars().all()
    for ds in pending_team:
        try:
            await post_team_logs(ds.session_id, bot)
        except Exception as e:
            logger.error(f"[reconciler] team-pool retry failed for {ds.session_id}: {e}")

    # Due public embeds: captured, unlock passed, not yet published. Bounded
    # by publish_retry_cutoff so a draft that can never publish (e.g. no
    # draft-logs channel in the guild) ages out instead of being re-selected
    # -- and rebuilding a transient manager -- forever.
    now = datetime.now()
    publish_retry_cutoff: datetime = now - timedelta(hours=PUBLISH_RETRY_WINDOW_HOURS)
    async with db_session() as session:
        due_publish = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.isnot(None),
                DraftSession.logs_captured_at >= publish_retry_cutoff,
                DraftSession.data_received == False,   # noqa: E712
                DraftSession.unlock_at.isnot(None),
                DraftSession.unlock_at <= now,
            )
        )).scalars().all()
    for ds in due_publish:
        try:
            # Prefer an already-active manager (real state, idempotent publish)
            # over constructing a transient one that would leak into / clobber
            # the module-global ACTIVE_MANAGERS registry.
            manager = DraftSetupManager.get_active_manager(ds.session_id)
            created_transient = False
            if manager is None:
                manager = DraftSetupManager(
                    session_id=ds.session_id, draft_id=ds.draft_id, cube_id=ds.cube,
                    guild_id=ds.guild_id,
                )
                # Only the transient path needs session_type set from the DB row; a reused
                # active manager already carries its own (authoritative) session_type.
                manager.session_type = ds.session_type or "team"
                created_transient = True
            manager.set_bot_instance(bot)
            try:
                await manager.publish_draft_log()   # release=False: no socket used
            finally:
                if created_transient and ACTIVE_MANAGERS.get(ds.session_id) is manager:
                    del ACTIVE_MANAGERS[ds.session_id]
        except Exception as e:
            logger.error(f"[reconciler] publish retry failed for {ds.session_id}: {e}")


async def run_log_reconciler(bot) -> None:
    """Periodic backup loop. Runs forever; each tick is best-effort.

    Discord fires on_ready on every gateway reconnect, and bot.py's on_ready
    starts this loop -- guard against a second concurrent loop (which would
    cause duplicate team-pool posts / duplicate public embeds)."""
    global _RECONCILER_RUNNING
    if _RECONCILER_RUNNING:
        logger.info("[reconciler] reconciler already running; skipping duplicate start")
        return
    _RECONCILER_RUNNING = True
    logger.info("[reconciler] starting draft-log reconciler loop")
    while True:
        try:
            await reconcile_capture(bot)
            await reconcile_publish_and_team_logs(bot)
        except Exception as e:
            logger.exception(f"[reconciler] tick failed: {e}")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
