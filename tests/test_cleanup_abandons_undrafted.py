"""Cleanup abandons a draft that never got past drafting, the way /abandon does.

A draft whose rooms were never created was scrapped and not restarted, or
stalled. At its deletion time cleanup removes the draft post (and its Cancel
Draft button), and /abandon finds drafts by their rooms, which this one never
had. Left alone, its entries would sit in the prize pool for good.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from conftest import run_until_sleep, seed_session
from helpers.stale_drafts import abandons_at_deletion
from services import draft_pool_service as pool
from services import wallet_service
from session import get_draft_session

PLAYERS = ["700000000000000001", "700000000000000002"]


def _row(**kw):
    base = dict(session_stage="teams", teams_start_time=datetime(2026, 1, 1),
                rooms_created_at=None, victory_message_id_draft_chat=None,
                victory_message_id_results_channel=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("row, expected", [
    (_row(), True),                                              # scrapped, never restarted
    (_row(rooms_created_at=datetime(2026, 1, 1)), False),         # got to its matches
    (_row(session_stage="abandoned"), False),                     # already done
    (_row(session_stage="completed"), False),
    (_row(victory_message_id_results_channel="1"), False),        # finished
    (_row(session_stage=None, teams_start_time=None), False),     # a queue: expiry's job
    (_row(session_stage=None), False),                            # a queue, whatever else is set
])
def test_which_drafts_cleanup_abandons(row, expected):
    assert abandons_at_deletion(row) is expected


async def _scrapped_staked_draft(sid="s_scrap", rooms=None):
    """A staked draft with teams formed and its pool funded, then left at 'teams'."""
    await seed_session(sid, guild="g", stype="staked", stage=None,
                       teams=(PLAYERS[:1], PLAYERS[1:]), sign_ups={p: p for p in PLAYERS})
    for p in PLAYERS:
        await wallet_service.adjust("g", p, 500, "seed", "test")
        await pool.set_entry("g", sid, p, 20)
    await seed_session(sid, guild="g", stype="staked", stage="teams",
                       teams=(PLAYERS[:1], PLAYERS[1:]), sign_ups={p: p for p in PLAYERS},
                       rooms_created_at=rooms,
                       deletion_time=datetime.now() - timedelta(minutes=1))
    assert await pool.pool_balance("g", sid) == 40


def _bot():
    bot = MagicMock()
    bot.get_channel.return_value = None
    return bot


@pytest.mark.asyncio
async def test_cleanup_abandons_a_scrapped_draft_and_returns_its_entries(test_db):
    import utils

    await _scrapped_staked_draft()
    await run_until_sleep(utils.cleanup_sessions_task(_bot()), 600)

    draft = await get_draft_session("s_scrap")
    assert draft.session_stage == "abandoned"
    assert await pool.pool_balance("g", "s_scrap") == 0
    for p in PLAYERS:
        assert await wallet_service.get_balance("g", p) == 500


@pytest.mark.asyncio
async def test_cleanup_leaves_a_draft_that_reached_its_matches(test_db):
    """A played-but-unreported draft is the table's to settle or /abandon."""
    import utils

    await _scrapped_staked_draft(rooms=datetime.now() - timedelta(hours=5))
    await run_until_sleep(utils.cleanup_sessions_task(_bot()), 600)

    assert (await get_draft_session("s_scrap")).session_stage == "teams"
    assert await pool.pool_balance("g", "s_scrap") == 40


@pytest.mark.asyncio
async def test_cleanup_never_abandons_a_draft_still_drafting(test_db):
    import utils
    from services.draft_setup_manager import DraftSetupManager

    await _scrapped_staked_draft()
    with patch.object(DraftSetupManager, "is_drafting", return_value=True):
        await run_until_sleep(utils.cleanup_sessions_task(_bot()), 600)

    assert (await get_draft_session("s_scrap")).session_stage == "teams"
    assert await pool.pool_balance("g", "s_scrap") == 40
