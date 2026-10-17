"""The wallet ledger's ``asset`` column and its transfer-leg uniqueness guard."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.wallet_tx import TIX, WalletTx

GUILD = "g1"


def _receive(source, **kw):
    return WalletTx(guild_id=GUILD, player_id="p1", kind="receive", amount=1,
                    source=source, **kw)


@pytest.mark.asyncio
async def test_rows_default_to_tix(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        await session.commit()
    async with db_session() as session:
        row = (await session.execute(select(WalletTx))).scalar_one()
    assert row.asset == TIX == "tix"


@pytest.mark.asyncio
async def test_same_source_and_kind_allowed_across_assets(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        session.add(_receive("s1", asset="other"))
        await session.commit()
    async with db_session() as session:
        rows = (await session.execute(select(WalletTx))).scalars().all()
    assert sorted(r.asset for r in rows) == ["other", "tix"]


@pytest.mark.asyncio
async def test_same_source_kind_and_asset_still_rejected(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        await session.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            session.add(_receive("s1"))
            await session.commit()


# ---------------------------------------------------------------------------
# wallet_service: per-asset reads, transfers and reconciliation
# ---------------------------------------------------------------------------
from services import wallet_service  # noqa: E402

G, P, Q, POT = "g1", "100", "200", "prize:tourney:1"


async def _seed():
    """Q -> P transfers of 6 tix and 9 'other', then P -> POT transfers of 4 tix and 2 'other'."""
    async with db_session() as session:
        await wallet_service.transfer_in(session, G, Q, P, 6, "t-in")
        await wallet_service.transfer_in(session, G, P, POT, 4, "t-out")
        await wallet_service.transfer_in(session, G, Q, P, 9, "o-in", asset="other")
        await wallet_service.transfer_in(session, G, P, POT, 2, "o-out", asset="other")
        await session.commit()


@pytest.mark.asyncio
async def test_tix_reads_ignore_other_assets(test_db):  # noqa: F811
    await _seed()
    # tix: P = +6 -4 = 2, Q = -6, POT = +4
    assert await wallet_service.get_balance(G, P) == 2
    assert (await wallet_service.get_wallet(G, P)).balance == 2
    assert await wallet_service.balances_for(G, [P]) == {P: 2}
    assert await wallet_service.contributions_to(G, POT) == {P: 4}
    assert await wallet_service.total_wallets() == 0
    hist = await wallet_service.get_history(G, P)
    assert len(hist) == 2 and {r.asset for r in hist} == {"tix"}
    async with db_session() as session:
        assert await wallet_service.movements_in(session, G, P, POT) == 1
        assert await wallet_service.net_between(session, G, POT, P) == 4
        assert await wallet_service.contributions_to_in(session, G, POT) == {P: 4}
        assert await wallet_service.balance_in(session, G, P) == 2


@pytest.mark.asyncio
async def test_reads_by_asset(test_db):  # noqa: F811
    await _seed()
    assert await wallet_service.get_balance(G, P, asset="other") == 7
    assert (await wallet_service.get_wallet(G, P, asset="other")).balance == 7
    assert await wallet_service.balances_for(G, [P, Q], asset="other") == {P: 7, Q: -9}
    assert await wallet_service.contributions_to(G, POT, asset="other") == {P: 2}
    hist = await wallet_service.get_history(G, P, asset="other")
    assert len(hist) == 2 and {r.asset for r in hist} == {"other"}
    async with db_session() as session:
        assert await wallet_service.movements_in(session, G, P, POT, asset="other") == 1
        assert await wallet_service.net_between(session, G, POT, P, asset="other") == 2
        assert await wallet_service.contributions_to_in(
            session, G, POT, asset="other") == {P: 2}


@pytest.mark.asyncio
async def test_transfer_in_writes_both_legs_in_its_asset(test_db):  # noqa: F811
    async with db_session() as session:
        debit, credit = await wallet_service.transfer_in(
            session, G, P, Q, 3, "src", asset="other")
        legs_other = await wallet_service.transfer_legs(session, "src", asset="other")
        legs_tix = await wallet_service.transfer_legs(session, "src")
        await session.commit()
    assert debit.asset == credit.asset == "other"
    assert len(legs_other) == 2 and legs_tix == []


@pytest.mark.asyncio
async def test_transfer_credit_is_per_asset(test_db):  # noqa: F811
    async with db_session() as session:
        await wallet_service.transfer_in(session, G, P, Q, 3, "src", asset="other")
        assert await wallet_service.transfer_credit(session, "src") is None
        credit = await wallet_service.transfer_credit(session, "src", asset="other")
    assert credit is not None and credit.amount == 3


@pytest.mark.asyncio
async def test_pay_idempotency_is_per_asset(test_db):  # noqa: F811
    await wallet_service.adjust(G, P, 50, "seed", "t")
    await wallet_service.adjust(G, P, 50, "seed", "t", asset="other")
    t1 = await wallet_service.pay(G, P, Q, 5, source="x")
    o1 = await wallet_service.pay(G, P, Q, 5, source="x", asset="other")
    assert {t1[0].asset, t1[1].asset} == {"tix"}
    assert {o1[0].asset, o1[1].asset} == {"other"}
    assert t1[0].id != o1[0].id
    t2 = await wallet_service.pay(G, P, Q, 5, source="x")
    o2 = await wallet_service.pay(G, P, Q, 5, source="x", asset="other")
    assert (t2[0].id, t2[1].id) == (t1[0].id, t1[1].id)
    assert (o2[0].id, o2[1].id) == (o1[0].id, o1[1].id)
    assert await wallet_service.get_balance(G, P) == 45
    assert await wallet_service.get_balance(G, P, asset="other") == 45


@pytest.mark.asyncio
async def test_pay_funds_check_is_per_asset(test_db):  # noqa: F811
    await wallet_service.adjust(G, P, 50, "seed", "t")
    with pytest.raises(wallet_service.InsufficientFunds):
        await wallet_service.pay(G, P, Q, 1, asset="other")


@pytest.mark.asyncio
async def test_adjust_writes_its_asset(test_db):  # noqa: F811
    tx = await wallet_service.adjust(G, P, 5, "n", "t", asset="other")
    assert tx.asset == "other"
    assert (await wallet_service.adjust(G, P, 5, "n", "t")).asset == wallet_service.TIX


@pytest.mark.asyncio
async def test_boundary_rows_are_tix(test_db):  # noqa: F811
    tx = await wallet_service.credit_done(G, P, 5, job_id="j1")
    assert tx.asset == "tix"


@pytest.mark.asyncio
async def test_reconcile_counts_tix_only(test_db):  # noqa: F811
    await wallet_service.credit_done(G, P, 12, job_id="j1")
    await wallet_service.adjust(G, P, 99, "n", "t", asset="other")
    result = await wallet_service.reconcile(bot_tix=12)
    assert result["ok"] and result["wallet_total"] == 12


# ---------------------------------------------------------------------------
# direct ledger readers: history, donation day, escrow probes, debt settlement
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta  # noqa: E402


async def _add(*rows):
    async with db_session() as session:
        session.add_all(rows)
        await session.commit()


def _tx(player, kind, amount, source, asset, **kw):
    return WalletTx(guild_id=G, player_id=player, kind=kind, amount=amount,
                    source=source, asset=asset, **kw)


@pytest.mark.asyncio
async def test_donation_gather_ignores_other_assets(test_db):  # noqa: F811
    from services import donation_day
    since = datetime.utcnow() - timedelta(days=1)
    await _add(_tx("bot", "receive", 5, "gift-tix", "tix", counterparty_id="300"),
               _tx("bot", "receive", 7, "gift-other", "other", counterparty_id="300"))
    async with db_session() as session:
        credits, _ = await donation_day.gather_credits(session, G, "bot", since)
    assert [c.amount for c in credits] == [5]


@pytest.mark.asyncio
async def test_wallet_history_page_ignores_other_assets(test_db):  # noqa: F811
    from services import wallet_history
    await _add(_tx("p1", "receive", 1, "a", "tix"), _tx("p1", "receive", 2, "b", "tix"),
               _tx("p1", "receive", 3, "c", "other"))
    page = await wallet_history.get_history_page(G, "p1")
    assert page.total == 2
    assert {r.asset for r in page.rows} == {"tix"}


@pytest.mark.asyncio
async def test_escrow_and_payout_probes_ignore_other_assets(test_db):  # noqa: F811
    from services import tournament_escrow_service as escrow
    base = escrow.entry_source(9, 4)
    await _add(_tx("p1", "receive", 1, base, "other"),
               _tx("p1", "receive", 1, "payout:9:1", "other"))
    async with db_session() as session:
        assert await escrow._entry_sources(session, 9, 4) == []
        assert not await escrow._already_paid(session, 9)
    await _add(_tx("p1", "receive", 1, base, "tix"),
               _tx("p1", "receive", 1, "payout:9:1", "tix"))
    async with db_session() as session:
        assert await escrow._entry_sources(session, 9, 4) == [base]
        assert await escrow._already_paid(session, 9)


@pytest.mark.asyncio
async def test_debt_wallet_source_probe_ignores_other_assets(test_db):  # noqa: F811
    from services import mtgo_resolution_service as resolution
    await _add(_tx("p1", "pay", -1, "debt:L1", "other"))
    res = await resolution.settle_debt_from_wallet(G, "p1", "p2", 1, link_id="L1")
    assert not res.get("idempotent")
