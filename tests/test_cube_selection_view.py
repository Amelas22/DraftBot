import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from modals import CubeDraftSelectionView, StakedCubeDraftSelectionView

CUBES = [{"label": "AlphaFrog", "value": "AlphaFrog"}]


def make_view(session_type="random"):
    with patch("cube_views.pack_options.get_cube_options", return_value=CUBES):
        return CubeDraftSelectionView(session_type=session_type, guild_id=1)


def make_staked_view():
    with patch("cube_views.pack_options.get_cube_options", return_value=CUBES):
        return StakedCubeDraftSelectionView(guild_id=1)


def make_interaction(values=None):
    interaction = MagicMock()
    interaction.data = {"values": values} if values is not None else {}
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    return interaction


# ---- selection no longer starts the draft -----------------------------------

@pytest.mark.asyncio
async def test_selecting_cube_stores_choice_and_does_not_start():
    view = make_view()
    interaction = make_interaction(values=["AlphaFrog"])
    with patch("modals.handle_draft_session", new_callable=AsyncMock) as handler:
        await view.cube_select_callback(interaction)
    assert view.cube_choice == "AlphaFrog"
    handler.assert_not_called()
    interaction.response.send_modal.assert_not_called()
    interaction.response.edit_message.assert_awaited_once()


# ---- start button -----------------------------------------------------------

@pytest.mark.asyncio
async def test_start_without_a_cube_errors_and_does_not_start():
    view = make_view()
    interaction = make_interaction()
    with patch("modals.handle_draft_session", new_callable=AsyncMock) as handler:
        await view.submit_callback(interaction)
    handler.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "❌" in msg


@pytest.mark.asyncio
async def test_start_with_random_cube_creates_session_with_pack_settings():
    view = make_view()
    view.cube_choice = "AlphaFrog"
    view.packs_per_player = 4
    view.cards_per_pack = 10
    interaction = make_interaction()
    with patch("modals.handle_draft_session", new_callable=AsyncMock) as handler, \
         patch("modals.SessionDetails") as SD:
        details = MagicMock()
        SD.return_value = details
        await view.submit_callback(interaction)
    handler.assert_awaited_once()
    assert details.cube_choice == "AlphaFrog"
    assert details.packs_per_player == 4
    assert details.cards_per_pack == 10


@pytest.mark.asyncio
async def test_start_premade_opens_modal_carrying_pack_settings():
    view = make_view(session_type="premade")
    view.cube_choice = "AlphaFrog"
    view.packs_per_player = 5
    view.cards_per_pack = 12
    interaction = make_interaction()
    await view.submit_callback(interaction)
    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.call_args.args[0]
    assert modal.packs_per_player == 5
    assert modal.cards_per_pack == 12


# ---- staked view ------------------------------------------------------------

@pytest.mark.asyncio
async def test_staked_start_without_cube_errors():
    view = make_staked_view()
    interaction = make_interaction()
    with patch("modals.handle_staked_draft_session", new_callable=AsyncMock) as handler:
        await view.submit_callback(interaction)
    handler.assert_not_called()
    assert "❌" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_staked_start_creates_session_with_pack_settings_and_min_stake():
    view = make_staked_view()
    view.cube_choice = "AlphaFrog"
    view.packs_per_player = 4
    view.cards_per_pack = 10
    interaction = make_interaction()
    with patch("modals.handle_staked_draft_session", new_callable=AsyncMock) as handler, \
         patch("modals.SessionDetails") as SD:
        details = MagicMock()
        SD.return_value = details
        await view.submit_callback(interaction)
    handler.assert_awaited_once()
    assert details.cube_choice == "AlphaFrog"
    assert details.min_stake == 20
    assert details.packs_per_player == 4
    assert details.cards_per_pack == 10
