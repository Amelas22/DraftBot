"""What the wallet panel actually shows.

Goes through the real cog against a real (throwaway) ledger, so the panel is
pinned end to end rather than against a mocked describe_rows.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import wallet_history_view as whv
from cogs.wallet_cog import WalletCommands
from conftest import embed_field, seed_session, test_db  # noqa: F401  (fixtures)
from services import wallet_history as wh
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


@pytest.mark.asyncio
async def test_a_filtered_panel_shows_only_that_category(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 100, job_id="dep")
    await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 10,
                 source="draft-entry:sid1:111:0:0-10")

    embed, _ = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)
    activity = embed_field(embed, "Recent activity").value

    assert "Entry fee" in activity
    assert "Deposit from MTGO" not in activity


@pytest.mark.asyncio
async def test_the_footer_carries_the_filter_so_a_button_keeps_it(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 10,
                 source="draft-entry:sid1:111:0:0-10")
    embed, _ = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)
    assert embed.footer.text.endswith("· Drafts")
    assert whv.category_from_footer(embed.footer.text) == wh.DRAFT


@pytest.mark.asyncio
async def test_an_empty_filtered_panel_still_says_which_filter_it_is(test_db):  # noqa: F811
    """The footer is where the filter lives between clicks. An empty page is
    still a filtered panel, so it has to carry the category the same way -- the
    buttons are dead on a single page today, but the select is not, and it reads
    its state back off the message."""
    await ws.credit_done(GUILD, PLAYER, 5, job_id="dep")

    embed, _ = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)

    assert embed_field(embed, "Recent activity") is None
    assert embed.footer.text.endswith("· Drafts")
    assert whv.category_from_footer(embed.footer.text) == wh.DRAFT


def test_an_unfiltered_footer_names_no_category():
    assert whv.category_from_footer("Page 1 of 3 · 23 entries") is None
    assert whv.category_from_footer(None) is None


@pytest.mark.asyncio
async def test_the_picker_marks_the_active_category(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 5, job_id="d")
    _, view = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.MTGO)
    selected = [o.value for o in view.category_select.options if o.default]
    assert selected == [wh.MTGO]


def _wire_options(view):
    """The picker's options as they are SENT, not as the objects the view
    holds. Discord validates the serialized payload, so that is where an
    unsendable option has to be caught."""
    return [option
            for row in view.to_components()
            for component in row["components"]
            if component["type"] == 3  # a string select
            for option in component["options"]]


@pytest.mark.asyncio
async def test_every_filter_option_ships_a_value_discord_will_accept(test_db):  # noqa: F811
    """Discord requires an option value of 1 to 100 characters and rejects the
    whole message with 50035 without one. "All activity" is None internally, and
    None rendered as the empty string is not a filter that misbehaves -- it is
    /wallet show and /wallet-admin show failing to send at all, for everyone."""
    _, view = await whv.wallet_embed(GUILD, PLAYER, "Me")
    options = _wire_options(view)

    assert len(options) == len(whv.CATEGORY_LABELS)
    for option in options:
        assert 1 <= len(option["value"]) <= 100, option
        # ...and the value still says which category it is, on the way back in.
        category = whv._option_category(option["value"])
        assert whv.CATEGORY_LABELS[category] == option["label"]


@pytest.mark.asyncio
async def test_two_panels_do_not_share_one_set_of_options(test_db):  # noqa: F811
    """py-cord keeps the decorator's option list by reference, so options
    declared on the class are one set of objects for the whole process. Marking
    the active filter on them would let the newest panel re-select the filter on
    every panel already built -- another player's, on an admin's screen."""
    _, unfiltered = await whv.wallet_embed(GUILD, PLAYER, "Me")
    _, filtered = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)

    assert [o.value for o in unfiltered.category_select.options if o.default] \
        == [whv.ALL_CATEGORIES]
    assert [o.value for o in filtered.category_select.options if o.default] == [wh.DRAFT]


def test_the_all_activity_value_is_not_one_of_the_categories():
    """It is a name for "no filter" on the wire. If it collided with a real
    category, choosing All activity would filter to that category instead."""
    assert whv.ALL_CATEGORIES not in wh.CATEGORIES


@pytest.mark.asyncio
async def test_filtering_starts_again_at_the_first_page(test_db):  # noqa: F811
    """Page 3 of everything is not page 3 of drafts."""
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    for i in range(15):
        await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 1,
                     source=f"draft-entry:sid1:111:{i}:0-1")
    embed, _ = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)
    assert embed.footer.text.startswith("Page 1 of 2")


# --- wiring: does the button/select rebuild the panel it was clicked on? ----

def _interaction(footer_text, clicker=PLAYER, clicker_name="Me"):
    """A mocked interaction shaped like the one `_turn`/`category_select` see:
    a message whose embed's footer is what the panel is currently showing,
    plus the user and guild on the click -- which are the clicker's, and are
    not always the holder whose wallet the panel is showing."""
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
async def test_turning_a_page_preserves_the_active_filter(test_db):  # noqa: F811
    """Not just that the footer PARSES a category -- that `_turn` actually
    passes it back into `wallet_embed`. Nothing else exercises that wiring."""
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    for i in range(15):
        await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 1,
                     source=f"draft-entry:sid1:111:{i}:0-1")

    embed, view = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)
    interaction = _interaction(embed.footer.text)

    await view.next_button.callback(interaction)

    new_embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert new_embed.footer.text.startswith("Page 2 of 2")
    assert new_embed.footer.text.endswith("· Drafts")
    activity = embed_field(new_embed, "Recent activity").value
    assert "Entry fee" in activity
    assert "Deposit from MTGO" not in activity


@pytest.mark.asyncio
async def test_choosing_a_filter_resets_to_the_first_page(test_db):  # noqa: F811
    """Not just that `wallet_embed(category=...)` starts at page 0 -- that the
    select callback actually asks for page 0 rather than the page the panel
    happened to be on when it was opened."""
    for i in range(23):
        await ws.credit_done(GUILD, PLAYER, 1, job_id=f"j{i}")

    embed, view = await whv.wallet_embed(GUILD, PLAYER, "Me", page=2)
    assert embed.footer.text.startswith("Page 3 of 3")  # sanity: a later page

    interaction = _interaction(embed.footer.text)
    view.category_select._interaction = MagicMock()
    view.category_select._selected_values = [wh.MTGO]

    await view.category_select.callback(interaction)

    new_embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert new_embed.footer.text.startswith("Page 1 of 3")
    assert new_embed.footer.text.endswith("· MTGO deposits & withdrawals")


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


@pytest.mark.asyncio
async def test_filtering_an_admin_panel_stays_on_the_player_too(test_db):  # noqa: F811
    """The select rebuilds the whole panel the same way the buttons do, so it
    can lose the subject the same way."""
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 10,
                 source="draft-entry:sid1:111:0:0-10")
    await ws.credit_done(GUILD, ADMIN, 7, job_id="admin-seed")

    embed, view = await whv.wallet_embed(GUILD, PLAYER, "Ada")
    interaction = _interaction(embed.footer.text, clicker=ADMIN, clicker_name="Manager")
    view.category_select._interaction = MagicMock()
    view.category_select._selected_values = [wh.DRAFT]

    await view.category_select.callback(interaction)

    new_embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert new_embed.title == "Ada's Tix Wallet"
    assert new_embed.footer.text.endswith("· Drafts")
    assert "Entry fee" in embed_field(new_embed, "Recent activity").value


@pytest.mark.asyncio
async def test_choosing_all_activity_clears_the_filter(test_db):  # noqa: F811
    """The sentinel is a wire concern: the callback has to turn it back into the
    None the ledger query means by "no filter"."""
    await ws.credit_done(GUILD, PLAYER, 20, job_id="seed")
    await ws.pay(GUILD, PLAYER, "pool:draft:sid1", 10,
                 source="draft-entry:sid1:111:0:0-10")

    embed, view = await whv.wallet_embed(GUILD, PLAYER, "Me", category=wh.DRAFT)
    interaction = _interaction(embed.footer.text)
    view.category_select._interaction = MagicMock()
    view.category_select._selected_values = [whv.ALL_CATEGORIES]

    await view.category_select.callback(interaction)

    new_embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert whv.category_from_footer(new_embed.footer.text) is None
    activity = embed_field(new_embed, "Recent activity").value
    assert "Entry fee" in activity and "Deposit from MTGO" in activity
