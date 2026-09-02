"""Whose wallet you are allowed to read.

A balance and the last ten transactions -- counterparties included -- are the
player's own business. /wallet show took an optional `player`, so anyone could
read anyone's. Looking someone up is a bot-manager action now, and lives in its
own command.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ctx(author_id=111, guild_id=999):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.author.display_name = "Me"
    ctx.guild.id = guild_id
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _member(member_id=222, name="Someone"):
    member = MagicMock()
    member.id = member_id
    member.display_name = name
    return member


def test_wallet_show_cannot_be_pointed_at_another_player():
    from cogs.wallet_cog import WalletCommands

    assert "player" not in [o.name for o in WalletCommands.wallet_show.options]


@pytest.mark.asyncio
async def test_wallet_show_reads_the_callers_own_wallet():
    """The other half of the same rule: with no target to pass, the command has to
    resolve the caller, not some default that could be steered elsewhere."""
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    ctx = _ctx(author_id=111)
    wallet = MagicMock(balance=120)

    get_wallet = AsyncMock(return_value=wallet)
    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.wallet_service.get_wallet", new=get_wallet), \
         patch("cogs.wallet_cog.wallet_service.get_history", new=AsyncMock(return_value=[])):
        await WalletCommands.wallet_show.callback(cog, ctx)

    assert get_wallet.await_args.args[1] == "111"


@pytest.mark.asyncio
async def test_admin_show_renders_the_targets_wallet():
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    ctx = _ctx(author_id=111)
    target = _member(222, "Ada")
    wallet = MagicMock(balance=120)

    get_wallet = AsyncMock(return_value=wallet)
    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.wallet_service.get_wallet", new=get_wallet), \
         patch("cogs.wallet_cog.wallet_service.get_history", new=AsyncMock(return_value=[])):
        await WalletCommands.wallet_admin_show.callback(cog, ctx, target)

    assert get_wallet.await_args.args[1] == "222"
    embed = ctx.followup.send.await_args.kwargs["embed"]
    assert "Ada" in embed.title
    assert "120" in embed.fields[0].value


@pytest.mark.asyncio
async def test_admin_show_records_who_looked():
    """A privileged read of someone else's money should be answerable after the
    fact, without the player having to be told each time."""
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    ctx = _ctx(author_id=111)
    target = _member(222, "Ada")

    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.wallet_service.get_wallet",
               new=AsyncMock(return_value=MagicMock(balance=120))), \
         patch("cogs.wallet_cog.wallet_service.get_history", new=AsyncMock(return_value=[])), \
         patch("cogs.wallet_cog.logger") as log:
        await WalletCommands.wallet_admin_show.callback(cog, ctx, target)

    logged = " ".join(str(c) for c in log.info.call_args_list)
    assert "111" in logged and "222" in logged


def test_admin_show_is_restricted_to_bot_managers():
    """The whole point of moving it out of /wallet show. py-cord records the check
    on the command, so assert it is there rather than simulating a rejection."""
    from cogs.wallet_cog import WalletCommands

    assert WalletCommands.wallet_admin_show.checks, "admin_show carries no permission check"
