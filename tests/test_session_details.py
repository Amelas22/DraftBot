from unittest.mock import MagicMock, patch

from models.session_details import SessionDetails


def make_interaction(user_id=42, guild_id=123):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = guild_id
    return interaction


def test_pack_settings_default_to_three_by_fifteen():
    with patch("models.session_details.get_draftmancer_session_url", return_value="http://x"):
        details = SessionDetails(make_interaction())
    assert details.packs_per_player == 3
    assert details.cards_per_pack == 15
