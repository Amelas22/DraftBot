"""/escrow open and /escrow mine: what a player is told, and what reaches the service."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cogs.escrow_cog import EscrowCommands, describe_escrow
from models.escrow_hold import OPEN, RELEASED, EscrowHold
from services import escrow_service
from services.wallet_service import InsufficientFunds

# The commands are called unbound, so the cog never needs its __init__ to run.
COG = EscrowCommands.__new__(EscrowCommands)
ME, THEM = 111, 222


def _ctx():
    ctx = MagicMock()
    ctx.author.id = ME
    ctx.guild.id = 999
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _member(member_id=THEM, *, bot=False):
    member = MagicMock()
    member.id = member_id
    member.bot = bot
    member.display_name = "Ada"
    return member


def _hold(hold_id=7, *, status=OPEN, amount=60, note="tix sale", funder=ME, counterparty=THEM):
    return EscrowHold(id=hold_id, guild_id="999", funder_id=str(funder),
                      counterparty_id=str(counterparty), amount=amount, note=note,
                      status=status, created_at=datetime(2026, 11, 1))


def _sent(ctx):
    return ctx.followup.send.await_args


def _open(ctx, player, amount=60, note="tix sale", *, gate=None, refusal=None,
          opened=None, balance=40):
    """Run /escrow open with the gate, link check, service and balance patched.

    Returns the open_escrow mock so a test can assert whether, and how, it was called.
    """
    open_escrow = AsyncMock(return_value=opened or _hold(), side_effect=None)

    async def run():
        with patch("cogs.escrow_cog.gate_read", return_value=gate), \
             patch("cogs.escrow_cog.unlinked_refusal", new=AsyncMock(return_value=refusal)), \
             patch("cogs.escrow_cog.escrow_service.open_escrow", new=open_escrow), \
             patch("cogs.escrow_cog.wallet_service.get_balance",
                   new=AsyncMock(return_value=balance)):
            await EscrowCommands.escrow_open.callback(COG, ctx, player, amount, note)

    return open_escrow, run


@pytest.mark.asyncio
async def test_open_relays_the_gate_refusal_and_never_reaches_the_service():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(), gate="The tix wallet isn't enabled on this server.")
    await run()
    assert _sent(ctx).args[0] == "The tix wallet isn't enabled on this server."
    open_escrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_refuses_an_escrow_with_yourself():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(ME))
    await run()
    assert _sent(ctx).args[0] == "You can't open an escrow with yourself."
    open_escrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_refuses_a_bot():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(bot=True))
    await run()
    assert "bot" in _sent(ctx).args[0].lower()
    open_escrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["funder", "counterparty"])
async def test_open_relays_the_unlinked_refusal(who):
    refusal = ("Link your MTGO account first with `/link_mtgo`." if who == "funder"
               else "Ada hasn't linked an MTGO account yet.")
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(), refusal=refusal)
    await run()
    assert _sent(ctx).args[0] == refusal
    open_escrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_short_of_funds_shows_have_and_need():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(), amount=60)
    open_escrow.side_effect = InsufficientFunds("111", 60, 25)
    await run()
    msg = _sent(ctx).args[0]
    assert "You have **25 tix** — holding **60** needs **35** more." in msg


@pytest.mark.asyncio
async def test_open_success_calls_the_service_and_names_the_escrow():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(), opened=_hold(7), balance=40)
    await run()
    open_escrow.assert_awaited_once_with("999", "111", "222", 60, "tix sale")
    assert _sent(ctx).args[0] == (
        "Escrow **#7** opened: **60 tix** held for <@222>. "
        "Only a moderator can release or return them. Your balance: **40 tix**.")
    assert _sent(ctx).kwargs["ephemeral"] is True
    assert _sent(ctx).kwargs["allowed_mentions"].users is False


@pytest.mark.asyncio
async def test_open_reports_a_note_the_service_rejects_instead_of_raising():
    ctx = _ctx()
    open_escrow, run = _open(ctx, _member(), note="   ")
    open_escrow.side_effect = ValueError("An escrow needs a note saying what it is for")
    await run()
    assert _sent(ctx).args[0] == "An escrow needs a note saying what it is for"
    assert _sent(ctx).kwargs["ephemeral"] is True


def test_the_note_option_is_capped_at_the_service_limit():
    options = {o.name: o for o in EscrowCommands.escrow_open.options}
    assert options["note"].max_length == escrow_service.NOTE_MAX
    assert options["note"].required is True
    assert options["amount"].min_value == 1


def test_describe_escrow_is_one_line_with_the_parties_status_and_note():
    assert describe_escrow(_hold(7, status=RELEASED)) == (
        "#7 · 60 tix · <@111> → <@222> · released · tix sale")


def test_describe_escrow_neutralises_markdown_and_mentions_in_the_note():
    line = describe_escrow(_hold(note="**bold** @everyone <@333333333333333333>"))
    assert "**bold**" not in line
    assert "@everyone" not in line and "<@333333333333333333>" not in line


async def _mine(ctx, holds):
    with patch("cogs.escrow_cog.gate_read", return_value=None), \
         patch("cogs.escrow_cog.escrow_service.escrows_for",
               new=AsyncMock(return_value=holds)) as escrows_for:
        await EscrowCommands.escrow_mine.callback(COG, ctx)
    return escrows_for


@pytest.mark.asyncio
async def test_mine_with_nothing_says_so():
    ctx = _ctx()
    escrows_for = await _mine(ctx, [])
    escrows_for.assert_awaited_once_with("999", "111")
    assert _sent(ctx).args[0] == "You have no escrows on this server."
    assert _sent(ctx).kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_mine_lists_an_open_and_a_decided_escrow_open_first():
    ctx = _ctx()
    open_hold, decided = _hold(7), _hold(3, status=RELEASED, note="old deal")
    await _mine(ctx, [open_hold, decided])
    msg = _sent(ctx).args[0]
    assert describe_escrow(open_hold) in msg and describe_escrow(decided) in msg
    assert msg.index("#7") < msg.index("#3")
    assert _sent(ctx).kwargs["allowed_mentions"].users is False


@pytest.mark.asyncio
async def test_mine_relays_the_gate_refusal():
    ctx = _ctx()
    with patch("cogs.escrow_cog.gate_read", return_value="nope"), \
         patch("cogs.escrow_cog.escrow_service.escrows_for", new=AsyncMock()) as escrows_for:
        await EscrowCommands.escrow_mine.callback(COG, ctx)
    assert _sent(ctx).args[0] == "nope"
    escrows_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_mine_stays_within_the_message_limit_and_counts_what_it_cut():
    ctx = _ctx()
    holds = [_hold(i, note="n" * 200) for i in range(1, 31)]
    await _mine(ctx, holds)
    msg = _sent(ctx).args[0]
    assert len(msg) <= 2000
    shown = sum(1 for h in holds if describe_escrow(h) in msg)
    assert 0 < shown < 30
    assert msg.splitlines()[-1] == f"…and {30 - shown} more."
    assert describe_escrow(holds[0]) in msg  # the oldest open ones are kept
