"""The control message follows the match through its state changes."""
import random
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


@pytest.mark.asyncio
async def test_cancel_reads_tournament_match_id_before_the_row_is_deleted():
    """Cancelling hard-deletes the draft row -- extend_deletion_if_unfinished
    exempts a linked unfinished draft from the reaper -- so this is the only
    place a linked draft's control message can ever go back to 'scheduling'.

    The id must be captured BEFORE the delete, not after. This test rigs the
    delete to mutate the session's tournament_match_id as a side effect, the
    way a real ORM delete can leave an object in a changed state. A capture
    that moved to *after* the delete block would read the mutated value and
    call refresh_match_control with it instead of the real id, failing the
    assertion below. (Verified by hand: temporarily moving the capture after
    the delete block makes this test fail with the mutated value — see the
    fix-round report.)
    """
    import views
    from ready_check import ReadyCheckSession
    from services.draft_setup_manager import ACTIVE_MANAGERS

    session = MagicMock()
    session.draft_channel_id = 555
    session.tournament_match_id = 99

    def _mutate_on_delete(obj):
        obj.tournament_match_id = "MUTATED-AFTER-DELETE"

    db_sess = MagicMock()
    db_sess.delete = AsyncMock(side_effect=_mutate_on_delete)
    db_sess.commit = AsyncMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=db_sess)
    begin.__aexit__ = AsyncMock(return_value=None)
    db_sess.begin = MagicMock(return_value=begin)
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=db_sess)
    outer.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=outer)

    view = views.CancelConfirmationView.__new__(views.CancelConfirmationView)
    view.bot = MagicMock()
    view.bot.get_channel = MagicMock(return_value=None)  # skip channel messaging entirely
    view.draft_session_id = "sid"
    view.user_display_name = "Alice"
    view.children = []

    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    with patch("views.get_draft_session", AsyncMock(return_value=session)), \
         patch("views.AsyncSessionLocal", session_factory), \
         patch.dict(ACTIVE_MANAGERS, {}, clear=True), \
         patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
         patch("match_control_view.refresh_match_control", AsyncMock()) as refresh:
        await view.confirm_button(MagicMock(), interaction)

    db_sess.delete.assert_awaited_once_with(session)
    refresh.assert_awaited_once_with(view.bot, 99)


@pytest.mark.asyncio
async def test_set_result_refreshes_the_match_control_message(test_db):
    """Admin /tournament set_result must also refresh the linked match's
    control message, so a match recorded by staff doesn't leave a stale
    control message (still offering 'Start draft' or a lobby link) behind."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session
    from services.tournament_service import create_tournament, register_team, start_tournament

    async with db_session() as session:
        tournament = await create_tournament(session, "g1", "Cup", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        match_id = matches[0].id

    cog = TournamentCog(MagicMock())
    ctx = MagicMock()
    ctx.guild.id = "g1"
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("match_control_view.refresh_match_control", AsyncMock()) as refresh:
        await TournamentCog.set_result.callback(
            cog, ctx, team="Alpha", team_wins=2, opponent_wins=0)

    refresh.assert_awaited_once_with(cog.bot, match_id)
