"""Draft vouchers in the /wallet panel and its history."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.wallet_tx import WalletTx, VOUCHER
from services import voucher_service, wallet_history as wh, wallet_service
from wallet_history_view import wallet_embed

G, P, Q = "g1", "111", "222"


async def _give_vouchers(to: str, count: int):
    async with db_session() as session:
        await wallet_service.transfer_in(
            session, G, voucher_service.MINT, to, count, f"voucher-mint:{to}", asset=VOUCHER)
        await session.commit()


@pytest.mark.asyncio
async def test_wallet_embed_shows_a_voucher_line_only_for_a_holder(test_db):  # noqa: F811
    await _give_vouchers(P, 2)
    embed, _ = await wallet_embed(G, P, "Pat")
    assert any("🎟️ Draft vouchers: 2" in f.value for f in embed.fields)
    embed, _ = await wallet_embed(G, Q, "Quinn")
    assert not any("Draft vouchers" in f.value for f in embed.fields)


@pytest.mark.asyncio
async def test_history_shows_voucher_rows_labelled_as_vouchers(test_db):  # noqa: F811
    await _give_vouchers(P, 2)
    await voucher_service.give(G, P, Q, 1)
    page = await wh.get_history_page(G, Q)
    lines = await wh.describe_rows(page.rows)
    assert len(lines) == 1
    assert "+1 voucher" in lines[0] and "tix" not in lines[0]
    assert "Voucher gift" in lines[0]


@pytest.mark.asyncio
async def test_history_still_hides_any_other_asset(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(WalletTx(guild_id=G, player_id=P, kind="receive", amount=1,
                             source="s1", asset="other"))
        await session.commit()
    assert (await wh.get_history_page(G, P)).total == 0


def test_voucher_tix_legs_land_in_the_vouchers_category():
    tx = WalletTx(kind="receive", amount=20, source="voucher-fund:abc", asset="tix")
    assert wh.classify(tx).category == wh.VOUCHERS


@pytest.mark.asyncio
async def test_vouchers_filter_selects_only_voucher_rows(test_db):  # noqa: F811
    await _give_vouchers(P, 1)
    page = await wh.get_history_page(G, P, category=wh.VOUCHERS)
    assert page.total == 1
    assert (await wh.get_history_page(G, P, category=wh.TRANSFER)).total == 0
