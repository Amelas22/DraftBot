"""Winning a draft's prize pool tells the winner about it.

Under the old debt regime a win booked a debt, and debts are loud -- they show
on the /debts board and the settle flow DMs when they are paid. The pool regime
credits the wallet instantly and silently, so the one moment a player most wants
to hear from the bot became the one moment it says nothing to them directly.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service
from utils import settle_decided_draft

GUILD = "g"
SID = "s1"
FRIENDLY = "dragon-fodder-26"
A = ["a1", "a2", "a3", "a4"]
B = ["b1", "b2", "b3", "b4"]
STAKE = 50


@pytest.fixture(autouse=True)
def _a_registered_bot():
    """notify_wallet fetches the bot from bot_registry and no-ops without one."""
    with patch("bot_registry.get_bot", return_value=MagicMock()):
        yield


@pytest.fixture
def sent():
    """The DM boundary, patched; the returned mock holds the calls it received."""
    dm = AsyncMock(return_value=True)
    with patch("notification_service.send_dm", dm):
        yield dm


def _dmed(sent):
    """(player_id, message) per DM, in the order they were sent."""
    return {call.args[1]: call.args[2] for call in sent.await_args_list}


@pytest_asyncio.fixture
async def _a_decided_staked_draft(test_db):
    """A staked draft that team A has won, its pool funded and matched.

    Every player stakes the same, so the arithmetic stays out of the way: each
    side holds 200, and a winner holding 50 takes 100 -- the one figure the DM
    reports.

    Seeded twice on purpose. entry_in refuses to move money once the queue has
    closed, so the entries have to be charged while the draft is still at
    stage=None; the second call closes the book with the matches and the
    match_counter that make it a decided draft.
    """
    await seed_session(SID, guild=GUILD, stype="staked", stage=None,
                       teams=(A, B), sign_ups={p: p for p in A + B})
    for player in A + B:
        await wallet_service.adjust(GUILD, player, 1000, "seed", "test")
        await pool.set_entry(GUILD, SID, player, STAKE)
    await pool.match_pool(GUILD, SID, A, B)

    await seed_session(SID, guild=GUILD, stype="staked", stage="completed",
                       teams=(A, B), sign_ups={p: p for p in A + B},
                       matches=[(A[i], B[i], A[i], None) for i in range(3)],
                       match_counter=4, friendly_id=FRIENDLY)


@pytest.mark.asyncio
async def test_each_winner_is_told_what_they_won(_a_decided_staked_draft, sent):
    await settle_decided_draft(SID)

    told = _dmed(sent)
    # Set equality, so this also pins that no LOSER was DMed -- the Bet Outcomes
    # embed already tells them, and a DM would be bad news arriving twice.
    assert set(told) == set(A), f"DMed {sorted(told)}, expected the winners {A}"
    for message in told.values():
        # The amount that actually landed in the wallet, named as the prize for
        # the event -- not itemised into stake-back and winnings.
        assert "100" in message, message
        assert FRIENDLY in message, message
        assert "stake back" not in message, message


@pytest.mark.asyncio
async def test_a_player_who_turned_dms_off_is_not_told(_a_decided_staked_draft, sent):
    """Winning does not re-subscribe someone who opted out of draft DMs."""
    from preference_service import update_player_dm_notification_preference
    await update_player_dm_notification_preference("a2", GUILD, False)

    await settle_decided_draft(SID)

    assert set(_dmed(sent)) == {"a1", "a3", "a4"}


@pytest.mark.asyncio
async def test_a_failing_dm_does_not_cost_anyone_their_winnings(_a_decided_staked_draft):
    """The tix have already moved by the time we try to talk about them.

    Asserts the money, not merely that no exception escaped: a settlement that
    rolled back would also "not raise" if the DM were sent inside it.
    """
    before = await wallet_service.get_balance(GUILD, "a1")
    with patch("notification_service.send_dm",
               new=AsyncMock(side_effect=RuntimeError("Discord is down"))):
        await settle_decided_draft(SID)

    assert await pool.pool_balance(GUILD, SID) == 0, "pool did not settle"
    assert await wallet_service.get_balance(GUILD, "a1") == before + STAKE * 2, \
        "winner was not paid"


@pytest.mark.asyncio
async def test_a_failing_preference_lookup_does_not_break_the_victory_path(
        _a_decided_staked_draft):
    """The guards on the notifier do not cover the work done to reach it.

    The preference lookup and the balance read happen before any @_best_effort
    notifier is entered, and check_and_post_victory_or_draw awaits this -- so an
    exception here would take the victory post down with it, after the money had
    already moved.
    """
    with patch("notification_service.get_players_dm_notification_preferences",
               new=AsyncMock(side_effect=RuntimeError("db hiccup"))):
        await settle_decided_draft(SID)

    assert await pool.pool_balance(GUILD, SID) == 0, "pool did not settle"


@pytest.mark.asyncio
async def test_replaying_the_settlement_does_not_dm_twice(_a_decided_staked_draft, sent):
    """settle_decided_draft runs on EVERY match report, so this is the ordinary
    case, not an edge one. The pool being empty afterwards is what stops it."""
    await settle_decided_draft(SID)
    after_first = sent.await_count
    await settle_decided_draft(SID)
    await settle_decided_draft(SID)

    assert after_first == len(A), f"first settlement told {after_first}, expected {len(A)}"
    assert sent.await_count == after_first, (
        f"replays sent {sent.await_count - after_first} extra DM(s)")
