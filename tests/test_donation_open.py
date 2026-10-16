"""A donation draft is a staked session stored with the flag set."""
from unittest.mock import MagicMock, patch

from models.session_details import SessionDetails
from sessions.staked_session import StakedSession


def _details(donation):
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    with patch("models.session_details.get_draftmancer_session_url", return_value="http://x"):
        details = SessionDetails(interaction)
    details.cube_choice = "LSVCube"
    details.min_stake = 20
    details.donation = donation
    return details


def test_a_donation_session_is_stored_as_a_staked_donation_draft():
    with patch("sessions.base_session.get_session_deletion_hours", return_value=4):
        row = StakedSession(_details(True)).setup_draft_session(MagicMock(), "x")

    assert row.session_type == "staked" and row.donation is True


def test_a_staked_session_is_not_a_donation_draft():
    with patch("sessions.base_session.get_session_deletion_hours", return_value=4):
        row = StakedSession(_details(False)).setup_draft_session(MagicMock(), "x")

    assert row.donation is False


def test_the_donation_queue_says_where_the_money_goes():
    embed = StakedSession(_details(True))._create_embed_content()

    assert "Donation Draft" in embed.title
    assert "DraftBot" in embed.description and "/donation_day paid_cash" in embed.description
