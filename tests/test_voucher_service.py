"""Draft vouchers: the ledger core that backs each voucher with 20 tix."""
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

import config
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.wallet_tx import TIX, VOUCHER, WalletTx
from services import voucher_service as vs
from services import wallet_service

GUILD = "g1"
BOT = "bot"
ABER = "aber"


async def _seed(holder, amount, asset=TIX):
    await wallet_service.adjust(GUILD, holder, amount, "seed", "test", asset=asset)


async def _bal(holder, asset=TIX):
    async with db_session() as session:
        return await wallet_service.balance_in(session, GUILD, holder, asset=asset)


async def _mint(count, source="mint:1"):
    async with db_session() as session:
        out = await vs.mint_in(session, GUILD, BOT, ABER, count, source)
        await session.commit()
    return out


async def _row_count():
    async with db_session() as session:
        return (await session.execute(select(func.count()).select_from(WalletTx))).scalar_one()


@pytest.mark.asyncio
async def test_mint_moves_twenty_tix_per_voucher_from_the_bot_into_the_reserve(test_db):  # noqa: F811
    await _seed(BOT, 200)
    await _mint(10)
    assert await _bal(BOT) == 0
    assert await _bal(vs.RESERVE) == 200
    assert await vs.vouchers_held(GUILD, ABER) == 10
    assert await vs.backing(GUILD) == vs.Backing(10, 200, 0, True)


@pytest.mark.asyncio
async def test_mint_leaves_the_tix_system_total_unchanged(test_db):  # noqa: F811
    await _seed(BOT, 200)
    before = await wallet_service.total_wallets()
    await _mint(10)
    assert await wallet_service.total_wallets() == before


@pytest.mark.asyncio
async def test_mint_draws_free_reserve_before_the_bot(test_db):  # noqa: F811
    await _seed(BOT, 200)
    await _seed(vs.RESERVE, 40)
    assert await _mint(3) == {"from_free": 40, "from_bot": 20}
    assert await _bal(BOT) == 180


@pytest.mark.asyncio
async def test_mint_refuses_when_the_bot_cannot_cover_it(test_db):  # noqa: F811
    await _seed(BOT, 19)
    before = await _row_count()
    with pytest.raises(wallet_service.InsufficientFunds):
        await _mint(1)
    assert await _row_count() == before


@pytest.mark.asyncio
async def test_mint_replayed_with_the_same_source_books_nothing_twice(test_db):  # noqa: F811
    await _seed(BOT, 200)
    await _mint(2)
    after_first = await _row_count()
    assert await _mint(2) == {"from_free": 0, "from_bot": 0}
    assert await _row_count() == after_first
    assert await vs.vouchers_held(GUILD, ABER) == 2


@pytest.mark.asyncio
async def test_return_free_sends_only_the_unbacked_reserve_back(test_db):  # noqa: F811
    await _seed(BOT, 40)
    await _mint(2)
    await _seed(vs.RESERVE, 20)
    async with db_session() as session:
        assert await vs.return_free_in(session, GUILD, BOT, "ret:1") == 20
        assert await vs.return_free_in(session, GUILD, BOT, "ret:2") == 0
        await session.commit()
    assert await _bal(vs.RESERVE) == 40
    assert await _bal(BOT) == 20


@pytest.mark.asyncio
async def test_give_moves_vouchers_and_refuses_more_than_held(test_db):  # noqa: F811
    await _seed(BOT, 60)
    await _mint(3)
    await vs.give(GUILD, ABER, "friend", 2)
    assert await vs.vouchers_held(GUILD, ABER) == 1
    assert await vs.vouchers_held(GUILD, "friend") == 2
    with pytest.raises(wallet_service.InsufficientFunds):
        await vs.give(GUILD, ABER, "friend", 2)
    with pytest.raises(ValueError):
        await vs.give(GUILD, ABER, "friend", 0)


def test_vouchers_enabled_defaults_off_and_reads_the_flag():
    with patch.object(config, "get_config", return_value={"features": {}}):
        assert config.vouchers_enabled(GUILD) is False
    with patch.object(config, "get_config", return_value={}):
        assert config.vouchers_enabled(GUILD) is False
    with patch.object(config, "get_config", return_value={"features": {"vouchers": True}}):
        assert config.vouchers_enabled(GUILD) is True
