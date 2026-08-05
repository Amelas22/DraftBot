"""Card lending: multi-entity debt ledger (spec 2026-08-05-card-lending-design)."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.debt_ledger import DebtLedger


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_debt_ledger_rows_carry_optional_card_name(test_db):
    async with AsyncSessionLocal() as session:
        session.add(DebtLedger(
            guild_id="g", player_id="1", counterparty_id="2",
            amount=4, source_type="card_loan", source_id="u1",
            card_name="Lightning Bolt"))
        session.add(DebtLedger(
            guild_id="g", player_id="1", counterparty_id="2",
            amount=-30, source_type="draft", source_id="s1"))
        await session.commit()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(DebtLedger))).scalars().all()
    by_type = {r.source_type: r for r in rows}
    assert by_type["card_loan"].card_name == "Lightning Bolt"
    assert by_type["draft"].card_name is None


from services import debt_service


async def _seed_tix(guild="g"):
    await debt_service.create_ledger_entries(
        guild_id=guild, debtor_id="1", creditor_id="2", amount=30,
        source_type="draft", source_id="sess-1")
    await debt_service.create_ledger_entries(
        guild_id=guild, debtor_id="3", creditor_id="1", amount=12,
        source_type="draft", source_id="sess-2")


async def _tix_surface_snapshot(guild="g"):
    """Outputs of every tix-facing service function we rely on."""
    return {
        "balance_1_2": await debt_service.get_balance_with(guild, "1", "2"),
        "balance_2_1": await debt_service.get_balance_with(guild, "2", "1"),
        "all_for_1": await debt_service.get_all_balances_for(guild, "1"),
        "entries_1_2": [
            (e.amount, e.source_type) for e in
            await debt_service.get_entries_since_last_settlement(guild, "1", "2")],
        "stats": await debt_service.get_guild_debt_stats(guild),
        "history_1": [
            (e.amount, e.source_type) for e in
            (await debt_service.get_debt_history(guild, "1"))],
        "owed_map": await debt_service.get_total_owed_map(guild, ["1", "2", "3"]),
        "creditors": await debt_service.get_top_net_creditors(guild),
        "involved": await debt_service.get_most_involved_players(guild),
        "outstanding": await debt_service.get_most_outstanding_creditors(guild),
    }


@pytest.mark.asyncio
async def test_card_rows_never_touch_tix_outputs(test_db):
    await _seed_tix()
    before = await _tix_surface_snapshot()

    # Assorted card rows, inserted raw so this test predates the loan API.
    async with AsyncSessionLocal() as session:
        for player, counterparty, qty, name, stype in [
            ("1", "2", +4, "Lightning Bolt", "card_loan"),
            ("2", "1", -4, "Lightning Bolt", "card_loan"),
            ("1", "3", -1, "Ragavan", "card_loan"),
            ("3", "1", +1, "Ragavan", "card_loan"),
            ("1", "2", -2, "lightning bolt", "card_return"),
            ("2", "1", +2, "lightning bolt", "card_return"),
        ]:
            session.add(DebtLedger(
                guild_id="g", player_id=player, counterparty_id=counterparty,
                amount=qty, source_type=stype, source_id="u",
                card_name=name))
        await session.commit()

    after = await _tix_surface_snapshot()
    assert after == before


@pytest.mark.asyncio
async def test_debts_history_command_ignores_card_rows(test_db):
    """/debts history's active_only=False branch runs a raw select(DebtLedger)
    directly in cogs/debt_commands.py (not routed through debt_service). It
    needs the same card_name IS NULL guarantee as every service-layer query."""
    from unittest.mock import AsyncMock, MagicMock
    from cogs.debt_commands import DebtCommands

    guild_id, user_id, counterparty_id = "g", "1", "2"

    await debt_service.adjust_debt(
        guild_id=guild_id, player1_id=user_id, player2_id=counterparty_id,
        amount=30, notes="seed debt", created_by="tester")

    def make_ctx():
        ctx = MagicMock()
        ctx.author.id = user_id
        ctx.guild.id = guild_id
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()
        return ctx

    player = MagicMock()
    player.id = counterparty_id
    player.display_name = "Bob"

    cog = DebtCommands(MagicMock())

    ctx_before = make_ctx()
    await cog.debts_history.callback(cog, ctx_before, player, active_only=False)
    embed_before = ctx_before.followup.send.call_args.kwargs["embed"]
    history_before = [f.value for f in embed_before.fields]

    # Card rows between the same pair, inserted raw (predates the loan API).
    async with AsyncSessionLocal() as session:
        for player_id, cp_id, qty, name, stype in [
            (user_id, counterparty_id, +4, "Lightning Bolt", "card_loan"),
            (counterparty_id, user_id, -4, "Lightning Bolt", "card_loan"),
            (user_id, counterparty_id, -2, "lightning bolt", "card_return"),
            (counterparty_id, user_id, +2, "lightning bolt", "card_return"),
        ]:
            session.add(DebtLedger(
                guild_id=guild_id, player_id=player_id, counterparty_id=cp_id,
                amount=qty, source_type=stype, source_id="u", card_name=name))
        await session.commit()

    ctx_after = make_ctx()
    await cog.debts_history.callback(cog, ctx_after, player, active_only=False)
    embed_after = ctx_after.followup.send.call_args.kwargs["embed"]
    history_after = [f.value for f in embed_after.fields]

    assert history_after == history_before


@pytest.mark.asyncio
async def test_create_card_loan_writes_mirrored_pair(test_db):
    lender_row, borrower_row = await debt_service.create_card_loan(
        guild_id="g", lender_id="2", borrower_id="1",
        card_name="  Lightning Bolt ", quantity=4, created_by="2")
    assert lender_row.player_id == "2" and lender_row.amount == 4
    assert borrower_row.player_id == "1" and borrower_row.amount == -4
    assert lender_row.card_name == "Lightning Bolt"      # trimmed
    assert lender_row.source_type == "card_loan"
    assert lender_row.source_id == borrower_row.source_id
    # tix balance between the pair is untouched
    assert await debt_service.get_balance_with("g", "1", "2") == 0


@pytest.mark.asyncio
async def test_create_card_loan_rejects_bad_input(test_db):
    for kwargs in [
        dict(lender_id="1", borrower_id="1", card_name="Bolt", quantity=1),
        dict(lender_id="1", borrower_id="2", card_name="   ", quantity=1),
        dict(lender_id="1", borrower_id="2", card_name="Bolt", quantity=0),
    ]:
        with pytest.raises(ValueError):
            await debt_service.create_card_loan(guild_id="g", **kwargs)


@pytest.mark.asyncio
async def test_open_card_positions_net_case_insensitively(test_db):
    await debt_service.create_card_loan(
        guild_id="g", lender_id="1", borrower_id="2",
        card_name="Lightning Bolt", quantity=4)
    await debt_service.create_card_loan(
        guild_id="g", lender_id="2", borrower_id="1",
        card_name="lightning bolt", quantity=1)   # nets against, newer spelling
    await debt_service.create_card_loan(
        guild_id="g", lender_id="2", borrower_id="1",
        card_name="Ragavan", quantity=1)
    await debt_service.create_card_loan(
        guild_id="g", lender_id="1", borrower_id="3",
        card_name="Brainstorm", quantity=2)

    positions = await debt_service.get_open_card_positions("g", "1")
    as_map = {(p["counterparty_id"], p["card_name"].lower()): p["net"] for p in positions}
    assert as_map == {
        ("2", "lightning bolt"): 3,    # 4 lent - 1 borrowed back
        ("2", "ragavan"): -1,          # player 1 owes it
        ("3", "brainstorm"): 2,
    }
    # display spelling = most recent entry in the group
    bolt = next(p for p in positions if p["card_name"].lower() == "lightning bolt")
    assert bolt["card_name"] == "lightning bolt"

    only_2 = await debt_service.get_open_card_positions("g", "1", counterparty_id="2")
    assert {p["card_name"].lower() for p in only_2} == {"lightning bolt", "ragavan"}


@pytest.mark.asyncio
async def test_fully_returned_position_disappears(test_db):
    await debt_service.create_card_loan(
        guild_id="g", lender_id="1", borrower_id="2", card_name="Bolt", quantity=2)
    await debt_service.create_card_loan(
        guild_id="g", lender_id="2", borrower_id="1", card_name="Bolt", quantity=2)
    assert await debt_service.get_open_card_positions("g", "1") == []
