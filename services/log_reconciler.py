"""Backup retry loop for draft-log capture/publish. Push is primary; this poll
re-fires idempotent steps the push path left pending. Pure-DB actions
(team-pool retry, deck assignment, delayed public embed) live here; the socket
capture-retry is added in reconcile_capture (Task 6)."""
import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from services.draft_log_store import post_team_logs
from services.draft_deck_assignment import assign_drafted_decks
from services.draft_setup_manager import ACTIVE_MANAGERS, DraftSetupManager

RECONCILE_INTERVAL_SECONDS: int = 60
# How long after its teams form a draft is still worth rejoining for its log.
#
# The log exists nowhere but the Draftmancer room (see
# draft_setup_manager.must_preserve_draft_room), and a rejoin re-delivers it for
# as long as the room exists. Draftmancer deletes a room only after its LAST USER
# has left -- and the bot is not connected as its owner -- and then keeps it
# about 28 minutes more (10, plus an extra the unlock timer adds;
# removeUserFromSession in its server.ts). So those 28 minutes run from whenever
# the room empties, not from the end of the draft: a player who leaves the tab
# open keeps the log retrievable for as long as they stay.
#
# Measured over 764 captures in the 120 days to 2026-10-05: teams forming to log
# captured is 20.4 minutes median and never exceeded 31.9. Past that, only a
# room someone kept open can still yield a log -- which is the case this window
# exists for: a bot that restarted mid-draft, missed endDraft, and must rejoin.
#
# Three hours, matching the unlock timer: generous, because a rejoin that can
# still work is the only copy of the log. The cost of a draft that never yields
# one is what keeps it cheap, and it has two shapes. A bot REFUSED by the room
# (someone else owns it) stood down and was rebuilt every minute -- the
# 2026-10-05 loop -- which reconciler-stops-after-standdown ends. A room that
# was deleted refuses nobody: the rejoin gets an empty room and one idle manager,
# reused on every later tick and bounded by MANAGER_MAX_LIFETIME_MINUTES.
CAPTURE_RETRY_WINDOW_MINUTES: int = 180

# How long a draft stays eligible to have its decks handed out. Its own
# constant, not the team-post window it happens to equal today: that one is
# justified by how long a teammate might still want a pool posted, and retuning
# it should not silently change how long a missed deck assignment can be
# recovered. 3 days covers a bot that was down over a weekend.
DECK_ASSIGN_RETRY_WINDOW_HOURS: int = 72

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
    re-delivers on join, for as long as the room can still exist (see
    CAPTURE_RETRY_WINDOW_MINUTES)."""
    cutoff = datetime.now() - timedelta(minutes=CAPTURE_RETRY_WINDOW_MINUTES)
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
    """Three idempotent passes over captured drafts: retry pending team-pool
    posts, hand each drafter their pool as a borrowable deck, and publish the
    public embed for drafts whose unlock_at has passed."""
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

    # Decks to borrow: its OWN sweep, not a passenger on the one above. That one
    # selects only drafts whose pools have NOT posted, which is the opposite of
    # the case that needs retrying here -- pools post on the first try for
    # almost every draft, so riding along would mean a draft that missed the
    # endDraft push (the bot was restarting, the log landed late) never got
    # another chance. Scoped by "a log was captured recently" and nothing else,
    # because that is the only precondition for handing out a deck; it is not
    # bounded by session type either, since a pool exists in every draft format
    # whether or not the format has team channels to post it to.
    #
    # Re-running is idempotent, and assign_drafted_decks is ordered so that a
    # draft it has already finished costs two small queries and never touches
    # the draft log -- which matters here, because this selects every draft in
    # the window on every tick and a captured log is hundreds of KB of JSON.
    # NOTE: card_loans.source is not indexed, so the "already done" lookup is a
    # table scan. Fine at this scale; index it before card_loans gets large.
    deck_cutoff: datetime = datetime.now() - timedelta(hours=DECK_ASSIGN_RETRY_WINDOW_HOURS)
    async with db_session() as session:
        recently_captured = (await session.execute(
            select(DraftSession).filter(
                DraftSession.logs_captured_at.isnot(None),
                DraftSession.logs_captured_at >= deck_cutoff,
            )
        )).scalars().all()
    for ds in recently_captured:
        try:
            await assign_drafted_decks(ds.session_id)
        except Exception:
            logger.opt(exception=True).error(
                f"[reconciler] deck assignment failed for {ds.session_id}; it may "
                f"have assigned some drafters and not others")

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
                    session_id=ds.session_id, draft_id=ds.draft_id, friendly_id=ds.friendly_id,
                    cube_id=ds.cube, guild_id=ds.guild_id,
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
