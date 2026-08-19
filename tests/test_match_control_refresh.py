"""The control message follows the match through its state changes."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_db_session():
    @asynccontextmanager
    async def fake():
        yield MagicMock()
    return fake


@pytest.mark.asyncio
async def test_announce_posts_the_link_line_and_refreshes():
    import match_control_view

    channel = MagicMock()
    channel.send = AsyncMock()
    facts = (MagicMock(), "Alpha", "Bravo", 2, None)
    bot = MagicMock()

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()), \
         patch.object(match_control_view, "refresh_match_control", AsyncMock()) as refresh:
        await match_control_view.announce_and_refresh(bot, channel, 7)

    posted = channel.send.call_args.args[0]
    assert "Round 2" in posted and "Alpha" in posted and "Bravo" in posted
    assert "record automatically" in posted
    refresh.assert_awaited_once_with(bot, 7)


@pytest.mark.asyncio
async def test_linked_premade_draft_announces_instead_of_nudging():
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = 77

    session = PremadeSession(details)
    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()), \
         patch("match_control_view.announce_and_refresh", AsyncMock()) as announce, \
         patch("tournament_nudge.post_premade_nudge", AsyncMock()) as nudge:
        await session.create_draft_session(interaction, MagicMock())

    announce.assert_awaited_once()
    nudge.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlinked_premade_draft_still_nudges():
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"

    session = PremadeSession(details)
    session.draft_manager = MagicMock()
    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()), \
         patch("match_control_view.announce_and_refresh", AsyncMock()) as announce, \
         patch("tournament_nudge.post_premade_nudge", AsyncMock()) as nudge:
        await session.create_draft_session(interaction, MagicMock())

    announce.assert_not_awaited()
    nudge.assert_awaited_once()
