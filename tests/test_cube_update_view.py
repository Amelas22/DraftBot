import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cube_views.CubeSelectionView import CubeUpdateSelectionView
from cube_views.pack_options import CustomCubeNameModal

CUBES = [{"label": "AlphaFrog", "value": "AlphaFrog"}]


def make_view(on_submit=None, current_cube=None):
    with patch("cube_views.pack_options.get_cube_options", return_value=CUBES):
        return CubeUpdateSelectionView(
            session_type="random", guild_id=1, current_cube=current_cube, on_submit=on_submit
        )


def make_interaction(values=None):
    interaction = MagicMock()
    interaction.data = {"values": values} if values is not None else {}
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_view_has_custom_option_and_buttons():
    view = make_view()
    values = [o.value for o in view.cube_select.options]
    assert "custom" in values
    # advanced + submit buttons present
    assert hasattr(view, "advanced_button")
    assert hasattr(view, "submit_button")


@pytest.mark.asyncio
async def test_selecting_cube_stores_choice_without_submitting():
    on_submit = AsyncMock()
    view = make_view(on_submit=on_submit)
    interaction = make_interaction(values=["AlphaFrog"])
    await view.cube_select_callback(interaction)
    assert view.cube_choice == "AlphaFrog"
    on_submit.assert_not_called()
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_without_cube_errors():
    on_submit = AsyncMock()
    view = make_view(on_submit=on_submit)
    interaction = make_interaction()
    await view.submit_callback(interaction)
    on_submit.assert_not_called()
    assert "❌" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_submit_with_cube_invokes_on_submit_with_pack_settings():
    on_submit = AsyncMock()
    view = make_view(on_submit=on_submit)
    view.cube_choice = "AlphaFrog"
    view.packs_per_player = 4
    view.cards_per_pack = 10
    interaction = make_interaction()
    await view.submit_callback(interaction)
    on_submit.assert_awaited_once()
    args = on_submit.call_args.args
    assert args[0] is interaction
    assert args[1] is view
    assert view.packs_per_player == 4
    assert view.cards_per_pack == 10


@pytest.mark.asyncio
async def test_submit_with_custom_opens_name_modal():
    on_submit = AsyncMock()
    view = make_view(on_submit=on_submit)
    view.cube_choice = "custom"
    interaction = make_interaction()
    await view.submit_callback(interaction)
    interaction.response.send_modal.assert_awaited_once()
    on_submit.assert_not_called()


@pytest.mark.asyncio
async def test_custom_name_modal_sets_choice_and_submits():
    on_submit = AsyncMock()
    view = make_view(on_submit=on_submit)
    modal = CustomCubeNameModal(view, on_submit)
    modal.children[0].value = "MyHomebrew"
    interaction = make_interaction()
    await modal.callback(interaction)
    assert view.cube_choice == "MyHomebrew"
    on_submit.assert_awaited_once()
