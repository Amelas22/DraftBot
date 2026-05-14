import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from cube_views.CubeListModal import CubeListModal


def make_interaction(guild_id=123, guild_name="Test Guild", user_name="admin"):
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.guild.name = guild_name
    interaction.user.name = user_name
    interaction.response.send_message = AsyncMock()
    return interaction


def make_modal(session_type="default", input_value=""):
    modal = CubeListModal(session_type=session_type, prefill="")
    modal.children[0].value = input_value
    return modal


@pytest.mark.asyncio
async def test_valid_input_saves_config():
    modal = make_modal(input_value="LSVCube : LSVCube\nAlphaFrog : AlphaFrog")
    interaction = make_interaction()

    with patch("cube_views.CubeListModal.get_config") as mock_get, \
         patch("cube_views.CubeListModal.save_config") as mock_save:
        mock_get.return_value = {}
        await modal.callback(interaction)

    mock_save.assert_called_once_with("123")
    saved_config = mock_get.return_value
    assert saved_config["cubes"]["default"] == [
        {"label": "LSVCube", "value": "LSVCube"},
        {"label": "AlphaFrog", "value": "AlphaFrog"},
    ]
    interaction.response.send_message.assert_called_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "✅" in msg
    assert "2 cubes" in msg


@pytest.mark.asyncio
async def test_valid_input_for_winston_session_type():
    modal = make_modal(session_type="winston", input_value="LSVWinston : LSVWinston")
    interaction = make_interaction()

    with patch("cube_views.CubeListModal.get_config") as mock_get, \
         patch("cube_views.CubeListModal.save_config"):
        mock_get.return_value = {}
        await modal.callback(interaction)

    assert mock_get.return_value["cubes"]["winston"] == [
        {"label": "LSVWinston", "value": "LSVWinston"},
    ]


@pytest.mark.asyncio
async def test_invalid_line_sends_error_and_does_not_save():
    modal = make_modal(input_value="LSVCube : LSVCube\nbad line\nAlphaFrog : AlphaFrog")
    interaction = make_interaction()

    with patch("cube_views.CubeListModal.get_config") as mock_get, \
         patch("cube_views.CubeListModal.save_config") as mock_save:
        mock_get.return_value = {}
        await modal.callback(interaction)

    mock_save.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "❌" in msg
    assert "Line 2" in msg


@pytest.mark.asyncio
async def test_blank_lines_are_ignored():
    modal = make_modal(input_value="LSVCube : LSVCube\n\n  \nAlphaFrog : AlphaFrog")
    interaction = make_interaction()

    with patch("cube_views.CubeListModal.get_config") as mock_get, \
         patch("cube_views.CubeListModal.save_config"):
        mock_get.return_value = {}
        await modal.callback(interaction)

    assert len(mock_get.return_value["cubes"]["default"]) == 2


@pytest.mark.asyncio
async def test_cube_id_containing_colon_is_preserved():
    modal = make_modal(input_value="My Cube : some:complex:id")
    interaction = make_interaction()

    with patch("cube_views.CubeListModal.get_config") as mock_get, \
         patch("cube_views.CubeListModal.save_config"):
        mock_get.return_value = {}
        await modal.callback(interaction)

    assert mock_get.return_value["cubes"]["default"] == [
        {"label": "My Cube", "value": "some:complex:id"},
    ]
