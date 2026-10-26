"""A team draft's thread: opening it, its control message and the one-shot Start draft."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.draft_session import DraftSession
from models.team_draft import TeamDraft
from services.team_draft import Picks, create_team_draft

NOW = datetime(2026, 10, 26, 18, 0)
ROLES = {111: "Team Garruk"}
MEMBERS = {201: "Ash", 202: "Bo", 300: "Organiser"}


def make_guild(roles=ROLES, members=MEMBERS):
    guild = MagicMock()
    guild.id = 1
    guild.get_role.side_effect = lambda rid: (
        SimpleNamespace(name=roles[rid]) if rid in roles else None)
    guild.get_member.side_effect = lambda uid: (
        SimpleNamespace(display_name=members[uid]) if uid in members else None)
    return guild


async def seed_team_draft(thread_id=None, control_message_id=None, name="Team Garruk vs Ash, Bo"):
    async with db_session() as session:
        row = await create_team_draft(
            session, "1", "50", name,
            Picks(role_ids=("111",), user_ids=()),
            Picks(role_ids=(), user_ids=("201", "202")),
            created_by="300", now=NOW,
        )
        row.thread_id = thread_id
        row.control_message_id = control_message_id
        return row.id


async def stored(team_draft_id):
    async with db_session() as session:
        return await session.get(TeamDraft, team_draft_id)


def make_channel(guild, send_side_effect=None):
    thread = MagicMock()
    thread.id = 900
    control = MagicMock()
    control.id = 777
    control.pin = AsyncMock()
    thread.send = AsyncMock(return_value=control, side_effect=send_side_effect)
    thread.delete = AsyncMock()
    channel = MagicMock()
    channel.guild = guild
    channel.create_thread = AsyncMock(return_value=thread)
    return channel, thread, control


def make_interaction(guild):
    interaction = MagicMock()
    interaction.guild = guild
    interaction.guild_id = guild.id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_opening_a_room_tags_both_sides_pins_and_records_it(test_db):
    from team_draft_view import TeamDraftControlView, open_team_draft_room

    team_draft_id = await seed_team_draft()
    channel, thread, control = make_channel(make_guild())

    room = await open_team_draft_room(channel, team_draft_id)

    assert room is thread
    kwargs = channel.create_thread.await_args.kwargs
    assert kwargs["name"] == "Team Garruk vs Ash, Bo"
    assert kwargs["type"] == discord.ChannelType.public_thread
    assert kwargs["auto_archive_duration"] == 10080
    sent = thread.send.await_args.kwargs
    assert "<@&111>" in sent["content"] and "<@201>" in sent["content"] and "<@202>" in sent["content"]
    assert "Set up by Organiser." in sent["content"]
    assert sent["allowed_mentions"].everyone is False
    assert sent["allowed_mentions"].roles is True
    assert sent["allowed_mentions"].users is True
    assert isinstance(sent["view"], TeamDraftControlView)
    control.pin.assert_awaited_once()
    row = await stored(team_draft_id)
    assert (row.thread_id, row.control_message_id) == ("900", "777")


@pytest.mark.asyncio
async def test_a_renamed_matchup_names_its_thread_by_the_chosen_name(test_db):
    from team_draft_view import open_team_draft_room

    team_draft_id = await seed_team_draft(name="Friday showdown")
    channel, _thread, _control = make_channel(make_guild())

    await open_team_draft_room(channel, team_draft_id)

    assert channel.create_thread.await_args.kwargs["name"] == "Friday showdown"


@pytest.mark.asyncio
async def test_a_room_whose_control_message_fails_is_deleted(test_db):
    from team_draft_view import open_team_draft_room

    team_draft_id = await seed_team_draft()
    failure = discord.HTTPException(MagicMock(status=403), "Missing Permissions")
    channel, thread, _control = make_channel(make_guild(), send_side_effect=failure)

    room = await open_team_draft_room(channel, team_draft_id)

    assert room is None
    thread.delete.assert_awaited_once()
    row = await stored(team_draft_id)
    assert row.thread_id is None and row.control_message_id is None


@pytest.mark.asyncio
async def test_start_draft_offers_the_picker_pre_named(test_db):
    from team_draft_view import start_team_draft

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    interaction = make_interaction(make_guild())

    with patch("modals.CubeDraftSelectionView") as picker:
        await start_team_draft(interaction, team_draft_id)

    assert picker.call_args.kwargs["session_type"] == "premade"
    assert picker.call_args.kwargs["session_details_overrides"] == {
        "team_a_name": "Team Garruk", "team_b_name": "Ash, Bo", "team_draft_id": team_draft_id}
    sent = interaction.response.send_message.await_args.kwargs
    assert sent["ephemeral"] is True
    assert sent["view"] is picker.return_value
    picker.return_value.mark_once_sent.assert_called_once_with(interaction)


@pytest.mark.asyncio
async def test_start_draft_after_a_draft_exists_shows_the_lobby_and_no_picker(test_db):
    from team_draft_view import start_team_draft

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    async with db_session() as session:
        session.add(DraftSession(session_id="s-1", guild_id="1", draft_channel_id="60",
                                 message_id="61", session_type="premade"))
        row = await session.get(TeamDraft, team_draft_id)
        row.draft_session_id = "s-1"
    interaction = make_interaction(make_guild())

    with patch("modals.CubeDraftSelectionView") as picker:
        await start_team_draft(interaction, team_draft_id)

    picker.assert_not_called()
    interaction.response.send_message.assert_not_awaited()
    # The control message is re-rendered for everyone, the reason goes to the presser.
    assert "Draft started" in interaction.response.edit_message.await_args.kwargs["content"]
    block = interaction.followup.send.await_args
    assert "https://discord.com/channels/1/60/61" in block.args[0]
    assert block.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_start_draft_on_a_vanished_matchup_says_so(test_db):
    from team_draft_view import start_team_draft

    interaction = make_interaction(make_guild())
    await start_team_draft(interaction, 4242)

    assert "no longer exists" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_deleted_role_reads_as_deleted_role_on_the_control_message(test_db):
    from team_draft_view import open_team_draft_room

    team_draft_id = await seed_team_draft()
    channel, thread, _control = make_channel(make_guild(roles={}))

    await open_team_draft_room(channel, team_draft_id)

    body = thread.send.await_args.kwargs["content"]
    assert body.startswith("**deleted role vs Ash, Bo**")


@pytest.mark.asyncio
async def test_a_missing_member_reads_as_their_id(test_db):
    from team_draft_view import team_draft_facts

    team_draft_id = await seed_team_draft()
    async with db_session() as session:
        facts = await team_draft_facts(session, team_draft_id, make_guild(members={201: "Ash"}))

    assert facts is not None
    assert facts.b.label == "Ash, 202"
    assert facts.set_up_by == "300"


def _details(session_id, team_draft_id):
    from models.session_details import SessionDetails

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 1
    details = SessionDetails(interaction)
    details.session_id = session_id
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Team Garruk"
    details.team_b_name = "Ash, Bo"
    details.team_draft_id = team_draft_id
    return details


@pytest.mark.asyncio
async def test_two_starts_create_one_draft(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    first, second = make_interaction(make_guild()), make_interaction(make_guild())

    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()) as base, \
         patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh, \
         patch("tournament_nudge.post_premade_nudge", AsyncMock()) as nudge:
        await PremadeSession(_details("s-1", team_draft_id)).create_draft_session(first, MagicMock())
        await PremadeSession(_details("s-2", team_draft_id)).create_draft_session(second, MagicMock())

    base.assert_awaited_once()
    refresh.assert_awaited_once_with(first.client, team_draft_id)
    nudge.assert_not_awaited()
    first.response.send_message.assert_not_awaited()
    refused = second.response.send_message.await_args
    assert "already" in refused.args[0]
    assert refused.kwargs["ephemeral"] is True
    assert (await stored(team_draft_id)).draft_session_id == "s-1"


@pytest.mark.asyncio
async def test_a_refused_start_links_the_lobby_of_the_draft_that_won(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    async with db_session() as session:
        session.add(DraftSession(session_id="s-1", guild_id="1", draft_channel_id="60",
                                 message_id="61", session_type="premade"))
        row = await session.get(TeamDraft, team_draft_id)
        row.draft_session_id = "s-1"
    interaction = make_interaction(make_guild())

    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()) as base:
        await PremadeSession(_details("s-2", team_draft_id)).create_draft_session(
            interaction, MagicMock())

    base.assert_not_awaited()
    assert "https://discord.com/channels/1/60/61" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_failed_draft_creation_frees_the_matchup(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    interaction = make_interaction(make_guild())

    with patch("sessions.base_session.BaseSession.create_draft_session",
               AsyncMock(side_effect=RuntimeError("draftmancer down"))), \
         patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh:
        with pytest.raises(RuntimeError):
            await PremadeSession(_details("s-1", team_draft_id)).create_draft_session(
                interaction, MagicMock())

    refresh.assert_not_awaited()
    assert (await stored(team_draft_id)).draft_session_id is None


@pytest.mark.asyncio
async def test_a_draft_that_fails_after_it_was_saved_keeps_the_matchup(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    interaction = make_interaction(make_guild())

    async def saved_then_fails(_self, _interaction, _bot):
        # The base create commits the row and posts the lobby, then a later
        # step (message info, sticky) raises.
        async with db_session() as session:
            session.add(DraftSession(session_id="s-1", guild_id="1", draft_channel_id="60",
                                     message_id="61", session_type="premade"))
        raise RuntimeError("sticky failed")

    with patch("sessions.base_session.BaseSession.create_draft_session", saved_then_fails), \
         patch("team_draft_view.refresh_team_draft_control", AsyncMock()):
        with pytest.raises(RuntimeError):
            await PremadeSession(_details("s-1", team_draft_id)).create_draft_session(
                interaction, MagicMock())

    assert (await stored(team_draft_id)).draft_session_id == "s-1"


@pytest.mark.asyncio
async def test_a_failing_refresh_never_breaks_the_draft(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    interaction = make_interaction(make_guild())

    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()), \
         patch("team_draft_view.refresh_team_draft_control",
               AsyncMock(side_effect=RuntimeError("discord down"))):
        await PremadeSession(_details("s-1", team_draft_id)).create_draft_session(
            interaction, MagicMock())

    assert (await stored(team_draft_id)).draft_session_id == "s-1"


@pytest.mark.asyncio
async def test_refresh_edits_the_control_message_in_place(test_db):
    from team_draft_view import TeamDraftControlView, refresh_team_draft_control

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    message = MagicMock()
    message.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread

    await refresh_team_draft_control(bot, team_draft_id)

    thread.fetch_message.assert_awaited_once_with(777)
    edit = message.edit.await_args.kwargs
    assert edit["content"].startswith("**Team Garruk vs Ash, Bo**")
    assert isinstance(edit["view"], TeamDraftControlView)


@pytest.mark.asyncio
async def test_refresh_of_a_deleted_control_message_is_logged_not_raised(test_db):
    from team_draft_view import refresh_team_draft_control

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message"))
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread

    await refresh_team_draft_control(bot, team_draft_id)


@pytest.mark.asyncio
async def test_room_context_pre_names_and_blocks_once_started(test_db):
    from team_draft_view import team_draft_room_context

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    async with db_session() as session:
        assert await team_draft_room_context(session, 12345, make_guild()) is None
        context = await team_draft_room_context(session, 900, make_guild())
    assert context == (team_draft_id, {
        "team_a_name": "Team Garruk", "team_b_name": "Ash, Bo",
        "team_draft_id": team_draft_id}, None)

    async with db_session() as session:
        (await session.get(TeamDraft, team_draft_id)).draft_session_id = "gone"
    async with db_session() as session:
        _id, _overrides, block = await team_draft_room_context(session, 900, make_guild())
    assert block is not None and "already been played" in block


@pytest.mark.asyncio
async def test_restart_re_registers_every_posted_control_message(test_db):
    from team_draft_view import TeamDraftControlView, re_register_team_draft_views

    posted = await seed_team_draft(thread_id="900", control_message_id="777")
    await seed_team_draft()  # never got a room
    bot = MagicMock()

    await re_register_team_draft_views(bot)

    bot.add_view.assert_called_once()
    view = bot.add_view.call_args.args[0]
    assert isinstance(view, TeamDraftControlView)
    assert bot.add_view.call_args.kwargs["message_id"] == 777
    assert [c.custom_id for c in view.children] == [f"team_draft_start:{posted}"]
