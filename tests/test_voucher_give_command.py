"""/wallet give-voucher: moving a draft voucher between members."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.wallet_tx import WalletTx
from services import voucher_service as vs
from services import wallet_service

GUILD = "999"
BOT = "bot"
GIVER = 111
TAKER = 222


def _ctx(author_id=GIVER):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.guild.id = int(GUILD)
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _member(pid=TAKER, bot=False):
    m = MagicMock()
    m.id = pid
    m.bot = bot
    m.display_name = "Taker"
    return m


async def _give_voucher_to_giver(count):
    await wallet_service.adjust(GUILD, BOT, 20 * count, "seed", "test")
    async with db_session() as session:
        await vs.mint_in(session, GUILD, BOT, str(GIVER), count, "mint:1")
        await session.commit()


async def _rows():
    async with db_session() as session:
        return (await session.execute(select(func.count()).select_from(WalletTx))).scalar_one()


async def _run(ctx, player, count=1, enabled=True, notify=None):
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    notify = notify or AsyncMock()
    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.vouchers_enabled", return_value=enabled), \
         patch("cogs.wallet_cog.notify_wallet", new=notify):
        await WalletCommands.wallet_give_voucher.callback(cog, ctx, player, count)
    return notify


def _said(ctx):
    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_give_voucher_moves_it_and_dms_the_recipient(test_db):  # noqa: F811
    from notification_service import notify_voucher_received

    await _give_voucher_to_giver(3)
    ctx = _ctx()
    notify = await _run(ctx, _member(), count=2)

    assert await vs.vouchers_held(GUILD, str(GIVER)) == 1
    assert await vs.vouchers_held(GUILD, str(TAKER)) == 2
    assert "Gave **2 draft vouchers** to <@222>." in _said(ctx)
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    notify.assert_awaited_once_with(
        notify_voucher_received, GUILD, str(TAKER), str(GIVER), 2)


@pytest.mark.asyncio
async def test_voucher_dm_reads_as_specified_and_pluralises():
    from notification_service import notify_voucher_received

    bot = MagicMock()
    with patch("notification_service.send_dm", new=AsyncMock()) as send, \
         patch("notification_service._wallet_name", return_value="Giver"):
        await notify_voucher_received(bot, GUILD, "222", "111", 1)
        await notify_voucher_received(bot, GUILD, "222", "111", 2)
    one, two = (c.args[2] for c in send.await_args_list)
    assert one == ("🎟️ You received **1 draft voucher** from Giver. Use one at sign-up "
                   "for a staked draft: a 20 tix entry, paid by DraftBot.")
    assert "**2 draft vouchers**" in two


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["self", "bot", "too_many"])
async def test_give_voucher_refuses_self_bots_and_more_than_held(test_db, case):  # noqa: F811
    await _give_voucher_to_giver(1)
    before = await _rows()
    ctx = _ctx()
    player = {"self": _member(GIVER), "bot": _member(bot=True),
              "too_many": _member()}[case]
    notify = await _run(ctx, player, count=2 if case == "too_many" else 1)

    expected = {
        "self": "You can't give a voucher to yourself.",
        "bot": "Vouchers can only go to members, not bots.",
        "too_many": "You have 1 draft voucher; you can't give 2.",
    }[case]
    assert _said(ctx) == expected
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    assert await _rows() == before
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_give_voucher_is_refused_where_vouchers_are_off(test_db):  # noqa: F811
    await _give_voucher_to_giver(1)
    before = await _rows()
    ctx = _ctx()
    notify = await _run(ctx, _member(), enabled=False)

    assert _said(ctx) == "Draft vouchers aren't available on this server."
    assert await _rows() == before
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_give_voucher_needs_no_mtgo_link(test_db):  # noqa: F811
    await _give_voucher_to_giver(1)
    ctx = _ctx()
    with patch("cogs.wallet_cog.MtgoAccount.usernames_for_discord_ids",
               new=AsyncMock(return_value={})) as links:
        await _run(ctx, _member())
    links.assert_not_awaited()
    assert await vs.vouchers_held(GUILD, str(TAKER)) == 1


def test_only_bot_managers_can_give_a_voucher():
    # Players can spend a voucher on an entry, but vouchers don't change hands
    # between players.
    from cogs.wallet_cog import WalletCommands
    from helpers.permissions import is_bot_manager

    assert is_bot_manager in WalletCommands.wallet_give_voucher.checks
