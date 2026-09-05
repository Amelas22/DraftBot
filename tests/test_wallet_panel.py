"""What the wallet panel actually shows.

Goes through the real cog against a real (throwaway) ledger, so the panel is
pinned end to end rather than against a mocked describe_rows.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.wallet_cog import WalletCommands
from conftest import embed_field, seed_session, test_db  # noqa: F401  (fixtures)
from services import wallet_service as ws

COG = WalletCommands.__new__(WalletCommands)
GUILD, PLAYER = "999", "111"


def _ctx():
    ctx = MagicMock()
    ctx.author.id = int(PLAYER)
    ctx.author.display_name = "Me"
    ctx.guild.id = int(GUILD)
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


async def _show(ctx):
    with patch("cogs.wallet_cog.gate_read", return_value=None):
        await WalletCommands.wallet_show.callback(COG, ctx)
    return ctx.followup.send.await_args.kwargs["embed"]


@pytest.mark.asyncio
async def test_the_panel_says_what_each_entry_was_for(test_db):  # noqa: F811
    await seed_session(session_id="sid1", friendly_id="worthy-knight-72")
    await ws.credit_done(GUILD, PLAYER, 50, job_id="j1")
    await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 10,
                 source="draft-entry:sid1:111:0:0-10", notes="Draft entry 10 (sid1)")

    embed = await _show(_ctx())
    activity = embed_field(embed, "Recent activity").value

    assert "Entry fee · worthy-knight-72" in activity
    assert "Deposit from MTGO" in activity
    assert "sid1" not in activity  # no raw session ids in front of a player


@pytest.mark.asyncio
async def test_an_empty_wallet_still_renders(test_db):  # noqa: F811
    embed = await _show(_ctx())
    assert embed_field(embed, "Recent activity") is None
    assert embed.footer.text == "No wallet activity yet."
