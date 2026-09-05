"""What the wallet panel actually shows.

Goes through the real cog against a real (throwaway) ledger, so the panel is
pinned end to end rather than against a mocked describe_rows.
"""
from types import SimpleNamespace
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


import wallet_history_view as whv


@pytest.mark.asyncio
async def test_the_footer_counts_the_pages_and_the_entries(test_db):  # noqa: F811
    for i in range(23):
        await ws.credit_done(GUILD, PLAYER, 1, job_id=f"j{i}")
    embed, _ = await whv.wallet_embed(GUILD, PLAYER, "Me")
    assert embed.footer.text == "Page 1 of 3 · 23 entries"


@pytest.mark.asyncio
async def test_prev_is_dead_on_the_first_page_and_next_on_the_last(test_db):  # noqa: F811
    for i in range(12):
        await ws.credit_done(GUILD, PLAYER, 1, job_id=f"j{i}")

    _, first = await whv.wallet_embed(GUILD, PLAYER, "Me", page=0)
    assert first.prev_button.disabled and not first.next_button.disabled

    _, last = await whv.wallet_embed(GUILD, PLAYER, "Me", page=1)
    assert not last.prev_button.disabled and last.next_button.disabled


@pytest.mark.asyncio
async def test_a_single_page_has_no_live_buttons(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 1, job_id="j0")
    _, view = await whv.wallet_embed(GUILD, PLAYER, "Me")
    assert view.prev_button.disabled and view.next_button.disabled


@pytest.mark.asyncio
async def test_the_panel_expires_instead_of_living_forever(test_db):  # noqa: F811
    """The view is not persistent, and py-cord only reaps a view that has
    finished -- so an unbounded timeout keeps every panel ever opened, and its
    items, registered for the life of the process."""
    await ws.credit_done(GUILD, PLAYER, 1, job_id="j0")
    _, view = await whv.wallet_embed(GUILD, PLAYER, "Me")
    assert view.timeout == 600


def test_the_page_is_recoverable_from_the_footer():
    """Wallet panels are ephemeral: they can only be edited through the
    originating interaction token, so the button reads its position from the
    message rather than from instance state."""
    assert whv.page_from_footer("Page 3 of 4 · 38 entries") == 2
    assert whv.page_from_footer("something else entirely") == 0
    assert whv.page_from_footer(None) == 0


@pytest.mark.asyncio
async def test_the_panel_ships_with_its_buttons(test_db):  # noqa: F811
    for i in range(12):
        await ws.credit_done(GUILD, PLAYER, 1, job_id=f"j{i}")
    ctx = _ctx()
    await _show(ctx)
    sent = ctx.followup.send.await_args.kwargs
    assert isinstance(sent["view"], whv.WalletHistoryView)
    assert sent["ephemeral"] is True
    assert sent["embed"].footer.text.startswith("Page 1 of 2")


# --- wiring: does a button rebuild the panel it was clicked on? -------------

def _interaction(footer_text, clicker=PLAYER, clicker_name="Me"):
    """A mocked interaction shaped like the one `_turn` sees: a message whose
    embed footer is what the panel is currently showing, plus the user and
    guild on the click -- which are the clicker's, and are not always the
    holder whose wallet the panel is showing."""
    message = SimpleNamespace(
        embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer_text))])
    return SimpleNamespace(
        message=message,
        user=SimpleNamespace(id=int(clicker), display_name=clicker_name),
        guild=SimpleNamespace(id=int(GUILD)),
        response=SimpleNamespace(edit_message=AsyncMock()),
    )


ADMIN = "222"


@pytest.mark.asyncio
async def test_paging_an_admin_panel_stays_on_the_player_it_was_opened_for(test_db):  # noqa: F811
    """/wallet-admin show renders someone else's wallet, so the click that turns
    the page is not coming from the holder. Rebuilding from `interaction.user`
    put the manager's own ledger behind the player's title -- a lookup that
    silently answers a different question than the one asked."""
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    for i in range(15):
        await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 1,
                     source=f"draft-entry:sid1:111:{i}:0-1")
    await ws.credit_done(GUILD, ADMIN, 7, job_id="admin-seed")

    embed, view = await whv.wallet_embed(GUILD, PLAYER, "Ada")
    assert embed.footer.text.startswith("Page 1 of 2")

    interaction = _interaction(embed.footer.text, clicker=ADMIN, clicker_name="Manager")
    await view.next_button.callback(interaction)

    new_embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert new_embed.title == "Ada's Tix Wallet"
    assert new_embed.footer.text.startswith("Page 2 of 2")
    assert "Entry fee" in embed_field(new_embed, "Recent activity").value
    assert embed_field(new_embed, "Balance").value == "**5** tix"
