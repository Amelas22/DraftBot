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
    facts = (MagicMock(), "Alpha", "Bravo", "Round 2", None)
    bot = MagicMock()

    with patch.object(match_control_view, "match_facts", AsyncMock(return_value=facts)), \
         patch.object(match_control_view, "db_session", _fake_db_session()), \
         patch.object(match_control_view, "_refresh_match_views_with_facts", AsyncMock()) as refresh:
        await match_control_view.announce_and_refresh(bot, channel, 7)

    posted = channel.send.call_args.args[0]
    assert "Round 2" in posted and "Alpha" in posted and "Bravo" in posted
    assert "record automatically" in posted
    # Facts already fetched for the announcement text are passed straight
    # through -- announce_and_refresh must not fetch this match a second time.
    refresh.assert_awaited_once_with(bot, 7, facts)


@pytest.mark.asyncio
async def test_linked_premade_draft_announces_instead_of_nudging(test_db):
    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = match.id

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
async def test_integrity_error_race_answers_with_the_friendly_message(test_db):
    """The creation-time guard is read-then-act, not atomic: two pickers can
    both pass it and then race draft_sessions.tournament_match_id's unique
    index at commit. The loser's IntegrityError must not surface as a broken
    interaction -- it must read exactly like the guard's own message would
    have, so the player sees "already underway" either way."""
    from sqlalchemy.exc import IntegrityError

    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.draft_session import DraftSession
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    interaction.response.send_message = AsyncMock()
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = match.id

    session = PremadeSession(details)

    async def _lose_the_race(*_args, **_kwargs):
        # The race's winner commits ITS draft during this call -- the guard
        # above already ran and saw the match free, exactly like the real
        # race this simulates (both pickers pass the guard, then race the
        # unique-index commit).
        async with db_session() as winner_session:
            winner_session.add(DraftSession(
                session_id="winner", guild_id="g1", session_type="premade",
                draft_channel_id="55", message_id="66", tournament_match_id=match.id,
            ))
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    with patch("sessions.base_session.BaseSession.create_draft_session", _lose_the_race):
        await session.create_draft_session(interaction, MagicMock())

    interaction.response.send_message.assert_awaited_once_with(
        "A draft for this match is already underway.", ephemeral=True)


@pytest.mark.asyncio
async def test_integrity_error_without_a_tournament_match_still_raises():
    """The catch is scoped to the tournament-match race -- an IntegrityError
    from an unrelated cause on a non-tournament draft must not be swallowed
    into a message that has nothing to do with it."""
    from sqlalchemy.exc import IntegrityError

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
    other_error = IntegrityError("INSERT", {}, Exception("some other constraint"))
    with patch("sessions.base_session.BaseSession.create_draft_session",
               AsyncMock(side_effect=other_error)):
        with pytest.raises(IntegrityError):
            await session.create_draft_session(interaction, MagicMock())


@pytest.mark.asyncio
async def test_integrity_error_for_a_match_with_no_existing_draft_still_raises(test_db):
    """Narrowed per the correctness review: the catch must not blame ANY
    IntegrityError on the tournament-match race just because a match id is
    present. If nothing is actually linked to this match after the failure,
    it must re-raise so the real error surfaces -- not lie with the
    "already underway" message, which would point players at a race that
    never happened."""
    from sqlalchemy.exc import IntegrityError

    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    interaction.response.send_message = AsyncMock()
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = match.id

    session = PremadeSession(details)
    # No draft ever gets linked to this match -- unlike the race test above,
    # nothing commits one as a side effect of the failure below.
    unrelated_error = IntegrityError("INSERT", {}, Exception("some other constraint"))
    with patch("sessions.base_session.BaseSession.create_draft_session",
               AsyncMock(side_effect=unrelated_error)):
        with pytest.raises(IntegrityError):
            await session.create_draft_session(interaction, MagicMock())

    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_reads_tournament_match_id_before_the_row_is_deleted():
    """Cancelling hard-deletes the draft row -- extend_deletion_if_unfinished
    exempts a linked unfinished draft from the reaper -- so this is the only
    place a linked draft's control message can ever go back to 'scheduling'.

    The id must be captured BEFORE the delete, not after. This test rigs the
    delete to mutate the session's tournament_match_id as a side effect, the
    way a real ORM delete can leave an object in a changed state. A capture
    that moved to *after* the delete block would read the mutated value and
    call refresh_match_views with it instead of the real id, failing the
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

    with patch.object(views, "release_draft_pool",
                      AsyncMock(return_value={"refunded": {}})), \
    patch("views.get_draft_session", AsyncMock(return_value=session)), \
         patch("views.AsyncSessionLocal", session_factory), \
         patch.dict(ACTIVE_MANAGERS, {}, clear=True), \
         patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
         patch("match_control_view.refresh_match_views", AsyncMock()) as refresh:
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
         patch("match_control_view.refresh_match_views", AsyncMock()) as refresh:
        await TournamentCog.set_result.callback(
            cog, ctx, team="Alpha", team_wins=2, opponent_wins=0)

    refresh.assert_awaited_once_with(cog.bot, match_id)


@pytest.mark.asyncio
async def test_premade_draft_refuses_a_match_that_already_has_a_draft(test_db):
    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.draft_session import DraftSession
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)
        seed_session.add(DraftSession(
            session_id="existing", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    interaction.response.send_message = AsyncMock()
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = match.id

    session = PremadeSession(details)
    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()) as create:
        await session.create_draft_session(interaction, MagicMock())

    # The draft must not be created at all: this is the last point before
    # tournament_match_id is written, and nothing downstream re-checks it.
    create.assert_not_awaited()
    assert "already underway" in interaction.response.send_message.call_args.args[0]
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_premade_draft_proceeds_when_the_match_is_free(test_db):
    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = match.id

    session = PremadeSession(details)
    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()) as create, \
         patch("match_control_view.announce_and_refresh", AsyncMock()):
        await session.create_draft_session(interaction, MagicMock())

    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_premade_draft_rederives_current_match_names_over_stale_overrides(test_db):
    """Correctness fix: the picker's session_details_overrides are captured
    when it OPENS. If the match's side order flips before this submit
    (services/tournament_linking.py's link_draft_to_match swaps
    team_a/b_participant_id on a reversed-name match, and a later
    cancellation never restores it), a picker opened before the flip and
    submitted after it would otherwise persist the OLD names against the
    NEW side order -- silently inverting the recorded result.
    create_draft_session must re-derive the names from the match's CURRENT
    facts at creation time instead of trusting the captured overrides."""
    from sqlalchemy import select

    from conftest import seed_tournament_match
    from database.db_session import db_session
    from models.draft_session import DraftSession
    from models.session_details import SessionDetails
    from sessions.premade_session import PremadeSession

    async with db_session() as seed_session:
        match = await seed_tournament_match(seed_session)  # current order: Alpha=A, Bravo=B

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    interaction.guild = MagicMock()
    interaction.guild.id = 123
    interaction.response = AsyncMock()
    interaction.original_response = AsyncMock()
    mock_message = MagicMock()
    mock_message.id = "1"
    mock_message.channel = MagicMock()
    mock_message.channel.id = "2"
    interaction.original_response.return_value = mock_message
    interaction.client = MagicMock()

    details = SessionDetails(interaction)
    details.cube_choice = "AlphaFrog"
    details.tournament_match_id = match.id
    # Stale overrides, as if the picker opened before the match's sides
    # swapped: reversed relative to the match's CURRENT order (Alpha=A,
    # Bravo=B).
    details.team_a_name = "Bravo"
    details.team_b_name = "Alpha"

    premade_session = PremadeSession(details)

    mock_draft_manager = MagicMock()
    mock_draft_manager.keep_connection_alive = AsyncMock()
    mock_draft_manager.socket_client = MagicMock()
    mock_draft_manager.socket_client.connected = False

    with patch('sessions.base_session.DraftSetupManager', return_value=mock_draft_manager), \
         patch('sessions.base_session.PersistentView'), \
         patch('sessions.base_session.make_message_sticky', new_callable=AsyncMock), \
         patch('sessions.base_session.get_session_deletion_hours', return_value=5), \
         patch('sessions.base_session.get_cube_thumbnail_url', return_value='https://example.com/thumb.jpg'), \
         patch('match_control_view.announce_and_refresh', new_callable=AsyncMock):
        await premade_session.create_draft_session(interaction, interaction.client)

    async with db_session() as check_session:
        draft = (await check_session.execute(
            select(DraftSession).where(DraftSession.session_id == details.session_id)
        )).scalars().first()

    assert draft is not None
    assert draft.team_a_name == "Alpha", "must use the match's CURRENT name, not the stale override"
    assert draft.team_b_name == "Bravo", "must use the match's CURRENT name, not the stale override"
