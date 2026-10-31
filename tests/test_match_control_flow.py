"""Play / Start draft behaviour, with Discord mocked at the boundary."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import match_control_db, seed_tournament_match  # noqa: F401  (fixture)
from models.draft_session import DraftSession
from models.tournament import TournamentMatch


@pytest.fixture
def patched_db(match_control_db):
    """Point match_control_view's db_session at the throwaway database."""
    @asynccontextmanager
    async def fake_db_session():
        async with match_control_db() as inner:
            yield inner
            await inner.commit()

    with patch("match_control_view.db_session", fake_db_session):
        yield match_control_db


def make_thread(thread_id=900):
    thread = MagicMock()
    thread.id = thread_id
    thread.mention = f"<#{thread_id}>"
    sent = MagicMock()
    sent.id = 777
    thread.send = AsyncMock(return_value=sent)
    thread.fetch_message = AsyncMock()
    thread.delete = AsyncMock()
    return thread


def make_interaction(thread):
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.guild.get_channel.return_value = thread
    interaction.message.thread = None
    interaction.message.create_thread = AsyncMock(return_value=thread)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_start_draft_posts_the_cube_picker(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()) as picker:
        await start_match_draft(interaction, match.id)

    picker.assert_called_once()
    assert picker.call_args.args[1] == match.id
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_draft_refuses_when_a_draft_is_live(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        session.add(DraftSession(
            session_id="d1", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))
        await session.commit()

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()) as picker:
        await start_match_draft(interaction, match.id)

    picker.assert_not_called()
    # It re-renders the stale message rather than only complaining to the clicker.
    interaction.response.edit_message.assert_awaited_once()
    assert "already underway" in interaction.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_refresh_is_a_noop_when_no_control_message_exists(patched_db):
    from match_control_view import refresh_match_views

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")

    bot = MagicMock()
    bot.get_channel.return_value = make_thread()
    await refresh_match_views(bot, match.id)

    bot.get_channel.return_value.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_draft_picker_is_ephemeral(patched_db):
    from match_control_view import start_match_draft

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()):
        await start_match_draft(interaction, match.id)

    # Ephemeral: the picker never persists in the thread, so no stale picker can
    # create a second draft for this match once one is under way.
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_start_draft_refresh_does_not_remention_roles(patched_db):
    """start_match_draft re-renders the SAME control message a live draft
    already blocks on; it must not re-add the mention line even when both
    teams have roles (control_body_and_view's docstring explains why)."""
    from match_control_view import start_match_draft
    from models.tournament import TournamentParticipant

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        part_a.role_id = "111"
        part_b.role_id = "222"
        session.add(DraftSession(
            session_id="d1", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))
        await session.commit()

    interaction = make_interaction(make_thread())
    with patch("match_control_view.cube_picker_for_match", MagicMock()):
        await start_match_draft(interaction, match.id)

    body = interaction.response.edit_message.call_args.kwargs["content"]
    assert "<@&" not in body


@pytest.mark.asyncio
async def test_refresh_control_message_does_not_remention_roles(patched_db):
    """refresh_match_views re-renders the SAME control message on every later
    refresh; it must not re-add the mention line even when both teams have
    roles (control_body_and_view's docstring explains why)."""
    from match_control_view import refresh_match_views
    from models.tournament import TournamentParticipant

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        stored = await session.get(TournamentMatch, match.id)
        stored.control_message_id = "777"
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        part_a.role_id = "111"
        part_b.role_id = "222"
        await session.commit()

    control_message = MagicMock()
    control_message.edit = AsyncMock()
    thread = MagicMock()
    thread.fetch_message = AsyncMock(return_value=control_message)
    bot = MagicMock()
    bot.get_channel.return_value = thread

    await refresh_match_views(bot, match.id)

    body = control_message.edit.call_args.kwargs["content"]
    assert "<@&" not in body


@pytest.mark.asyncio
async def test_launch_block_for_is_none_when_match_is_free(patched_db):
    from match_control_view import launch_block_for

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")

    assert await launch_block_for(match.id) is None


@pytest.mark.asyncio
async def test_launch_block_for_reports_a_live_draft(patched_db):
    from match_control_view import launch_block_for

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        session.add(DraftSession(
            session_id="d1", guild_id="g1", session_type="premade",
            draft_channel_id="55", message_id="66", tournament_match_id=match.id,
        ))
        await session.commit()

    block = await launch_block_for(match.id)
    assert "already underway" in block


async def _give_roles(session, match, a="111", b="222"):
    """Both teams hold a role: a room is only opened for teams it can tag."""
    from models.tournament import TournamentParticipant

    (await session.get(TournamentParticipant, match.team_a_participant_id)).role_id = a
    (await session.get(TournamentParticipant, match.team_b_participant_id)).role_id = b
    await session.commit()


@pytest.mark.asyncio
async def test_create_match_room_makes_a_long_lived_thread_and_pins_control(patched_db):
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await seed_tournament_match(session)
        await _give_roles(session, match)

    thread = make_thread()
    message = MagicMock()
    message.create_thread = AsyncMock(return_value=thread)

    with patch("match_control_view.safe_pin", AsyncMock()) as pin:
        result = await create_match_room(message, match.id)

    assert result is thread
    # 7 days: a thread made when pairings post must still be in the sidebar when
    # the match is actually played midweek.
    assert message.create_thread.call_args.kwargs["auto_archive_duration"] == 10080
    thread.send.assert_awaited_once()
    pin.assert_awaited_once()
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id == "900"
        assert stored.control_message_id == "777"


@pytest.mark.asyncio
async def test_create_match_room_mentions_both_team_roles(patched_db):
    """The control message's first post is the only place a mention belongs
    (see control_body_and_view's docstring), and it must actually reach
    Discord as a real mention -- not just a pill nobody gets notified by."""
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await seed_tournament_match(session)
        await _give_roles(session, match)

    thread = make_thread()
    message = MagicMock()
    message.create_thread = AsyncMock(return_value=thread)

    with patch("match_control_view.safe_pin", AsyncMock()):
        await create_match_room(message, match.id)

    body = thread.send.call_args.kwargs["content"]
    assert "<@&111>" in body and "<@&222>" in body
    # Naming the two roles is what turns the pill into a real mention: it
    # notifies both teams and adds their members to the thread, and a role id
    # smuggled in through a team name stays a pill.
    allowed_mentions = thread.send.call_args.kwargs["allowed_mentions"]
    assert allowed_mentions.to_dict()["roles"] == [111, 222]
    assert "roles" not in allowed_mentions.to_dict()["parse"]
    # everyone=False is the only thing standing between a team name containing
    # "@everyone" and a guild-wide ping: register_team only strips whitespace.
    assert allowed_mentions.everyone is False


@pytest.mark.asyncio
async def test_control_message_permits_role_mentions_without_a_view():
    """_resolve_control_message has two send branches. The no-view one is
    unreachable today -- both callers filter to unreported matches, so a view
    always exists -- but it must not be the one that quietly drops roles=True
    if that ever changes."""
    from match_control_view import _resolve_control_message

    thread = make_thread()
    with patch("match_control_view.safe_pin", AsyncMock()):
        await _resolve_control_message(thread, None, "body", None, role_ids=("111",))

    allowed_mentions = thread.send.call_args.kwargs["allowed_mentions"]
    assert allowed_mentions.to_dict()["roles"] == [111]
    assert allowed_mentions.everyone is False


@pytest.mark.asyncio
@pytest.mark.parametrize("b_role, guild_has", [(None, True), ("222", False)])
async def test_create_match_room_refuses_a_team_without_a_working_role(patched_db, b_role, guild_has):
    """No role stored, or one the guild has since lost: nobody on that side
    would be tagged, so no room opens (a bracket result reaches here unguarded)."""
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await seed_tournament_match(session)
        await _give_roles(session, match, b=b_role)

    message = MagicMock()
    message.create_thread = AsyncMock(return_value=make_thread())
    message.guild.get_role = lambda rid: MagicMock() if guild_has or rid == 111 else None

    assert await create_match_room(message, match.id) is None
    message.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_match_room_degrades_when_discord_refuses(patched_db):
    import discord as _discord
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await seed_tournament_match(session)
        await _give_roles(session, match)

    message = MagicMock()
    message.create_thread = AsyncMock(
        side_effect=_discord.HTTPException(MagicMock(), "no Manage Threads"))

    # The round must still post: a missing room costs a link, not the pairings.
    assert await create_match_room(message, match.id) is None
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id is None


@pytest.mark.asyncio
async def test_create_match_room_cleans_up_when_the_control_message_fails(patched_db):
    import discord as _discord
    from match_control_view import create_match_room

    async with patched_db() as session:
        match = await seed_tournament_match(session)
        await _give_roles(session, match)

    thread = make_thread()
    thread.send = AsyncMock(side_effect=_discord.HTTPException(MagicMock(), "boom"))
    message = MagicMock()
    message.create_thread = AsyncMock(return_value=thread)

    # A thread with no control message is a dead room: better none at all.
    assert await create_match_room(message, match.id) is None
    async with patched_db() as session:
        stored = await session.get(TournamentMatch, match.id)
        assert stored.thread_id is None
        assert stored.control_message_id is None


@pytest.mark.asyncio
async def test_refresh_updates_the_pairing_message_with_the_result(patched_db):
    import discord as _discord
    from match_control_view import refresh_match_views
    from services.tournament_service import set_result

    async with patched_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        stored = await session.get(TournamentMatch, match.id)
        stored.pairings_channel_id = "10"
        stored.pairings_message_id = "11"
        stored.control_message_id = None  # only the pairing message exists
        await set_result(session, match.id, 2, 1)
        await session.commit()

    pairing = MagicMock()
    pairing.edit = AsyncMock()
    # Spec'd so as_messageable's isinstance narrowing (a real Messageable check,
    # not a duck-typed one) accepts this mock the way it would a real channel.
    channel = MagicMock(spec=_discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=pairing)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    await refresh_match_views(bot, match.id)

    assert "Result recorded" in pairing.edit.call_args.kwargs["content"]
    assert "<#900>" in pairing.edit.call_args.kwargs["content"]
    # A pairing message posted before this task's switchover can still carry a
    # dead ▶ Play button; the rewrite must strip it, not just replace the text.
    assert pairing.edit.call_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_match_control_view_buttons():
    import discord
    from match_control_view import MatchControlView

    view = MatchControlView(135)
    assert [(b.label, b.style, b.custom_id) for b in view.children] == [
        ("Find a time", discord.ButtonStyle.secondary, "tournament_find_time:135"),
        ("Start draft", discord.ButtonStyle.primary, "tournament_start_draft:135"),
    ]


@pytest.mark.asyncio
async def test_find_a_time_sends_an_ephemeral_link_to_anyone():
    from datetime import datetime
    from helpers.matchup import TIME_LINK_INTRO
    from match_control_view import find_a_time_for_match
    from services.scheduler_match_api import VIEW

    interaction = make_interaction(make_thread())
    interaction.user.id = 4242  # on neither roster
    with patch("helpers.booking_room.scheduler_enabled", return_value=True), \
            patch("helpers.booking_room.issue_link",
                  AsyncMock(return_value="https://draftbot.fyi/x")) as issue:
        await find_a_time_for_match(interaction, 135)

    args, kwargs = issue.call_args
    assert args[:3] == ("4242", "1", VIEW)
    assert isinstance(args[3], datetime)
    assert kwargs == {"subject": "135"}
    call = interaction.response.send_message.call_args
    assert call.args[0] == TIME_LINK_INTRO
    assert call.kwargs["ephemeral"] is True
    (button,) = call.kwargs["view"].children
    assert (button.label, button.url) == ("Open Find a time", "https://draftbot.fyi/x")


@pytest.mark.asyncio
async def test_find_a_time_refuses_when_scheduler_is_off():
    from helpers.matchup import MATCH_SCHEDULING_NOT_ENABLED
    from match_control_view import find_a_time_for_match

    interaction = make_interaction(make_thread())
    with patch("helpers.booking_room.scheduler_enabled", return_value=False), \
            patch("helpers.booking_room.issue_link", AsyncMock()) as issue:
        await find_a_time_for_match(interaction, 135)

    issue.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        MATCH_SCHEDULING_NOT_ENABLED, ephemeral=True)


@pytest.mark.asyncio
async def test_find_a_time_reports_a_failed_link():
    from helpers.matchup import TIME_LINK_FAILED
    from match_control_view import find_a_time_for_match

    interaction = make_interaction(make_thread())
    with patch("helpers.booking_room.scheduler_enabled", return_value=True), \
            patch("helpers.booking_room.issue_link", AsyncMock(side_effect=RuntimeError("down"))):
        await find_a_time_for_match(interaction, 135)

    interaction.response.send_message.assert_awaited_once_with(TIME_LINK_FAILED, ephemeral=True)


def test_team_draft_view_still_exposes_the_shared_copy():
    import team_draft_view
    from helpers import matchup

    assert team_draft_view.NOT_ENABLED == matchup.NOT_ENABLED
