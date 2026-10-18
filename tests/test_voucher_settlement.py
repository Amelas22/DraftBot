"""A voucher entry's money goes back to the voucher reserve, wherever it comes back.

The 20 tix of a voucher entry travel reserve -> player -> pot, so to the pool it is
an ordinary entry. Every tix the pool later hands that player for it is swept on to
the reserve in the same transaction: a refund in full, a payout of 2m by m. A refund
that empties the player's hold also hands the spent voucher back, and settlement
retires the draft's spent vouchers.

Driven through the real pool functions against a scratch database. Each voucher leg
is checked against the tix system total, and every resting state against the
backing invariant.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from conftest import seed_session, test_db  # noqa: F401  (fixture)
# Registers player_preferences on the metadata before test_db creates the tables;
# settle_draft_winnings reads it.
import preference_service  # noqa: F401
from database.db_session import db_session
from models.draft_session import DraftSession
from models.wallet_tx import TIX, VOUCHER, WalletTx
from services import draft_pool_service as pool
from services import voucher_service as vs
from services import wallet_service

GUILD = "99"
BOT = "bot"
SID = "s1"
# Real snowflakes: settle_inflow skips non-numeric holders as system accounts, which
# would make the debt test pass for the wrong reason.
V = "700000000000000001"     # enters with a voucher
V2 = "700000000000000002"
P = "800000000000000001"     # pays their own tix
P2 = "800000000000000002"


async def _seed(team_a, team_b, stage=None):
    players = list(team_a) + list(team_b)
    await seed_session(SID, guild=GUILD, stype="staked", stage=stage,
                       teams=(team_a, team_b), sign_ups={p: p for p in players})


async def _set_stage(stage):
    async with db_session() as session:
        await session.execute(update(DraftSession)
                              .where(DraftSession.session_id == SID)
                              .values(session_stage=stage))
        await session.commit()


async def _mint(count, to=V):
    await wallet_service.adjust(GUILD, BOT, vs.VOUCHER_VALUE * count, "seed", "t")
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            await vs.mint_in(session, GUILD, BOT, to, count, f"mint:{to}:{count}")
            await session.commit()


async def _fund(player, amount):
    await wallet_service.adjust(GUILD, player, amount, "seed", "t")


async def _bal(holder, asset=TIX):
    async with db_session() as session:
        return await wallet_service.balance_in(session, GUILD, holder, asset=asset)


async def _legs(source, asset=TIX):
    async with db_session() as session:
        return await wallet_service.transfer_legs(session, source, asset=asset)


async def _voucher_sources():
    async with db_session() as session:
        return sorted({row.source for row in (await session.execute(
            select(WalletTx).where(WalletTx.source.like("voucher-%")))).scalars()})


async def _same_total(work):
    """Run one step and pin that it neither created nor destroyed tix."""
    before = await wallet_service.total_wallets(asset=TIX)
    result = await work
    assert await wallet_service.total_wallets(asset=TIX) == before, "a voucher leg moved the tix total"
    return result


async def _enter(player=V):
    async def go():
        with patch("config.vouchers_enabled", return_value=True):
            async with wallet_service.MONEY_LOCK:
                async with db_session() as session:
                    async with session.begin():
                        return await vs.enter_with_voucher_in(session, GUILD, SID, player)
    assert await _same_total(go()) is None


async def _backed():
    backing = await vs.backing(GUILD)
    assert backing.ok, backing
    return backing


async def _with_voucher(player):
    async with db_session() as session:
        return await vs.entered_with_voucher_in(session, GUILD, SID, player)


# --- settlement ------------------------------------------------------------

@pytest.mark.asyncio
async def test_voucher_winner_keeps_the_match_and_the_reserve_gets_its_share_back(test_db):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(P, 20)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await _same_total(pool.match_pool(GUILD, SID, [V], [P]))
    await _set_stage("teams")

    result = await _same_total(pool.settle_pool(GUILD, SID, [V]))

    assert result["paid"] == {V: 20}, "paid must be what the winner KEEPS"
    assert await _bal(V) == 20
    assert await _bal(P) == 0
    assert await _bal(vs.RESERVE) == 20, "the reserve's net over the draft is 0"
    assert await _bal(vs.RETIRED, VOUCHER) == 1
    assert await _bal(vs.spent_holder(SID), VOUCHER) == 0
    assert await _bal(V, VOUCHER) == 0
    backing = await _backed()
    assert (backing.outstanding, backing.free) == (0, 20)
    assert await pool.pool_balance(GUILD, SID) == 0


@pytest.mark.asyncio
async def test_voucher_loser_pays_nothing_and_the_voucher_is_retired(test_db):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(P, 20)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await pool.match_pool(GUILD, SID, [V], [P])
    await _set_stage("teams")
    assert (await vs.backing(GUILD)).outstanding == 1

    result = await _same_total(pool.settle_pool(GUILD, SID, [P]))

    assert result["paid"] == {P: 40}, "an ordinary winner is paid as before"
    assert await _bal(V) == 0
    assert await _bal(P) == 40
    assert await _bal(vs.RESERVE) == 0, "the reserve is 20 lower"
    assert await _bal(vs.RETIRED, VOUCHER) == 1
    backing = await _backed()
    assert (backing.outstanding, backing.free) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("voucher_wins", [False, True])
async def test_partial_levelling_then_loss_and_then_win_keep_the_backing_exact(test_db, voucher_wins):  # noqa: F811
    # 20 + 20 against 10 + 10: side A levels to 10 + 10, refunding 10 of the
    # voucher entry, which goes on to the reserve. The voucher stays spent.
    team_a, team_b = [V, P], [P2, V2]
    await _seed(team_a, team_b)
    await _mint(1)
    for player, amount in ((P, 20), (P2, 10), (V2, 10)):
        await _fund(player, amount)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await pool.set_entry(GUILD, SID, P2, 10)
    await pool.set_entry(GUILD, SID, V2, 10)

    levelled = await _same_total(pool.match_pool(GUILD, SID, team_a, team_b))
    await _set_stage("teams")

    assert levelled["refunded"] == {V: 10, P: 10}
    assert await pool.held_by(GUILD, SID, V) == 10
    assert await _bal(V) == 0, "the refunded share of a voucher entry is not the player's"
    assert await _bal(vs.RESERVE) == 10
    assert await _bal(vs.spent_holder(SID), VOUCHER) == 1, "still entered, so still spent"
    assert await _with_voucher(V)
    assert len(await _legs(f"voucher-sweep:draft-refund:unmatched:{SID}:{V}:1")) == 2
    # In play: the reserve plus the voucher money still at risk in the pot is
    # exactly what the outstanding voucher needs.
    mid = await vs.backing(GUILD)
    assert mid.outstanding == 1
    assert mid.reserve + await pool.held_by(GUILD, SID, V) == vs.VOUCHER_VALUE * mid.outstanding

    result = await _same_total(pool.settle_pool(GUILD, SID, team_a if voucher_wins else team_b))

    if voucher_wins:
        assert result["paid"] == {V: 10, P: 20}
        assert await _bal(V) == 10
        assert await _bal(vs.RESERVE) == 20
    else:
        assert result["paid"] == {P2: 20, V2: 20}
        assert await _bal(V) == 0
        assert await _bal(vs.RESERVE) == 10
    assert await _bal(vs.RETIRED, VOUCHER) == 1
    backing = await _backed()
    assert backing.outstanding == 0
    assert backing.free == (20 if voucher_wins else 10)


@pytest.mark.asyncio
async def test_voucher_winner_with_a_debt_only_their_kept_share_reaches_on_inflow(test_db):  # noqa: F811
    from services import mtgo_resolution_service as resolution
    from services.debt_service import create_ledger_entries, get_balance_with

    await _seed([V], [P])
    await _mint(1)
    await _fund(P, 20)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await pool.match_pool(GUILD, SID, [V], [P])
    await _set_stage("teams")
    await create_ledger_entries(guild_id=GUILD, debtor_id=V, creditor_id=P,
                                amount=30, source_type="draft", source_id="older-draft")

    real_on_inflow = resolution.on_inflow
    seen = []

    async def spy(guild_id, player_id, notifier=None, credited=None, *rest, **kw):
        seen.append((player_id, credited))
        return await real_on_inflow(guild_id, player_id, notifier, credited, *rest, **kw)

    result = await pool.settle_pool(GUILD, SID, [V])
    with patch.object(resolution, "on_inflow", spy), \
         patch("bot_registry.get_bot", return_value=MagicMock()), \
         patch("notification_service.send_dm", AsyncMock(return_value=True)):
        await resolution.settle_draft_winnings(GUILD, result["paid"], "a-draft")

    assert seen == [(V, 20)], "on_inflow must see the kept 20, never the 40 paid out"
    assert await _bal(vs.RESERVE) == 20, "the debt ate the reserve's share"
    assert await get_balance_with(GUILD, V, P) == -10, "the kept 20 went to the debt"
    assert await _bal(V) == 0
    await _backed()


@pytest.mark.asyncio
async def test_settle_twice_retires_once_and_sweeps_once(test_db):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(P, 20)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await pool.match_pool(GUILD, SID, [V], [P])
    await _set_stage("teams")

    first = await pool.settle_pool(GUILD, SID, [V])
    second = await pool.settle_pool(GUILD, SID, [V])
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            again = await vs.retire_in(session, GUILD, SID)
            await session.commit()

    assert first["paid"] == {V: 20} and second["paid"] == {}
    assert again == 0
    assert len(await _legs(f"voucher-sweep:draft-payout:{SID}:{V}")) == 2
    assert len(await _legs(f"voucher-retire:{SID}", VOUCHER)) == 2
    assert await _bal(vs.RETIRED, VOUCHER) == 1
    assert await _bal(V) == 20
    assert await _bal(vs.RESERVE) == 20
    await _backed()


# --- refunds -----------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["left", "removed"])
async def test_leave_returns_the_voucher_and_the_twenty_to_the_reserve(test_db, reason):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(V, 5)
    await _enter(V)

    result = await _same_total(pool.set_entry(GUILD, SID, V, 0, reason))

    assert result["ok"]
    assert await pool.held_by(GUILD, SID, V) == 0
    assert await _bal(V) == 5, "the player's own tix are untouched"
    assert await _bal(vs.RESERVE) == 20
    assert await _bal(V, VOUCHER) == 1, "the voucher comes back"
    assert await _bal(vs.spent_holder(SID), VOUCHER) == 0
    assert not await _with_voucher(V)
    assert len(await _legs(f"voucher-sweep:draft-refund:{reason}:{SID}:{V}:1")) == 2
    assert len(await _legs(f"voucher-return:{SID}:{V}:1", VOUCHER)) == 2
    backing = await _backed()
    assert (backing.outstanding, backing.free) == (1, 0)


@pytest.mark.asyncio
async def test_join_leave_rejoin_with_vouchers_keeps_sources_distinct(test_db):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(V, 30)

    await _enter(V)
    await _same_total(pool.set_entry(GUILD, SID, V, 0, "left"))
    await _enter(V)
    await _same_total(pool.set_entry(GUILD, SID, V, 0, "left"))
    # A third entry with the player's own tix is ordinary: no voucher leg follows it.
    await pool.set_entry(GUILD, SID, V, 30)
    await pool.set_entry(GUILD, SID, V, 0, "left")

    assert await _voucher_sources() == sorted([
        f"voucher-fund:{SID}:{V}:0",
        f"voucher-fund:{SID}:{V}:2",
        f"voucher-spend:{SID}:{V}:0",
        f"voucher-spend:{SID}:{V}:2",
        f"voucher-return:{SID}:{V}:1",
        f"voucher-return:{SID}:{V}:3",
        f"voucher-sweep:draft-refund:left:{SID}:{V}:1",
        f"voucher-sweep:draft-refund:left:{SID}:{V}:3",
    ])
    assert await _bal(V) == 30
    assert await _bal(V, VOUCHER) == 1
    assert await _bal(vs.RESERVE) == 20
    await _backed()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["draw", "cancel"])
async def test_draw_and_cancel_with_delete_draft_return_the_voucher(test_db, how):  # noqa: F811
    await _seed([V], [P])
    await _mint(1)
    await _fund(P, 20)
    await _enter(V)
    await pool.set_entry(GUILD, SID, P, 20)
    await pool.match_pool(GUILD, SID, [V], [P])
    await _set_stage("teams")

    if how == "draw":
        result = await _same_total(pool.settle_draw(GUILD, SID))
    else:
        result = await _same_total(
            pool.release_draft_pool(GUILD, SID, "cancelled", delete_draft=True))
        async with db_session() as session:
            assert (await session.execute(select(DraftSession).where(
                DraftSession.session_id == SID))).scalars().first() is None

    assert result["refunded"] == {V: 20, P: 20}
    assert await _bal(V) == 0
    assert await _bal(P) == 20
    assert await _bal(V, VOUCHER) == 1
    assert await _bal(vs.RESERVE) == 20
    assert await _bal(vs.RETIRED, VOUCHER) == 0
    backing = await _backed()
    assert (backing.outstanding, backing.free) == (1, 0)


# --- no voucher in play ------------------------------------------------------

@pytest.mark.asyncio
async def test_ordinary_entries_are_untouched(test_db):  # noqa: F811
    team_a, team_b = [V, P], [P2, V2]
    await _seed(team_a, team_b)
    for player, amount in ((V, 20), (P, 20), (P2, 10), (V2, 10)):
        await _fund(player, amount)
        await pool.set_entry(GUILD, SID, player, amount)

    levelled = await pool.match_pool(GUILD, SID, team_a, team_b)
    await _set_stage("teams")
    result = await pool.settle_pool(GUILD, SID, team_a)

    assert levelled["refunded"] == {V: 10, P: 10}
    assert result["paid"] == {V: 20, P: 20}
    assert {p: await _bal(p) for p in (V, P, P2, V2)} == {V: 30, P: 30, P2: 0, V2: 0}
    assert await _voucher_sources() == []
    assert await _bal(vs.RESERVE) == 0
