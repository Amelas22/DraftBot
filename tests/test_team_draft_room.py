"""A team draft's thread: opening it, its control message and the one-shot Start draft."""
from datetime import datetime, timezone
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


@pytest.fixture(autouse=True)
def scheduler_on():
    """Team drafts only run where the scheduler is turned on."""
    with patch("team_draft_view.scheduler_enabled", return_value=True), \
         patch("helpers.booking_room.scheduler_enabled", return_value=True):
        yield


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
    assert sent["allowed_mentions"].to_dict()["roles"] == [111]
    assert "roles" not in sent["allowed_mentions"].to_dict()["parse"]
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
    # The winner's draft is mocked out, so it never gets a row: still being set up.
    assert "being set up right now" in refused.args[0]
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
        session.add(DraftSession(session_id="s-ran", guild_id="1", session_type="premade",
                                 session_stage="pairings"))
        (await session.get(TeamDraft, team_draft_id)).draft_session_id = "s-ran"
    async with db_session() as session:
        _id, _overrides, block = await team_draft_room_context(session, 900, make_guild())
    assert block is not None and "already been played" in block


async def _claim(team_draft_id, draft=None):
    async with db_session() as session:
        if draft is not None:
            session.add(draft)
        (await session.get(TeamDraft, team_draft_id)).draft_session_id = "s-new"


async def _start_block(team_draft_id):
    from team_draft_view import start_team_draft

    interaction = make_interaction(make_guild())
    with patch("modals.CubeDraftSelectionView") as picker:
        await start_team_draft(interaction, team_draft_id)
    picker.assert_not_called()
    return (interaction.response.edit_message.await_args.kwargs["content"],
            interaction.followup.send.await_args.args[0])


@pytest.mark.asyncio
async def test_a_claim_whose_draft_row_is_not_saved_yet_is_being_set_up(test_db):
    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    await _claim(team_draft_id)

    body, block = await _start_block(team_draft_id)

    assert "being set up right now" in block and "played" not in block
    assert "being set up" in body and "played" not in body


@pytest.mark.asyncio
async def test_a_saved_draft_without_its_lobby_yet_is_being_set_up(test_db):
    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    await _claim(team_draft_id, DraftSession(session_id="s-new", guild_id="1",
                                             session_type="premade"))

    _body, block = await _start_block(team_draft_id)

    assert "being set up right now" in block


@pytest.mark.asyncio
async def test_a_draft_that_ran_and_lost_its_lobby_was_played(test_db):
    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    await _claim(team_draft_id, DraftSession(session_id="s-new", guild_id="1",
                                             session_type="premade",
                                             teams_start_time=datetime(2026, 10, 26, 19, 0)))

    body, block = await _start_block(team_draft_id)

    assert "already been played" in block
    assert "Draft played." in body


@pytest.mark.asyncio
async def test_a_refused_start_while_the_winner_is_being_set_up_says_so(test_db):
    from sessions.premade_session import PremadeSession

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    await _claim(team_draft_id)
    interaction = make_interaction(make_guild())

    with patch("sessions.base_session.BaseSession.create_draft_session", AsyncMock()):
        await PremadeSession(_details("s-2", team_draft_id)).create_draft_session(
            interaction, MagicMock())

    assert "being set up right now" in interaction.response.send_message.await_args.args[0]


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
    assert [c.custom_id for c in view.children] == [
        f"team_draft_time:{posted}", f"team_draft_start:{posted}"]


def _press_interaction(guild, user_id=201):
    interaction = make_interaction(guild)
    interaction.user = SimpleNamespace(id=user_id)
    return interaction


def _time_button(view):
    return next(c for c in view.children if c.custom_id.startswith("team_draft_time:"))


@pytest.mark.asyncio
async def test_find_a_time_hands_out_a_link_for_this_matchup(test_db):
    from team_draft_view import TeamDraftControlView

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    view = TeamDraftControlView(team_draft_id)
    assert [c.custom_id for c in view.children] == [
        f"team_draft_time:{team_draft_id}", f"team_draft_start:{team_draft_id}"]
    interaction = _press_interaction(make_guild())
    link = AsyncMock(return_value="https://draftbot.fyi/#tok")

    with patch("helpers.booking_room.issue_link", link):
        await _time_button(view).callback(interaction)

    args, kwargs = link.await_args
    assert args[:3] == ("201", "1", "team_draft")
    assert kwargs == {"subject": str(team_draft_id)}
    reply = interaction.response.send_message.await_args
    assert reply.kwargs["ephemeral"] is True
    assert "7 days" in reply.args[0]
    [button] = reply.kwargs["view"].children
    assert button.url == "https://draftbot.fyi/#tok"


@pytest.mark.asyncio
async def test_find_a_time_says_so_when_the_link_cannot_be_made(test_db):
    from helpers.matchup import TIME_LINK_FAILED
    from team_draft_view import TeamDraftControlView

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    interaction = _press_interaction(make_guild())

    with patch("helpers.booking_room.issue_link", AsyncMock(side_effect=RuntimeError("db"))):
        await _time_button(TeamDraftControlView(team_draft_id)).callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(TIME_LINK_FAILED, ephemeral=True)


@pytest.mark.asyncio
async def test_find_a_time_is_off_where_the_scheduler_is_off(test_db):
    from team_draft_view import NOT_ENABLED, TeamDraftControlView

    interaction = _press_interaction(make_guild())
    link = AsyncMock()
    with patch("helpers.booking_room.scheduler_enabled", return_value=False), \
         patch("helpers.booking_room.issue_link", link):
        await _time_button(TeamDraftControlView(5)).callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(NOT_ENABLED, ephemeral=True)
    link.assert_not_awaited()


def _booking_change(old=None, new=datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc)):
    from services.booking import BookingChange
    return BookingChange(True, None, old, new, "Bo", 1)


@pytest.mark.asyncio
async def test_announce_posts_in_the_thread_then_refreshes(test_db):
    from helpers.matchup import booking_announcement
    from team_draft_view import announce_booking

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    order = []
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(side_effect=lambda **kw: order.append("send"))
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread
    refresh = AsyncMock(side_effect=lambda *a: order.append("refresh"))
    change = _booking_change()

    with patch("team_draft_view.refresh_team_draft_control", refresh):
        await announce_booking(bot, team_draft_id, change)

    bot.get_channel.assert_called_once_with(900)
    sent = thread.send.await_args.kwargs
    async with db_session() as session:
        from team_draft_view import team_draft_facts
        facts = await team_draft_facts(session, team_draft_id, make_guild())
    assert sent["content"] == booking_announcement("Bo", None, change.new, facts.a, facts.b)
    assert sent["content"].startswith("<@&111> <@201> <@202>\n")
    mentions = sent["allowed_mentions"]
    assert (mentions.everyone, mentions.users) == (False, True)
    assert mentions.to_dict()["roles"] == [111]
    assert "roles" not in mentions.to_dict()["parse"]
    refresh.assert_awaited_once_with(bot, team_draft_id)
    assert order == ["send", "refresh"]


@pytest.mark.asyncio
async def test_announce_with_a_deleted_thread_logs_and_returns(test_db):
    from team_draft_view import announce_booking

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Channel"))
    refresh = AsyncMock()

    with patch("team_draft_view.refresh_team_draft_control", refresh):
        await announce_booking(bot, team_draft_id, _booking_change())

    bot.fetch_channel.assert_awaited_once_with(900)
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_announce_that_discord_refuses_still_refreshes(test_db):
    from team_draft_view import announce_booking

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread
    refresh = AsyncMock()

    with patch("team_draft_view.refresh_team_draft_control", refresh):
        await announce_booking(bot, team_draft_id, _booking_change())

    refresh.assert_awaited_once_with(bot, team_draft_id)


@pytest.mark.asyncio
async def test_announce_for_a_vanished_matchup_does_nothing(test_db):
    from team_draft_view import announce_booking

    bot = MagicMock()
    refresh = AsyncMock()
    with patch("team_draft_view.refresh_team_draft_control", refresh):
        await announce_booking(bot, 4242, _booking_change())

    bot.get_channel.assert_not_called()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_side_rosters_expand_roles_drop_bots_and_skip_deleted_roles(test_db):
    from helpers.matchup import Roster
    from team_draft_view import side_rosters

    async with db_session() as session:
        row = await create_team_draft(
            session, "1", "50", "Garruk vs Ash",
            Picks(role_ids=("111", "112"), user_ids=()),   # 112 has been deleted
            Picks(role_ids=("113",), user_ids=("201",)),
            created_by="300", now=NOW,
        )
        team_draft_id = row.id

    def member(uid, name, bot=False):
        return SimpleNamespace(id=uid, display_name=name, bot=bot)

    people = {
        201: member(201, "Ash"), 202: member(202, "Bo"), 203: member(203, "Cy"),
        900: member(900, "Robo", bot=True),
    }
    roles = {
        111: SimpleNamespace(name="Team Garruk",
                             members=[people[202], people[201], people[900]]),
        113: SimpleNamespace(name="Team Ash", members=[people[203], people[202]]),
    }
    guild = MagicMock()
    guild.get_role.side_effect = lambda rid: roles.get(rid)
    guild.get_member.side_effect = lambda uid: people.get(uid)
    bot = MagicMock()
    bot.get_guild.return_value = guild

    a, b = await side_rosters(bot, "1", team_draft_id)

    # 201 is named on side B, so B keeps them; 202 is in both roles, so A (first) wins.
    assert a == Roster(label="Team Garruk, deleted role", people=(("202", "Bo"),))
    assert b == Roster(label="Team Ash, Ash", people=(("201", "Ash"), ("203", "Cy")))


@pytest.mark.asyncio
async def test_rebooking_the_same_time_pings_nobody_but_still_refreshes(test_db):
    from team_draft_view import announce_booking

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread
    same = datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc)

    with patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh:
        await announce_booking(bot, team_draft_id, _booking_change(old=same, new=same))

    thread.send.assert_not_awaited()
    refresh.assert_awaited_once_with(bot, team_draft_id)


@pytest.mark.asyncio
async def test_announce_renders_a_booking_committed_while_the_post_was_sending(test_db):
    """The real refresh path: the control message shows the database as it is
    after the send, not the facts read before it."""
    from sqlalchemy import update

    from helpers.matchup import discord_time
    from team_draft_view import announce_booking

    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    first = datetime(2026, 10, 28, 19, 30)
    newer = datetime(2026, 10, 29, 20, 0)
    async with db_session() as session:
        await session.execute(update(TeamDraft).where(TeamDraft.id == team_draft_id)
                              .values(booked_start_utc=first, booked_by_name="Bo"))

    async def another_booking_lands(**_kw):
        async with db_session() as session:
            await session.execute(update(TeamDraft).where(TeamDraft.id == team_draft_id)
                                  .values(booked_start_utc=newer, booked_by_name="Ash"))

    message = MagicMock()
    message.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(side_effect=another_booking_lands)
    thread.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_guild.return_value = make_guild()
    bot.get_channel.return_value = thread

    await announce_booking(bot, team_draft_id, _booking_change(new=first.replace(tzinfo=timezone.utc)))

    content = message.edit.await_args.kwargs["content"]
    assert discord_time(newer) in content
    assert discord_time(first) not in content
