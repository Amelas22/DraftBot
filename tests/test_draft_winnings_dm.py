"""Winning a draft's prize pool tells the winner about it.

Under the old debt regime a win booked a debt, and debts are loud -- they show
on the /debts board and the settle flow DMs when they are paid. The pool regime
credits the wallet instantly and silently, so the one moment a player most wants
to hear from the bot became the one moment it says nothing to them directly.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service
from session import AsyncSessionLocal, DraftSession
from utils import settle_decided_draft

GUILD = "g"
SID = "s1"
FRIENDLY = "dragon-fodder-26"
A = ["a1", "a2", "a3", "a4"]
B = ["b1", "b2", "b3", "b4"]
STAKE = 50


@pytest_asyncio.fixture
async def _a_decided_staked_draft(test_db):
    """A staked draft that team A has won, its pool funded and matched.

    Every player stakes the same, so the arithmetic stays out of the way: each
    side holds 200, and a winner holding 50 takes 100 -- 50 of it their own
    stake coming back, 50 of it winnings. Those are the two numbers the DM has
    to keep straight.
    """
    await seed_session(SID, guild=GUILD, stype="staked", stage=None,
                       teams=(A, B), sign_ups={p: p for p in A + B})
    for player in A + B:
        await wallet_service.adjust(GUILD, player, 1000, "seed", "test")
        await pool.set_entry(GUILD, SID, player, STAKE)
    await pool.match_pool(GUILD, SID, A, B)

    # Team A wins three of four; seed_session leaves match_counter at 1, which
    # reads as a draft with no matches and would settle nothing.
    await seed_session(SID, guild=GUILD, stype="staked", stage="completed",
                       teams=(A, B), sign_ups={p: p for p in A + B},
                       matches=[(A[i], B[i], A[i], None) for i in range(3)])
    async with AsyncSessionLocal() as db:
        await db.execute(update(DraftSession)
                         .where(DraftSession.session_id == SID)
                         .values(match_counter=4, friendly_id=FRIENDLY))
        await db.commit()


def _capture_dms():
    """Patch the DM boundary and hand back the calls it received."""
    sent = AsyncMock(return_value=True)
    return sent, patch("notification_service.send_dm", sent)


@pytest.mark.asyncio
async def test_each_winner_is_told_what_they_won(_a_decided_staked_draft):
    sent, dm_patch = _capture_dms()
    with dm_patch, patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)

    told = {call.args[1]: call.args[2] for call in sent.await_args_list}
    assert set(told) == set(A), f"DMed {sorted(told)}, expected the winners {A}"
    for message in told.values():
        # The amount that actually landed in the wallet, named as the prize for
        # the event. One figure, not a breakdown of stake-back vs winnings --
        # the player is being told what they got, not shown a receipt.
        assert "100" in message, message
        assert FRIENDLY in message, message
        assert "stake back" not in message, (
            f"the DM still itemises entry vs winnings: {message}")


@pytest.mark.asyncio
async def test_losers_are_not_dmed(_a_decided_staked_draft):
    """The embed already tells them; a DM would only be bad news arriving twice."""
    sent, dm_patch = _capture_dms()
    with dm_patch, patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)

    dmed = {call.args[1] for call in sent.await_args_list}
    assert not (dmed & set(B)), f"losers were DMed: {sorted(dmed & set(B))}"


@pytest.mark.asyncio
async def test_a_player_who_turned_dms_off_is_not_told(_a_decided_staked_draft):
    """Winning does not re-subscribe someone who opted out of draft DMs.

    They still see it: the Bet Outcomes embed goes to the channel regardless.
    """
    from preference_service import update_player_dm_notification_preference
    await update_player_dm_notification_preference("a2", GUILD, False)

    sent, dm_patch = _capture_dms()
    with dm_patch, patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)

    dmed = {call.args[1] for call in sent.await_args_list}
    assert "a2" not in dmed, "opted-out winner was DMed anyway"
    assert dmed == {"a1", "a3", "a4"}, f"wrong winners told: {sorted(dmed)}"


@pytest.mark.asyncio
async def test_a_failing_dm_does_not_cost_anyone_their_winnings(_a_decided_staked_draft):
    """The tix have already moved by the time we try to talk about them.

    Asserts the money, not merely that no exception escaped: a settlement that
    rolled back would also "not raise" if the DM were sent inside it.
    """
    before = await wallet_service.get_balance(GUILD, "a1")
    sent = AsyncMock(side_effect=RuntimeError("Discord is down"))
    with patch("notification_service.send_dm", sent), \
         patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)

    assert await pool.pool_balance(GUILD, SID) == 0, "pool did not settle"
    assert await wallet_service.get_balance(GUILD, "a1") == before + STAKE * 2, \
        "winner was not paid"


@pytest.mark.asyncio
async def test_a_failing_preference_lookup_does_not_break_the_victory_path(
        _a_decided_staked_draft):
    """The DM guards cover the notifier, not the work done to reach it.

    The preference lookup and the balance read happen BEFORE any @_best_effort
    notifier is entered, and settle_decided_draft is awaited by
    check_and_post_victory_or_draw -- so an exception here would take the
    victory post down with it, after the money had already moved.
    """
    with patch("notification_service.get_players_dm_notification_preferences",
               new=AsyncMock(side_effect=RuntimeError("db hiccup"))), \
         patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)

    assert await pool.pool_balance(GUILD, SID) == 0, "pool did not settle"


@pytest.mark.asyncio
async def test_replaying_the_settlement_does_not_dm_twice(_a_decided_staked_draft):
    """settle_decided_draft runs on EVERY match report, so this is the ordinary
    case, not an edge one. The pool being empty afterwards is what stops it."""
    sent, dm_patch = _capture_dms()
    with dm_patch, patch("bot_registry.get_bot", return_value=MagicMock()):
        await settle_decided_draft(SID)
        after_first = sent.await_count
        await settle_decided_draft(SID)
        await settle_decided_draft(SID)

    assert after_first == len(A), f"first settlement told {after_first}, expected {len(A)}"
    assert sent.await_count == after_first, (
        f"replays sent {sent.await_count - after_first} extra DM(s)")
