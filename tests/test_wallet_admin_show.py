"""Whose wallet you are allowed to read.

The last ten transactions -- counterparties included -- are the player's own
business, and they are not derivable anywhere else in the bot. /wallet show took
an optional `player`, so anyone could read anyone's. Looking someone up is a
bot-manager action now, and lives in its own command.
"""
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import embed_field
from cogs.wallet_cog import WalletCommands
from helpers.permissions import is_bot_manager

# The commands are called unbound, so the cog never needs its __init__ to run.
COG = WalletCommands.__new__(WalletCommands)
ME, THEM = 111, 222


def _ctx():
    ctx = MagicMock()
    ctx.author.id = ME
    ctx.author.display_name = "Me"
    ctx.guild.id = 999
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _member(member_id=THEM, name="Ada"):
    member = MagicMock()
    member.id = member_id
    member.display_name = name
    return member


@contextlib.contextmanager
def _patched_wallet(balance=120, history=()):
    """Gate open, a wallet with `balance`, and `history`.

    Yields the get_wallet mock so a test can assert WHOSE wallet was read --
    the question this whole change turns on.
    """
    get_wallet = AsyncMock(return_value=MagicMock(balance=balance))
    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.wallet_service.get_wallet", new=get_wallet), \
         patch("cogs.wallet_cog.wallet_service.get_history",
               new=AsyncMock(return_value=list(history))):
        yield get_wallet


def _embed(ctx):
    return ctx.followup.send.await_args.kwargs["embed"]


def test_wallet_show_cannot_be_pointed_at_another_player():
    assert "player" not in [o.name for o in WalletCommands.wallet_show.options]


@pytest.mark.asyncio
async def test_wallet_show_reads_the_callers_own_wallet():
    """The other half of the same rule: with no target to pass, the command has
    to resolve the caller, not some default that could be steered elsewhere."""
    ctx = _ctx()
    with _patched_wallet() as get_wallet:
        await WalletCommands.wallet_show.callback(COG, ctx)

    assert get_wallet.await_args.args[1] == str(ME)


@pytest.mark.asyncio
async def test_admin_show_renders_the_targets_wallet():
    ctx = _ctx()
    with _patched_wallet() as get_wallet:
        await WalletCommands.wallet_admin_show.callback(COG, ctx, _member())

    assert get_wallet.await_args.args[1] == str(THEM)
    assert "Ada" in _embed(ctx).title
    assert "120" in embed_field(_embed(ctx), "Balance").value


@pytest.mark.asyncio
async def test_admin_show_records_who_looked():
    """A privileged read of someone else's money should be answerable after the
    fact, without the player having to be told each time."""
    ctx = _ctx()
    with _patched_wallet(), patch("cogs.wallet_cog.logger") as log:
        await WalletCommands.wallet_admin_show.callback(COG, ctx, _member())

    logged = " ".join(str(c) for c in log.info.call_args_list)
    assert str(ME) in logged and str(THEM) in logged


def test_admin_show_is_restricted_to_bot_managers():
    """The whole point of moving it out of /wallet show.

    Asserts the bot-manager check specifically, not merely that SOME check is
    attached -- a cooldown would satisfy that and prove nothing.
    """
    assert is_bot_manager in WalletCommands.wallet_admin_show.checks
