"""A draft's public log posts once the result is decided, not three hours later.

The log is held back so neither team can study the other's pools while the
result is open. A clinch closes it, so release_decided_draft_log makes the log
due and the log reconciler -- the one place that posts it -- picks it up on its
next tick. These tests run that real reconciler query, so they prove the log
would post, not only that a column moved.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from conftest import seed_session
from models.draft_session import DraftSession
from services.log_reconciler import reconcile_publish_and_team_logs
from session import AsyncSessionLocal
from utils import release_decided_draft_log

SID = "s1"
A = ["a1", "a2", "a3"]
B = ["b1", "b2", "b3"]
NINE_MATCHES = 10   # match_counter names the NEXT match


async def _a_captured_draft(*, a_wins, b_wins, stype="premade", data_received=False):
    """A premade with its log captured and locked for three more hours."""
    matches = ([(A[i % 3], B[i % 3], A[i % 3], None) for i in range(a_wins)]
               + [(A[i % 3], B[i % 3], B[i % 3], None) for i in range(b_wins)])
    await seed_session(SID, guild="1", stype=stype, stage="pairings", teams=(A, B),
                       matches=matches, match_counter=NINE_MATCHES)
    now = datetime.now()
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID).values(
            logs_captured_at=now, unlock_at=now + timedelta(hours=3),
            team_logs_posted_at=now, data_received=data_received))
        await s.commit()


async def _unlock_at():
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(DraftSession.unlock_at)
                                .where(DraftSession.session_id == SID))).scalar_one()


async def _reconciler_publishes() -> bool:
    """Run one real reconciler tick; True if it published this draft's log."""
    mgr = MagicMock(publish_draft_log=AsyncMock(), set_bot_instance=MagicMock())
    MgrCls = MagicMock(return_value=mgr)
    MgrCls.get_active_manager = MagicMock(return_value=None)
    with patch("services.log_reconciler.post_team_logs", AsyncMock()), \
         patch("services.log_reconciler.DraftSetupManager", MgrCls):
        await reconcile_publish_and_team_logs(MagicMock())
    return mgr.publish_draft_log.await_count == 1


@pytest.mark.asyncio
async def test_a_clinched_draft_posts_its_log_on_the_next_tick(test_db):
    await _a_captured_draft(a_wins=5, b_wins=1)
    assert not await _reconciler_publishes()     # still locked before the clinch is seen

    await release_decided_draft_log(SID)

    assert await _reconciler_publishes()


@pytest.mark.asyncio
async def test_the_report_that_decides_the_draft_releases_its_log(test_db):
    """Every match report runs check_and_post_victory_or_draw. The release sits
    ahead of its guild lookup, so an unresolvable guild still reaches it."""
    from utils import check_and_post_victory_or_draw
    await _a_captured_draft(a_wins=5, b_wins=1)

    await check_and_post_victory_or_draw(MagicMock(get_guild=lambda _id: None), SID)

    assert await _reconciler_publishes()


@pytest.mark.asyncio
async def test_a_draft_still_in_play_keeps_its_log_locked(test_db):
    await _a_captured_draft(a_wins=4, b_wins=4)  # 4-4 of nine: one match still owed
    locked_until = await _unlock_at()

    await release_decided_draft_log(SID)

    assert await _unlock_at() == locked_until
    assert not await _reconciler_publishes()


@pytest.mark.asyncio
async def test_a_swiss_draft_keeps_its_timer(test_db):
    await _a_captured_draft(a_wins=5, b_wins=0, stype="swiss")
    locked_until = await _unlock_at()

    await release_decided_draft_log(SID)

    assert await _unlock_at() == locked_until


@pytest.mark.asyncio
async def test_an_already_published_log_is_left_alone(test_db):
    await _a_captured_draft(a_wins=5, b_wins=0, data_received=True)
    locked_until = await _unlock_at()

    await release_decided_draft_log(SID)

    assert await _unlock_at() == locked_until


@pytest.mark.asyncio
async def test_a_draft_whose_log_is_not_captured_yet_is_left_for_capture(test_db):
    """No unlock_at means no log yet; capture sets the timer when it lands."""
    await seed_session(SID, stype="premade", stage="pairings", teams=(A, B),
                       matches=[(A[i % 3], B[i % 3], A[i % 3], None) for i in range(5)],
                       match_counter=NINE_MATCHES)

    await release_decided_draft_log(SID)

    assert await _unlock_at() is None


def _a_live_manager():
    """The draft's manager, still connected: its socket is the only way to unlock
    the log in Draftmancer, and it disconnects within seconds of unlock_at."""
    manager = MagicMock(release_draftmancer_log=AsyncMock())
    return manager, patch("services.draft_setup_manager.DraftSetupManager.get_active_manager",
                          side_effect=lambda sid: manager if sid == SID else None)


@pytest.mark.asyncio
async def test_the_clinch_unlocks_draftmancer_through_the_live_manager(test_db):
    await _a_captured_draft(a_wins=5, b_wins=1)
    manager, registry = _a_live_manager()

    with registry:
        await release_decided_draft_log(SID)

    manager.release_draftmancer_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_undecided_draft_leaves_draftmancer_locked(test_db):
    await _a_captured_draft(a_wins=4, b_wins=4)
    manager, registry = _a_live_manager()

    with registry:
        await release_decided_draft_log(SID)

    manager.release_draftmancer_log.assert_not_awaited()
