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
# Real snowflakes, not toy ids. wallet_service.is_system_account treats any
# non-numeric holder as synthetic, and settle_inflow skips those -- so toy ids
# silently opt every winner out of debt settlement.
A = [f"70000000000000000{i}" for i in range(4)]
B = [f"80000000000000000{i}" for i in range(4)]
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


def _dms(sent):
    """(player_id, message) for every DM sent, in order.

    A list, not a dict keyed by player: a winner can receive two DMs from one
    settlement -- their prize, and then the auto-settlement notice if it drew
    against a debt -- and keying by player silently drops the first.
    """
    return [(call.args[1], call.args[2]) for call in sent.await_args_list]


def _winners_told(sent):
    """Just the players who got the WINNINGS DM.

    Distinguished from the auto-settlement DM, which is a different notifier
    with a different policy: it reports tix leaving your wallet on someone
    else's initiative, so it ignores the dm_notifications opt-out.
    """
    return {pid for pid, message in _dms(sent) if "prize for winning" in message}


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

    told = _winners_told(sent)
    # Set equality, so this also pins that no LOSER was DMed -- the Bet Outcomes
    # embed already tells them, and a DM would be bad news arriving twice.
    assert told == set(A), f"DMed {sorted(told)}, expected the winners {A}"
    for _pid, message in _dms(sent):
        if "prize for winning" not in message:
            continue
        # The amount that actually landed in the wallet, named as the prize for
        # the event -- not itemised into stake-back and winnings.
        assert "100" in message, message
        assert FRIENDLY in message, message
        assert "stake back" not in message, message


@pytest.mark.asyncio
async def test_a_player_who_turned_dms_off_is_not_told(_a_decided_staked_draft, sent):
    """Winning does not re-subscribe someone who opted out of draft DMs."""
    from preference_service import update_player_dm_notification_preference
    await update_player_dm_notification_preference(A[1], GUILD, False)

    await settle_decided_draft(SID)

    assert _winners_told(sent) == {A[0], A[2], A[3]}


@pytest.mark.asyncio
async def test_winnings_draw_against_what_the_winner_owes(_a_decided_staked_draft, sent):
    """A prize is an inflow, so it settles debts on the way in.

    Otherwise a debtor collects their winnings and can withdraw them straight
    back out while their creditor is still owed -- the exact case settle_inflow
    names as its reason for existing. The tournament prize payout already does
    this (tournament_escrow_service.execute_payout); a draft payout is the same
    shape and must not be the one that doesn't.
    """
    from services.debt_service import create_ledger_entries, get_balance_with

    await create_ledger_entries(guild_id=GUILD, debtor_id=A[0], creditor_id=B[0],
                                amount=30, source_type="draft", source_id="older-draft")
    assert await get_balance_with(GUILD, A[0], B[0]) == -30

    await settle_decided_draft(SID)

    assert await get_balance_with(GUILD, A[0], B[0]) == 0, (
        "the winner was paid but their debt was never drawn against it")


@pytest.mark.asyncio
async def test_an_opted_out_winner_still_has_their_debts_settled(_a_decided_staked_draft, sent):
    """The opt-out silences the DM, not the money.

    Settlement and notification share a loop, so it is one `continue` away from
    letting a notification preference decide whether someone's winnings pay
    their creditors.
    """
    from preference_service import update_player_dm_notification_preference
    from services.debt_service import create_ledger_entries, get_balance_with

    await update_player_dm_notification_preference(A[0], GUILD, False)
    await create_ledger_entries(guild_id=GUILD, debtor_id=A[0], creditor_id=B[0],
                                amount=30, source_type="draft", source_id="older-draft")

    await settle_decided_draft(SID)

    assert A[0] not in _winners_told(sent), "opted-out winner got the winnings DM"
    assert await get_balance_with(GUILD, A[0], B[0]) == 0, (
        "opting out of DMs also stopped the debt being settled")
    # They are still told the money LEFT, though -- a different notifier, and
    # one the opt-out deliberately does not gate.
    assert any(pid == A[0] and "settle what you owed" in m for pid, m in _dms(sent)), \
        "the auto-settlement notice was suppressed along with the winnings DM"


@pytest.mark.asyncio
async def test_a_failed_preference_lookup_still_settles_the_winners_debts(
        _a_decided_staked_draft, sent):
    """Deciding whether to DM is optional work. Sweeping the debt is not.

    The prefs read happens before any on_inflow call, so if it throws, a
    swallowed failure leaves the winner credited and their creditor unpaid --
    with no replay path, because settle_decided_draft skips the pool branch
    for good once the holder is empty. That is the exact case this whole
    change exists to close, reachable through its own error handling.
    """
    from services.debt_service import create_ledger_entries, get_balance_with

    await create_ledger_entries(guild_id=GUILD, debtor_id=A[0], creditor_id=B[0],
                                amount=30, source_type="draft", source_id="older-draft")

    with patch("services.mtgo_resolution_service"
               ".get_players_dm_notification_preferences",
               side_effect=RuntimeError("preference lookup is down")):
        await settle_decided_draft(SID)

    assert await get_balance_with(GUILD, A[0], B[0]) == 0, (
        "a failed preference lookup cost the creditor their settlement")


@pytest.mark.asyncio
async def test_a_failing_dm_does_not_cost_anyone_their_winnings(_a_decided_staked_draft):
    """The tix have already moved by the time we try to talk about them.

    Asserts the money, not merely that no exception escaped: a settlement that
    rolled back would also "not raise" if the DM were sent inside it.
    """
    before = await wallet_service.get_balance(GUILD, A[0])
    with patch("notification_service.send_dm",
               new=AsyncMock(side_effect=RuntimeError("Discord is down"))):
        await settle_decided_draft(SID)

    assert await pool.pool_balance(GUILD, SID) == 0, "pool did not settle"
    assert await wallet_service.get_balance(GUILD, A[0]) == before + STAKE * 2, \
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
    after_first = len(_dms(sent))
    await settle_decided_draft(SID)
    await settle_decided_draft(SID)

    assert _winners_told(sent) == set(A)
    assert len(_dms(sent)) == after_first, (
        f"replays sent {len(_dms(sent)) - after_first} extra DM(s)")
