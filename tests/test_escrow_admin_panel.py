"""/escrow-admin list: the moderator's panel for releasing or returning escrows."""
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import escrow_admin_view
from cogs.escrow_cog import EscrowCommands
from escrow_admin_view import EscrowPanel
from helpers.permissions import is_bot_manager
from models.escrow_hold import OPEN, RELEASED, RETURNED, EscrowHold
from services.escrow_service import EscrowAlreadyDecided, EscrowNotFound

COG = EscrowCommands.__new__(EscrowCommands)
MOD = 555


def _hold(hold_id, *, amount=60, note="tix sale", funder=111, counterparty=222,
          status=OPEN, decided_by=None):
    return EscrowHold(id=hold_id, guild_id="999", funder_id=str(funder),
                      counterparty_id=str(counterparty), amount=amount, note=note,
                      status=status, decided_by=decided_by,
                      created_at=datetime(2026, 11, 1, 12, 0))


def _guild(names=None):
    names = names if names is not None else {111: "Fay", 222: "Cal"}
    guild = MagicMock()

    def get_member(member_id):
        if member_id not in names:
            return None
        member = MagicMock()
        member.display_name = names[member_id]
        return member
    guild.get_member = get_member
    return guild


def _interaction():
    interaction = MagicMock()
    interaction.user.id = MOD
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.guild.id = 999
    return ctx


def _items(view):
    return {i.label: i for i in view.children if isinstance(i, discord.ui.Button)}


def _select(view):
    return next(i for i in view.children if isinstance(i, discord.ui.Select))


def _sent(interaction):
    """What the panel drew, through either the interaction's own response or, after a
    defer, the edit of the original message."""
    edit = (interaction.edit_original_response if interaction.response.defer.await_count
            else interaction.response.edit_message)
    return edit.await_args.kwargs


def _description(interaction):
    return _sent(interaction)["embed"].description


async def _click(view, label, interaction=None):
    interaction = interaction or _interaction()
    await _items(view)[label].callback(interaction)
    return interaction


async def _choose(view, hold_id, interaction=None):
    interaction = interaction or _interaction()
    select = _select(view)
    with patch.object(type(select), "values", [str(hold_id)]):
        await select.callback(interaction)
    return interaction


@contextmanager
def _open(holds):
    """The panel's reads: the open list, and a lookup by id over the same rows."""
    holds = list(holds)

    async def get_escrow(guild_id, escrow_id):
        return next((h for h in holds if h.id == escrow_id), None)

    with patch("escrow_admin_view.escrow_service.get_escrow", new=get_escrow), \
            patch("escrow_admin_view.escrow_service.open_escrows",
                  new=AsyncMock(return_value=[h for h in holds if h.status == OPEN])) as listed:
        yield listed


def _next_view(interaction):
    return _sent(interaction)["view"]


def test_the_command_is_for_bot_managers():
    assert is_bot_manager in EscrowCommands.escrow_admin_list.checks


@pytest.mark.asyncio
async def test_a_gate_refusal_is_relayed_and_nothing_is_read():
    ctx = _ctx()
    with patch("cogs.escrow_cog.gate_read", return_value="Wallets are closed."), \
         _open([_hold(1)]) as open_escrows:
        await EscrowCommands.escrow_admin_list.callback(COG, ctx)
    assert ctx.followup.send.await_args.args[0] == "Wallets are closed."
    open_escrows.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_command_sends_an_ephemeral_panel_without_pings():
    ctx = _ctx()
    with patch("cogs.escrow_cog.gate_read", return_value=None), _open([_hold(1)]):
        await EscrowCommands.escrow_admin_list.callback(COG, ctx)
    kwargs = ctx.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["embed"].title == "Open escrows"
    assert isinstance(kwargs["view"], EscrowPanel)


@pytest.mark.asyncio
async def test_an_empty_list_says_so_and_offers_nothing_to_pick():
    with _open([]):
        panel = await EscrowPanel.build("999", guild=_guild())
    assert panel.embed.description == "No open escrows."
    assert not any(isinstance(i, discord.ui.Select) for i in panel.children)


@pytest.mark.asyncio
async def test_23_escrows_make_3_pages_and_the_ends_disable_their_arrow():
    holds = [_hold(i) for i in range(1, 24)]
    with _open(holds):
        first = await EscrowPanel.build("999", 0, guild=_guild())
        last = await EscrowPanel.build("999", 2, guild=_guild())
    assert first.embed.footer.text == "Page 1 of 3"
    assert first.prev_button.disabled and not first.next_button.disabled
    assert last.embed.footer.text == "Page 3 of 3"
    assert last.next_button.disabled and not last.prev_button.disabled
    assert len(_select(last).options) == 3


@pytest.mark.asyncio
async def test_page_two_offers_escrows_11_to_20():
    with _open([_hold(i) for i in range(1, 24)]):
        panel = await EscrowPanel.build("999", 1, guild=_guild())
    assert [o.value for o in _select(panel).options] == [str(i) for i in range(11, 21)]
    assert "#11 " in panel.embed.description and "#20 " in panel.embed.description
    assert "#10 " not in panel.embed.description


@pytest.mark.asyncio
async def test_a_page_past_the_end_shows_the_last_page():
    with _open([_hold(i) for i in range(1, 12)]):
        panel = await EscrowPanel.build("999", 5, guild=_guild())
    assert panel.embed.footer.text == "Page 2 of 2"


@pytest.mark.asyncio
async def test_next_rereads_the_database_for_the_following_page():
    with _open([_hold(i) for i in range(1, 24)]) as open_escrows:
        panel = await EscrowPanel.build("999", 0, guild=_guild())
        interaction = await _click(panel, "▶")
    assert open_escrows.await_count == 2
    assert _sent(interaction)["embed"].footer.text == "Page 2 of 3"


@pytest.mark.asyncio
async def test_a_note_is_escaped_and_the_row_shows_the_parties_and_age():
    with _open([_hold(4, note="@everyone **x**\nsecond line")]):
        panel = await EscrowPanel.build("999", guild=_guild())
    row = panel.embed.description
    assert "@​everyone" in row and "\\*\\*x\\*\\*" in row
    assert "\n" not in row
    assert "<@111> → <@222>" in row and "60 tix" in row
    assert f"<t:{int(datetime(2026, 11, 1, 12, 0).timestamp())}:R>" in row


@pytest.mark.asyncio
async def test_ten_rows_with_full_length_notes_fit_an_embed():
    with _open([_hold(i, note="n" * 200) for i in range(1, 11)]):
        panel = await EscrowPanel.build("999", guild=_guild())
    assert len(panel.embed.description) <= 4096


@pytest.mark.asyncio
async def test_choosing_an_escrow_shows_its_detail_with_named_buttons():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        interaction = await _choose(panel, 7)
    detail = _next_view(interaction)
    assert set(_items(detail)) == {"Release to Cal", "Return to Fay", "Back"}
    assert "#7" in _sent(interaction)["embed"].description


@pytest.mark.asyncio
async def test_a_party_who_left_the_server_is_named_by_id():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild({111: "Fay"}))
        interaction = await _choose(panel, 7)
    assert "Release to User 222" in _items(_next_view(interaction))


@pytest.mark.asyncio
async def test_a_long_name_is_truncated_to_the_button_limit():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild({111: "Fay", 222: "C" * 90}))
        interaction = await _choose(panel, 7)
    labels = [i.label for i in _next_view(interaction).children
              if isinstance(i, discord.ui.Button)]
    assert all(len(label) <= 80 for label in labels)
    assert any(label.endswith("…") for label in labels)


async def _to_confirm(button, hold=None):
    """Panel -> detail -> the confirm step for `button`; returns the confirm view."""
    with _open([hold or _hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        detail = _next_view(await _choose(panel, 7))
        return _next_view(await _click(detail, button))


@pytest.mark.asyncio
async def test_clicking_release_asks_to_confirm_and_decides_nothing():
    with patch("escrow_admin_view.escrow_service.decide", new=AsyncMock()) as decide:
        confirm = await _to_confirm("Release to Cal")
    decide.assert_not_awaited()
    assert set(_items(confirm)) == {"Confirm: release 60 tix to Cal", "Cancel"}


@pytest.mark.asyncio
async def test_confirming_a_release_decides_with_the_moderators_id():
    decide = AsyncMock(return_value=_hold(7, status=RELEASED))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        interaction = await _click(confirm, "Confirm: release 60 tix to Cal")
    decide.assert_awaited_once_with("999", 7, RELEASED, str(MOD))
    assert "released" in _description(interaction)
    interaction.response.edit_message.assert_not_awaited()
    assert "Back to list" in _items(_next_view(interaction))


@pytest.mark.asyncio
async def test_confirming_a_return_decides_returned():
    decide = AsyncMock(return_value=_hold(7, status=RETURNED))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Return to Fay")
        interaction = await _click(confirm, "Confirm: return 60 tix to Fay")
    decide.assert_awaited_once_with("999", 7, RETURNED, str(MOD))
    assert "returned" in _description(interaction)


@pytest.mark.asyncio
async def test_cancel_goes_back_to_the_detail_without_deciding():
    with patch("escrow_admin_view.escrow_service.decide", new=AsyncMock()) as decide:
        confirm = await _to_confirm("Release to Cal")
        with _open([_hold(7)]):
            interaction = await _click(confirm, "Cancel")
    decide.assert_not_awaited()
    assert "Release to Cal" in _items(_next_view(interaction))


@pytest.mark.asyncio
async def test_an_escrow_decided_meanwhile_is_refused_with_who_decided():
    decided = _hold(7, status=RETURNED, decided_by="888")
    decide = AsyncMock(side_effect=EscrowAlreadyDecided(decided))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        interaction = await _click(confirm, "Confirm: release 60 tix to Cal")
    assert _description(interaction) == "Escrow #7 was already returned by <@888>."
    assert "Back to list" in _items(_next_view(interaction))


@pytest.mark.asyncio
async def test_an_escrow_that_vanished_is_refused():
    decide = AsyncMock(side_effect=EscrowNotFound())
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        interaction = await _click(confirm, "Confirm: release 60 tix to Cal")
    assert _description(interaction) == "That escrow no longer exists."


@pytest.mark.asyncio
async def test_an_escrow_decided_before_the_detail_is_drawn_says_who_decided_it():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
    with _open([_hold(7, status=RETURNED, decided_by="888")]):
        interaction = await _choose(panel, 7)
    assert _description(interaction) == "Escrow #7 was already returned by <@888>."
    assert "Back to list" in _items(_next_view(interaction))


@pytest.mark.asyncio
async def test_the_moderators_own_earlier_decision_reads_in_the_second_person():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
    with _open([_hold(7, status=RELEASED, decided_by=str(MOD))]):
        interaction = await _choose(panel, 7)
    assert _description(interaction) == "You already released escrow #7."


@pytest.mark.asyncio
async def test_an_escrow_that_no_longer_exists_is_said_so_at_the_detail():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
    with _open([]):
        interaction = await _choose(panel, 7)
    assert _description(interaction) == "That escrow no longer exists."


@pytest.mark.asyncio
async def test_an_escrow_decided_before_the_confirm_is_drawn_is_refused():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        detail = _next_view(await _choose(panel, 7))
    with _open([_hold(7, status=RELEASED, decided_by="888")]):
        interaction = await _click(detail, "Release to Cal")
    assert _description(interaction) == "Escrow #7 was already released by <@888>."


@pytest.mark.asyncio
async def test_back_to_list_rebuilds_from_the_database():
    decide = AsyncMock(return_value=_hold(7, status=RELEASED))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        done = _next_view(await _click(confirm, "Confirm: release 60 tix to Cal"))
    with _open([]):
        interaction = await _click(done, "Back to list")
    assert _sent(interaction)["embed"].description == "No open escrows."


@pytest.mark.asyncio
async def test_deciding_the_last_escrow_of_the_last_page_lands_on_the_last_page():
    holds = [_hold(i) for i in range(1, 12)]
    with _open(holds):
        panel = await EscrowPanel.build("999", 1, guild=_guild())
        detail = _next_view(await _choose(panel, 11))
        confirm = _next_view(await _click(detail, "Release to Cal"))
    decide = AsyncMock(return_value=_hold(11, status=RELEASED))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        done = _next_view(await _click(confirm, "Confirm: release 60 tix to Cal"))
    with _open(holds[:10]):
        interaction = await _click(done, "Back to list")
    assert _sent(interaction)["embed"].footer.text == "Page 1 of 1"


@pytest.mark.asyncio
async def test_the_live_view_disables_its_own_components_when_it_times_out():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        interaction = _interaction()
        interaction.edit_original_response = AsyncMock()
        detail = _next_view(await _choose(panel, 7, interaction))
    live = detail.interaction
    live.edit_original_response = AsyncMock()
    await detail.on_timeout()
    live.edit_original_response.assert_awaited_once_with(view=detail)
    assert all(i.disabled for i in detail.children)
    assert detail.timeout == 300


@pytest.mark.asyncio
async def test_a_failed_edit_at_timeout_is_swallowed():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
    panel.interaction = _interaction()
    panel.interaction.edit_original_response = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=404), "gone"))
    await panel.on_timeout()
    assert all(i.disabled for i in panel.children)


@pytest.mark.asyncio
async def test_a_replaced_view_is_stopped_and_never_edits_the_screen():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        first = _interaction()
        first.edit_original_response = AsyncMock()
        panel.interaction = first
        detail = _next_view(await _choose(panel, 7))
    assert panel.is_finished()
    assert not detail.is_finished()
    first.edit_original_response.assert_not_awaited()


def _timeline(interaction, decide):
    """Record the order of defer, decide and the two ways of drawing a screen."""
    order = []
    interaction.response.defer.side_effect = lambda *a, **k: order.append("defer")
    interaction.edit_original_response.side_effect = lambda *a, **k: order.append("edit")
    interaction.response.edit_message.side_effect = lambda *a, **k: order.append("response")

    async def recorded(*args):
        order.append("decide")
        return decide

    return order, recorded


@pytest.mark.asyncio
async def test_confirm_acknowledges_before_deciding_then_draws_through_the_deferred_edit():
    interaction = _interaction()
    order, recorded = _timeline(interaction, _hold(7, status=RELEASED))
    with patch("escrow_admin_view.escrow_service.decide", new=recorded):
        confirm = await _to_confirm("Release to Cal")
        await _click(confirm, "Confirm: release 60 tix to Cal", interaction)
    assert order == ["defer", "decide", "edit"]


@pytest.mark.asyncio
async def test_a_failed_edit_leaves_the_old_view_live_and_decides_only_once():
    decide = AsyncMock(return_value=_hold(7, status=RELEASED))
    interaction = _interaction()
    interaction.edit_original_response.side_effect = discord.HTTPException(
        MagicMock(status=404), "Unknown interaction")
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        await _click(confirm, "Confirm: release 60 tix to Cal", interaction)
    decide.assert_awaited_once()
    assert not confirm.is_finished()
    assert confirm.interaction is not interaction


@pytest.mark.asyncio
async def test_a_failed_edit_on_any_step_leaves_that_view_live_and_logs_it():
    with _open([_hold(7)]):
        panel = await EscrowPanel.build("999", guild=_guild())
        interaction = _interaction()
        interaction.response.edit_message.side_effect = discord.HTTPException(
            MagicMock(status=500), "boom")
        with patch("escrow_admin_view.logger") as log:
            await _choose(panel, 7, interaction)
    assert not panel.is_finished()
    log.warning.assert_called_once()


@pytest.mark.asyncio
async def test_a_double_click_on_confirm_tells_the_same_moderator_they_already_decided():
    decided = _hold(7, status=RELEASED, decided_by=str(MOD))
    decide = AsyncMock(side_effect=EscrowAlreadyDecided(decided))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        interaction = await _click(confirm, "Confirm: release 60 tix to Cal")
    assert _description(interaction) == "You already released escrow #7."


@pytest.mark.asyncio
async def test_another_moderators_decision_is_still_attributed_to_them():
    decided = _hold(7, status=RELEASED, decided_by="888")
    decide = AsyncMock(side_effect=EscrowAlreadyDecided(decided))
    with patch("escrow_admin_view.escrow_service.decide", new=decide):
        confirm = await _to_confirm("Release to Cal")
        interaction = await _click(confirm, "Confirm: release 60 tix to Cal")
    assert _description(interaction) == "Escrow #7 was already released by <@888>."
