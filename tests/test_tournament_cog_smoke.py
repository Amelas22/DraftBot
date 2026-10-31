"""Smoke tests for cogs/tournament_commands.py (Slice 1)."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.permissions import is_bot_manager


def test_cog_imports_and_setup_registers():
    from cogs.tournament_commands import TournamentCog, setup

    bot = MagicMock()
    setup(bot)
    bot.add_cog.assert_called_once()
    assert isinstance(bot.add_cog.call_args.args[0], TournamentCog)


def test_tournament_group_has_slice_one_and_two_commands():
    from cogs.tournament_commands import TournamentCog

    subcommands = {cmd.name for cmd in TournamentCog.tournament.subcommands}
    assert {"create", "register", "status",
            "start", "set_result", "next_round", "finish",
            "add_team", "remove_team", "add_match", "refresh_standings",
            "recover_draft", "open_rooms", "sync_roles"} <= subcommands


def test_admin_commands_are_gated_by_bot_manager_check():
    from cogs.tournament_commands import TournamentCog

    for command in ("create", "start", "set_result", "next_round", "finish",
                    "add_team", "remove_team", "add_match", "refresh_standings",
                    "recover_draft", "open_rooms", "sync_roles"):
        assert is_bot_manager in getattr(TournamentCog, command).checks, command


def test_register_and_status_are_open_to_everyone():
    from cogs.tournament_commands import TournamentCog

    assert is_bot_manager not in TournamentCog.register.checks
    assert is_bot_manager not in TournamentCog.status.checks


def test_recorded_result_line_formats_score():
    from helpers.match_control import recorded_result_line

    line = recorded_result_line("Latecomers", "Strixhaven Dropouts", 5, 4)
    assert line == "✅ Result recorded: **Latecomers** 5–4 **Strixhaven Dropouts**"


def test_register_replies_are_all_ephemeral():
    """The board is the public record; a captain's confirmations are private, so no
    reply in register may omit ephemeral=True (a public reply could contradict the
    board later — e.g. a '✅ registered' message left in scrollback after a drop)."""
    import ast
    import inspect
    import textwrap

    from cogs.tournament_commands import TournamentCog

    src = textwrap.dedent(inspect.getsource(TournamentCog.register.callback))
    tree = ast.parse(src)
    sends = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("send", "defer", "respond")
    ]
    assert sends, "expected register to reply to the user"
    for call in sends:
        assert any(kw.arg == "ephemeral" and kw.value.value is True
                   for kw in call.keywords), f"non-ephemeral reply at line {call.lineno}"


@pytest.mark.asyncio
async def test_create_posts_the_board_even_if_the_confirmation_reply_fails(test_db):  # noqa: F811
    """Regression: live testing found the board never posted and board_channel_id/
    board_message_id stayed NULL, with no "Could not post registration board"
    warning in the log. Root cause: the ephemeral confirmation's ctx.followup.send()
    and the post_registration_board() call were sequenced in one unguarded flow —
    a flaky/expired interaction token during the confirmation raised past the
    try/except that was supposed to guard the board post, skipping it entirely and
    escaping create() as an unhandled exception. The board goes to ctx.channel, not
    the interaction, so it must post (and the command must not raise) regardless of
    whether the confirmation reply succeeds."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session
    from models.tournament import Tournament
    from sqlalchemy import select

    cog = TournamentCog(MagicMock())
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    # Simulates the observed HTTPException(40060)/NotFound(10062): the confirmation
    # reply itself fails.
    ctx.followup.send = AsyncMock(side_effect=RuntimeError("interaction already acknowledged"))

    posted_message = MagicMock()
    posted_message.id = 999
    posted_message.channel.id = 777
    ctx.channel = MagicMock()
    ctx.channel.send = AsyncMock(return_value=posted_message)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True):
        await TournamentCog.create.callback(
            cog, ctx, name="Cup", format="swiss", rounds=3, entry_fee=0, payout="winner_take_all",
            cut=None,
        )

    ctx.channel.send.assert_awaited_once()
    async with db_session() as session:
        tournament = (
            await session.execute(select(Tournament).where(Tournament.name == "Cup"))
        ).scalar_one()
        assert tournament.board_channel_id == "777"
        assert tournament.board_message_id == "999"


@pytest.mark.asyncio
async def test_refresh_board_swallows_a_discord_failure():
    """The board is a view, never a source of truth: a Discord failure while
    refreshing it must log and return, not propagate and abort the command that
    changed the roster (a registration, a fee transfer, a tournament start)."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    # patched where the failure originates: the cog delegates to refresh_boards, which
    # owns the guard for every caller (cog, wallet deposit, watchdog)
    with patch(
        "services.tournament_formatter.update_registration_board",
        AsyncMock(side_effect=RuntimeError("discord boom")),
    ):
        await cog._refresh_board(1)  # must not raise


@pytest.mark.asyncio
async def test_start_freezes_the_board():
    """/tournament start must refresh the board, so it stops inviting registrations
    once the schedule is seeded.

    It no longer passes a closed flag: the board derives open-vs-closed from the
    tournament's own status, which start has already moved off "registration".
    That is what keeps a later roster edit -- which also refreshes the board --
    from flipping it back to open. The rendering half is covered by
    test_board_of_a_started_tournament_refreshes_as_closed."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock()
    cog._post_standings = AsyncMock()

    ctx = _ctx()

    res = {"tournament_id": 1, "name": "Cup", "pot": 0, "fee": 0}
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value=res)), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.active_tournament_id", AsyncMock(return_value=None)), \
         patch("cogs.tournament_commands.teams_missing_role", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.ensure_team_roles",
               AsyncMock(return_value={})):
        await TournamentCog.start.callback(cog, ctx)

    cog._refresh_board.assert_awaited_once_with(1)


def test_roster_commands_are_registered():
    from cogs.tournament_commands import TournamentCog

    subcommands = {cmd.name for cmd in TournamentCog.tournament.subcommands}
    assert {"add_teammate", "remove_teammate"} <= subcommands


def test_roster_commands_are_open_to_captains():
    """Captains manage their own roster; the bot-manager gate lives on the optional
    `team` argument instead, so the command itself must not carry the check."""
    from cogs.tournament_commands import TournamentCog

    assert is_bot_manager not in TournamentCog.add_teammate_cmd.checks
    assert is_bot_manager not in TournamentCog.remove_teammate_cmd.checks


def test_playoff_command_is_registered():
    from cogs.tournament_commands import TournamentCog
    names = {c.name for c in TournamentCog.tournament.subcommands}
    assert "playoff" in names


def _playoff_prompt_interaction(is_owner=False):
    """A mock discord.Interaction that passes isinstance checks, for clicking
    a PlayoffPromptView button. Mirrors _component_interaction in
    tests/test_permissions.py."""
    import discord
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client.is_owner = AsyncMock(return_value=is_owner)
    interaction.user.roles = []
    interaction.user.guild_permissions.manage_roles = False
    interaction.guild.id = 123
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize("button_name, expected", [
    ("start_playoff_button", "Cut to the top"),
    ("finish_button", "Finished"),
])
async def test_playoff_prompt_settles_its_message_after_a_successful_answer(button_name, expected):
    """_answer closes the prompt with a "⏳ ..." line before its slow work, and
    _failed replaces that line when the work raises. Success had nothing, so a
    prompt that had been answered went on advertising work in progress -- it
    reads as a hung command to everyone in the channel except the clicker."""
    from cogs.tournament_commands import PlayoffPromptView

    cog = MagicMock()
    cog._run_playoff = AsyncMock(return_value=(MagicMock(), 8))
    cog._run_finish = AsyncMock(return_value="🏁 Complete! Champion: **Alpha** 🏆")
    view = PlayoffPromptView(cog, tournament_id=1, cut_to=8)
    view.message = MagicMock()
    view.message.edit = AsyncMock()

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("helpers.permissions.is_bot_manager", return_value=True):
        interaction = _playoff_prompt_interaction(is_owner=True)
        await getattr(view, button_name).callback(interaction)

    # The last edit is what the channel is left reading.
    assert view.message.edit.await_count >= 1
    final = view.message.edit.await_args.kwargs["content"]
    assert expected in final, final
    assert "⏳" not in final, f"prompt left advertising work in progress: {final!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("button_name", ["start_playoff_button", "finish_button"])
async def test_playoff_prompt_buttons_reject_non_managers(button_name):
    """Regression for the end-of-swiss prompt being posted publicly: without a
    gate, any guild member could click 'Finish now' and do what the role-gated
    /tournament finish does (or force the bracket to start). Both buttons must
    reject a non-manager before their body -- and therefore start_playoff /
    finish_tournament -- ever runs."""
    from cogs.tournament_commands import PlayoffPromptView

    with patch("cogs.tournament_commands.start_playoff", AsyncMock()) as mock_start, \
         patch("cogs.tournament_commands.finish_tournament", AsyncMock()) as mock_finish, \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(MagicMock(), tournament_id=1, cut_to=8)
        button = getattr(view, button_name)
        interaction = _playoff_prompt_interaction(is_owner=False)
        await button.callback(interaction)

    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_called()
    mock_start.assert_not_awaited()
    mock_finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_button_surfaces_a_value_error_instead_of_dying_silently():
    """finish_tournament raises ValueError if the tournament isn't active
    (double-click, or another path finished it first). Without a try/except,
    that falls through to py-cord's default on_error -- a stderr log and a
    failed interaction with no explanation. Must match start_playoff_button's
    handling of the same failure class."""
    from cogs.tournament_commands import PlayoffPromptView, TournamentCog

    tournament = MagicMock()
    tournament.name = "Cup"
    tournament.entry_fee = 0
    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.finish_tournament",
               AsyncMock(side_effect=ValueError("'Cup' is not active."))), \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(TournamentCog(MagicMock()), tournament_id=1, cut_to=8)
        role = MagicMock()
        role.name = "Bot Manager"
        interaction = _playoff_prompt_interaction(is_owner=False)
        interaction.user.roles = [role]

        await view.finish_button.callback(interaction)

    interaction.followup.send.assert_awaited_once()
    msg = interaction.followup.send.call_args.args[0]
    assert msg.startswith("❌")
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True


class _NullSession:
    """Stands in for db_session(): an async context manager yielding a mock,
    whose ``get`` returns ``found`` (the roster commands load their team by id)."""
    def __init__(self, found=None):
        self.found = found

    async def __aenter__(self):
        session = MagicMock()
        session.get = AsyncMock(return_value=self.found)
        return session

    async def __aexit__(self, *_exc):
        return False


def _ctx():
    """An ApplicationContext stand-in for invoking a command callback."""
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.channel.send = AsyncMock()
    return ctx


def _player(user_id=4242):
    """A discord.Member stand-in. `bot` must be explicitly False: a bare
    MagicMock attribute is truthy, and add_teammate_cmd rejects bots.
    `display_name` must be a real string too: get_display_name() passes it to
    discord.utils.escape_markdown(), which needs a str, not a MagicMock."""
    player = MagicMock()
    player.id = user_id
    player.bot = False
    player.display_name = f"Player{user_id}"
    return player


def _roster_command_open(participant):
    """Patch everything the roster commands touch before the role sync.

    Without this they read the real drafts.db. `refresh_boards` and
    `other_teams_for_user` are patched because both run with the mock session
    and would otherwise await a MagicMock.
    """
    from cogs.tournament_commands import TournamentCog

    stack = ExitStack()
    stack.enter_context(patch("cogs.tournament_commands.tournament_enabled",
                              return_value=True))
    stack.enter_context(patch("cogs.tournament_commands.db_session",
                              lambda: _NullSession()))
    stack.enter_context(patch("cogs.tournament_commands.get_active_tournament",
                              AsyncMock(return_value=MagicMock(id=1, name="Cup"))))
    stack.enter_context(patch.object(TournamentCog, "_roster_target",
                                     AsyncMock(return_value=participant)))
    stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
    # add_teammate's work lives in the shared registration service.
    stack.enter_context(patch("services.tournament_registration.db_session",
                              lambda: _NullSession(participant)))
    stack.enter_context(patch("services.tournament_registration.other_teams_for_user",
                              AsyncMock(return_value=[])))
    stack.enter_context(patch("services.tournament_registration.refresh_boards", AsyncMock()))
    # The role work opens its own sessions and calls Discord. A team with a role keeps
    # it; one without gets "777" created for it.
    stack.enter_context(patch("services.tournament_registration.ensure_team_role",
                              AsyncMock(return_value=participant.role_id or "777")))
    stack.enter_context(patch("services.tournament_registration.grant_league_roles",
                              AsyncMock(return_value=True)))
    return stack


async def _start_with_real_roles(cog, ctx, tournament_id):
    from cogs.tournament_commands import TournamentCog

    res = {"tournament_id": tournament_id, "name": "Cup", "fee": 0, "pot": 0}
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value=res)):
        await TournamentCog.start.callback(cog, ctx)


def _start_cog():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock()
    cog._post_standings = AsyncMock()
    return cog


def _role_guild(ctx):
    """ctx.guild whose create_role hands out ids and whose members are all gone."""
    made = []

    async def create_role(name, **kwargs):
        role = MagicMock()
        role.id = 9000 + len(made)
        made.append(role)
        return role

    ctx.guild.create_role = AsyncMock(side_effect=create_role)
    ctx.guild.get_member = MagicMock(return_value=None)
    ctx.guild.get_role = MagicMock(return_value=MagicMock())
    return made


@pytest.mark.asyncio
async def test_start_fills_a_team_without_a_role_and_leaves_others_alone(test_db):  # noqa: F811
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=0)
    await _set_role_id(t_id, "Alpha", "555")
    cog = _start_cog()
    ctx = _ctx()
    made = _role_guild(ctx)

    await _start_with_real_roles(cog, ctx, t_id)

    assert ctx.guild.create_role.await_count == 1
    assert await _role_id_of(t_id, "Alpha") == "555"
    assert await _role_id_of(t_id, "Bravo") == str(made[0].id)


@pytest.mark.asyncio
async def test_start_survives_role_creation_failing(test_db):  # noqa: F811
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=0)
    cog = _start_cog()
    ctx = _ctx()
    ctx.guild.create_role = AsyncMock(side_effect=RuntimeError("discord boom"))

    await _start_with_real_roles(cog, ctx, t_id)

    cog._refresh_board.assert_awaited_once_with(t_id)
    cog._post_schedule.assert_awaited_once()
    cog._post_standings.assert_awaited_once()
    sent = [c.args[0] for c in ctx.followup.send.await_args_list]
    assert any("has started" in text for text in sent)


@pytest.mark.asyncio
async def test_start_closes_its_session_before_calling_discord():
    """Filling roles is many sequential Discord calls. Holding a SQLite session
    open across them blocks every other writer for the duration. A mocked
    session cannot show this, so this test uses one that reports its real state."""
    from cogs.tournament_commands import TournamentCog

    open_sessions = []

    class _WatchedSession:
        """Reports whether it is still open, the way a real session would."""
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            open_sessions.append(self)
            return MagicMock()

        async def __aexit__(self, *exc):
            self.closed = True
            return False

    was_open_during_discord = []

    async def _ensure(guild, tournament_id):
        was_open_during_discord.append(
            any(not sess.closed for sess in open_sessions))
        return {}

    cog = _start_cog()
    ctx = _ctx()
    res = {"tournament_id": 1, "name": "Cup", "fee": 0, "pot": 0}

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _WatchedSession()), \
         patch("cogs.tournament_commands.active_tournament_id", AsyncMock(return_value=None)), \
         patch("cogs.tournament_commands.teams_missing_role", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value=res)), \
         patch("cogs.tournament_commands.ensure_team_roles", _ensure):
        await TournamentCog.start.callback(cog, ctx)

    assert was_open_during_discord, "ensure_team_roles was never reached"
    assert not any(was_open_during_discord), (
        "a database session was still open while creating roles on Discord")


@pytest.mark.asyncio
@pytest.mark.parametrize("button_name,other_name", [
    ("start_playoff_button", "finish_button"),
    ("finish_button", "start_playoff_button"),
])
async def test_playoff_prompt_closes_before_doing_the_slow_work(button_name, other_name):
    """The prompt is public and its work is slow -- starting the bracket posts a
    pairing message, a thread and a control message per match. While that ran,
    BOTH buttons stayed dispatchable: a manager who saw nothing happen (or a
    second one answering the same prompt) could hit "Finish now" and
    irreversibly complete the tournament mid-bracket. Each handler must disable
    both items and say what was taken BEFORE any of that work starts."""
    from cogs.tournament_commands import PlayoffPromptView

    seen = {}

    async def _record(*_a, **_k):
        seen["disabled"] = [item.disabled for item in view.children]
        return MagicMock()      # a TournamentRound / champion stand-in

    cog = MagicMock()
    # Both buttons hand off to one cog launcher each; those are what must find
    # the prompt already closed.
    cog._run_playoff = AsyncMock(side_effect=_record)
    cog._run_finish = AsyncMock(side_effect=_record)

    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.start_playoff", AsyncMock(side_effect=_record)), \
         patch("cogs.tournament_commands.finish_tournament", AsyncMock(side_effect=_record)), \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(cog, tournament_id=1, cut_to=4)
        role = MagicMock()
        role.name = "Bot Manager"
        interaction = _playoff_prompt_interaction(is_owner=False)
        interaction.user.roles = [role]

        await getattr(view, button_name).callback(interaction)

    # The prompt itself was rewritten, carrying the disabled view.
    interaction.response.edit_message.assert_awaited_once()
    assert interaction.response.edit_message.call_args.kwargs["view"] is view
    # ...and by the time the service call ran, neither option was clickable.
    assert seen["disabled"] == [True, True], (
        f"{other_name} was still live while {button_name} was working")


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible,expected", [(1, False), (3, True)])
async def test_short_field_only_suggests_a_playoff_size_the_option_accepts(eligible, expected):
    """`top:` is min_value=2, so `/tournament playoff top:1` is a command
    Discord refuses to send. With too few teams for the declared cut, the
    remedy offered must be one the TO can actually type."""
    from cogs.tournament_commands import TournamentCog
    from services.tournament_service import SwissComplete

    cog = TournamentCog.__new__(TournamentCog)
    tournament = MagicMock()
    tournament.id, tournament.name = 1, "Cup"

    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=tournament)), \
         patch.object(TournamentCog, "_refuse_missing_roles", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands._playoff_rounds", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.advance_round",
               AsyncMock(side_effect=SwissComplete(4, eligible, eligible >= 4))):
        await TournamentCog.next_round.callback(cog, ctx)

    message = ctx.followup.send.call_args.args[0]
    assert f"Only **{eligible}**" in message           # the warning still fires
    assert (f"top:{eligible}" in message) is expected


def _fake_db_session(session_stub):
    """A db_session() stand-in yielding a prepared session double."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake():
        yield session_stub
    return fake


def _session_stub(tournament):
    stub = MagicMock()
    stub.get = AsyncMock(return_value=tournament)
    return stub


def _manager_interaction():
    interaction = _playoff_prompt_interaction(is_owner=False)
    role = MagicMock()
    role.name = "Bot Manager"
    interaction.user.roles = [role]
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["round_robin", "manual"])
async def test_create_refuses_a_cut_on_a_format_that_can_never_cut(fmt):
    """start_playoff seeds a cut from Swiss standings and refuses every other
    format, but creation stored cut_to regardless -- so the registration board
    advertised a cut that could never be run. Refuse at creation instead of
    silently dropping the value."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog.__new__(TournamentCog)
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.post_registration_board", AsyncMock()), \
         patch("cogs.tournament_commands.create_tournament", AsyncMock()) as create:
        await TournamentCog.create.callback(
            cog, ctx, name="Cup", format=fmt, rounds=None, entry_fee=0,
            payout="winner_take_all", cut=8,
        )

    create.assert_not_awaited()
    message = ctx.followup.send.call_args.args[0]
    assert message.startswith("❌") and "swiss" in message.lower()


@pytest.mark.asyncio
async def test_start_playoff_button_refreshes_the_pinned_standings():
    """Every refresh path is routed through the pinned standings window. The
    prompt's Start button was not: answering the prompt posted the bracket while
    the pinned window still showed the last Swiss round."""
    from cogs.tournament_commands import PlayoffPromptView, TournamentCog

    cog = TournamentCog(MagicMock())
    play = MagicMock()
    play.send = AsyncMock(return_value=MagicMock(id=88, pin=AsyncMock()))
    cog._destination = MagicMock(return_value=play)
    tournament = MagicMock()
    tournament.cut_to = 4

    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.start_playoff",
               AsyncMock()), \
         patch("cogs.tournament_commands.bracket_rows", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.create_bracket_embed", MagicMock()), \
         patch("cogs.tournament_commands.open_ready_bracket_matches",
               AsyncMock()) as open_rooms, \
         patch("cogs.tournament_commands.update_standings_message",
               AsyncMock()) as refresh, \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(cog, tournament_id=7, cut_to=4)
        await view.start_playoff_button.callback(_manager_interaction())

    open_rooms.assert_awaited_once_with(cog.bot, 7)
    refresh.assert_awaited_once_with(cog.bot, 7)


@pytest.mark.asyncio
async def test_finish_button_gives_the_payout_hint_and_refreshes_standings():
    """The prompt is the path a TO answers by default, so it must not be the
    poorer one. The button's inlined two-liner dropped the payout hint -- on a
    money tournament the only prompt that says to pay it out -- along with the
    standings refresh and the tournament's name and id."""
    from cogs.tournament_commands import PlayoffPromptView, TournamentCog

    cog = TournamentCog(MagicMock())
    tournament = MagicMock()
    tournament.name = "Cup"
    tournament.entry_fee = 5
    champion = MagicMock()
    champion.team_name = "Alpha"

    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.finish_tournament",
               AsyncMock(return_value=champion)), \
         patch("cogs.tournament_commands.escrow.prize_pool", AsyncMock(return_value=40)), \
         patch("cogs.tournament_commands.escrow.is_paid_out", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands.update_standings_message",
               AsyncMock()) as refresh, \
         patch("helpers.permissions.get_config", return_value={}), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(return_value=set())):
        # Round 2 review, non-blocking 2: this test previously left
        # _drop_team_roles's real list_participants call to run against the
        # MagicMock session_stub, raising internally on every run -- a defect
        # invisible here because _drop_team_roles never raises. Patching both
        # is what makes this test exercise the real cleanup path instead of a
        # silently-broken one.
        view = PlayoffPromptView(cog, tournament_id=7, cut_to=4)
        interaction = _manager_interaction()
        await view.finish_button.callback(interaction)

    message = interaction.followup.send.call_args.args[0]
    assert "Prize pool: **40 tix**" in message
    assert "/tournament payout" in message
    assert "**Cup** (#7)" in message
    assert "Champion: **Alpha**" in message
    refresh.assert_awaited_once_with(cog.bot, 7)


@pytest.mark.asyncio
async def test_playoff_prompt_says_so_when_it_expires():
    """timeout=900 with no on_timeout left two live-looking buttons behind: a TO
    coming back later clicked one and got Discord's generic "interaction failed".
    The expired prompt must name the commands that still work."""
    from cogs.tournament_commands import PlayoffPromptView

    view = PlayoffPromptView(MagicMock(), tournament_id=1, cut_to=8)
    view.message = MagicMock()
    view.message.edit = AsyncMock()

    await view.on_timeout()

    content = view.message.edit.call_args.kwargs["content"]
    assert "/tournament playoff top:8" in content
    assert "/tournament finish" in content
    assert view.message.edit.call_args.kwargs["view"] is None
    assert all(item.disabled for item in view.children)


@pytest.mark.asyncio
async def test_adding_a_teammate_gives_them_the_team_role():
    """Rosters stay editable while a tournament runs -- _assert_roster_editable
    only locks a COMPLETED one -- so a mid-event roster change must move the
    role too, or the new player never gets pulled into their match room."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("services.tournament_registration.add_teammate",
               AsyncMock(return_value=(MagicMock(), True))), \
         patch("services.tournament_registration.sync_member", AsyncMock()) as sync:
        await TournamentCog.add_teammate_cmd.callback(cog, ctx, player=player, team=None)

    sync.assert_awaited_once()
    assert sync.await_args.args[0] is ctx.guild
    assert sync.await_args.args[1] == "555"
    # The actual added PLAYER, never the captain running the command -- both
    # are in scope on every line here, which is exactly the confusion a
    # `player`/`ctx.author` mix-up would slip past unnoticed.
    assert sync.await_args.args[2] == str(player.id)
    assert sync.await_args.kwargs["add"] is True
    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply and "Alpha" in reply


@pytest.mark.asyncio
async def test_re_adding_an_existing_teammate_still_repairs_their_role():
    """add_teammate returning created=False (they were already on the roster)
    must not skip the sync: re-running the command is the documented way to
    repair a role a player somehow lost, and that only works if the sync
    fires on the "already on the roster" reply too, not just on a fresh add."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("services.tournament_registration.add_teammate",
               AsyncMock(return_value=(MagicMock(), False))), \
         patch("services.tournament_registration.sync_member", AsyncMock()) as sync:
        await TournamentCog.add_teammate_cmd.callback(cog, ctx, player=player, team=None)

    sync.assert_awaited_once()
    assert sync.await_args.kwargs["add"] is True
    reply = ctx.followup.send.call_args.args[0]
    assert "already on" in reply


@pytest.mark.asyncio
async def test_add_teammate_on_a_team_without_a_role_gets_the_role_created():
    """A team with no role (tournament not started, or one that predates roles)
    gets one made by ensure_team_role, and the new member receives it -- no
    warning, since nothing failed."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = None
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("services.tournament_registration.add_teammate",
               AsyncMock(return_value=(MagicMock(), True))), \
         patch("services.tournament_registration.sync_member", AsyncMock(return_value=True)) as sync:
        await TournamentCog.add_teammate_cmd.callback(cog, ctx, player=player, team=None)

    assert sync.await_args.args[1] == "777"
    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply and "Alpha" in reply
    assert "⚠️" not in reply


@pytest.mark.asyncio
async def test_add_teammate_warns_when_the_role_could_not_be_given():
    """sync_member returning False means Discord refused (most commonly:
    the bot's own role sits below the team role). The reply must say so
    instead of a bare '✅ ... roster' that claims a role landed when it
    didn't -- Blocking 5 from round 1 review."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("services.tournament_registration.add_teammate",
               AsyncMock(return_value=(MagicMock(), True))), \
         patch("services.tournament_registration.sync_member", AsyncMock(return_value=False)):
        await TournamentCog.add_teammate_cmd.callback(cog, ctx, player=player, team=None)

    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply           # the roster change itself did succeed
    assert "⚠️" in reply and "Alpha" in reply


@pytest.mark.asyncio
async def test_removing_a_teammate_takes_the_team_role_away():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.sync_member", AsyncMock()) as sync:
        await TournamentCog.remove_teammate_cmd.callback(cog, ctx, player=player, team=None)

    sync.assert_awaited_once()
    assert sync.await_args.args[0] is ctx.guild
    # role_id, not the team name or participant id -- passing either of
    # those makes sync_member do int("Alpha") -> ValueError, after the
    # roster row has already been deleted.
    assert sync.await_args.args[1] == "555"
    assert sync.await_args.args[2] == str(player.id)
    assert sync.await_args.kwargs["add"] is False
    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply and "Alpha" in reply


@pytest.mark.asyncio
async def test_remove_teammate_warns_when_the_role_could_not_be_taken():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.sync_member", AsyncMock(return_value=False)):
        await TournamentCog.remove_teammate_cmd.callback(cog, ctx, player=player, team=None)

    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply
    assert "⚠️" in reply and "Alpha" in reply


@pytest.mark.asyncio
async def test_removing_someone_who_was_not_on_the_roster_leaves_roles_alone():
    """The captain holds the team role but is deliberately NOT in the roster
    table, so remove_teammate returns False for them. Syncing regardless would
    strip the captain's role and drop them out of every future match room."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands.sync_member", AsyncMock()) as sync:
        await TournamentCog.remove_teammate_cmd.callback(
            cog, ctx, player=_player(), team=None)

    sync.assert_not_awaited()
    reply = ctx.followup.send.call_args.args[0]
    assert "isn't on" in reply


@pytest.mark.asyncio
async def test_finishing_deletes_the_team_roles():
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    tournament = SimpleNamespace(id=1, name="Cup", entry_fee=0)
    ctx = _ctx()
    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(return_value=set())) as gone, \
         patch("cogs.tournament_commands.finish_tournament", AsyncMock(return_value=MagicMock())), \
         patch("cogs.tournament_commands.update_standings_message", AsyncMock()), \
         patch.object(TournamentCog, "_drop_team_roles", wraps=cog._drop_team_roles) as drop:
        await cog._run_finish(ctx, 1)
    gone.assert_awaited_once()
    # Round 2 review, non-blocking 4: a wrong guild (e.g. ctx.author, self.bot)
    # passed every prior test since none checked the argument.
    drop.assert_awaited_once_with(ctx.guild, 1)


@pytest.mark.asyncio
async def test_completing_via_next_round_also_deletes_the_team_roles():
    """The second completion path. Wiring only _run_finish leaves roles behind
    on the path most tournaments actually take."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1, name="Cup", total_rounds=3))), \
         patch.object(TournamentCog, "_refuse_missing_roles", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands._playoff_rounds", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.advance_round", AsyncMock(return_value=None)), \
         patch("cogs.tournament_commands.get_final_placement", AsyncMock(return_value=[MagicMock()])), \
         patch("cogs.tournament_commands.update_standings_message", AsyncMock()), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(return_value=set())) as gone, \
         patch.object(TournamentCog, "_drop_team_roles", wraps=cog._drop_team_roles) as drop:
        await TournamentCog.next_round.callback(cog, ctx)
    gone.assert_awaited_once()
    # Round 2 review, non-blocking 4: same coverage gap as the finish path.
    drop.assert_awaited_once_with(ctx.guild, 1)


@pytest.mark.asyncio
async def test_drop_team_roles_deletes_the_real_ids_before_clearing_them():
    """Pins the ordering this task turns on: role_id is the only record that a
    role exists, so if the ids were cleared first, delete_team_roles would be
    called with a list of Nones and every role would be stranded against the
    guild's 250-role cap. The other two tests use tournaments with no
    participants, so a reordering wouldn't touch them -- this one needs real
    participants to observe the order at all."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    guild = MagicMock()
    participants = [
        SimpleNamespace(role_id="111", captain_user_id="1", roster_user_ids=[]),
        SimpleNamespace(role_id="222", captain_user_id="2", roster_user_ids=[]),
    ]
    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=participants)), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock()), \
         patch("cogs.tournament_commands.delete_team_roles",
               AsyncMock(return_value={"111", "222"})) as gone:
        await cog._drop_team_roles(guild, 1)

    gone.assert_awaited_once_with(guild, ["111", "222"])
    assert all(p.role_id is None for p in participants)


@pytest.mark.asyncio
async def test_drop_team_roles_keeps_the_id_of_a_role_that_did_not_delete():
    """Round 2 review, Blocking 1: delete_team_roles can leave a role alive
    (Discord refuses one of 42 deletes) while still returning without
    raising. Clearing every role_id regardless -- which the round-1 code did,
    by discarding the return value entirely -- erases the only record that
    the survivor exists: exactly the stranding the delete-before-clear
    ordering exists to prevent, reintroduced one level up. Only ids that
    delete_team_roles actually confirms gone may be cleared."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    participants = [
        SimpleNamespace(role_id="111", captain_user_id="1", roster_user_ids=[]),
        SimpleNamespace(role_id="222", captain_user_id="2", roster_user_ids=[]),
    ]
    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=participants)), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock()), \
         patch("cogs.tournament_commands.delete_team_roles",
               AsyncMock(return_value={"111"})):  # "222" survived
        await cog._drop_team_roles(MagicMock(), 1)

    assert participants[0].role_id is None
    assert participants[1].role_id == "222"


@pytest.mark.asyncio
async def test_drop_team_roles_logs_instead_of_raising_on_failure():
    """Never raises -- a tournament is over either way -- but a failure
    swallowed with no trace would leave up to 42 roles stranded against the
    guild's 250-role cap with nobody told to go recover them by hand.
    logger.exception is asserted (not a bare logger.warning) because round 2
    review found a bare logger.warning(f"...{e}") gives only str(e) with no
    traceback -- which is exactly how a fully-broken _drop_team_roles (raising
    TypeError against a MagicMock session) passed silently in round 1 inside
    test_finish_button_gives_the_payout_hint_and_refreshes_standings."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.delete_team_roles",
               AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.logger") as log:
        await cog._drop_team_roles(MagicMock(), 1)  # must not raise

    log.exception.assert_called_once()


@pytest.mark.asyncio
async def test_run_finish_refreshes_standings_before_dropping_roles():
    """Round 2 review, non-blocking 5: role cleanup is bookkeeping that must
    never delay or fail the visible parts of finishing a tournament. The
    return-value shape doesn't expose call order on its own, so this pins it
    directly with a shared list."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    tournament = SimpleNamespace(id=1, name="Cup", entry_fee=0)
    order = []
    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.finish_tournament", AsyncMock(return_value=MagicMock())), \
         patch("cogs.tournament_commands.update_standings_message",
               AsyncMock(side_effect=lambda *a, **k: order.append("standings"))), \
         patch.object(TournamentCog, "_drop_team_roles",
                       AsyncMock(side_effect=lambda *a, **k: order.append("cleanup"))):
        await cog._run_finish(_ctx(), 1)

    assert order == ["standings", "cleanup"]


@pytest.mark.asyncio
async def test_next_round_announces_the_champion_before_dropping_roles():
    """Round 2 review, non-blocking 5: the champion message is what the TO is
    waiting for; cleanup must not delay or risk it. Pins the order that
    mutation M6 (moving cleanup ahead of the announcement) slipped past
    undetected in round 1."""
    from cogs.tournament_commands import TournamentCog
    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    order = []
    ctx.followup.send = AsyncMock(side_effect=lambda *a, **k: order.append("announce"))
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1, name="Cup", total_rounds=3))), \
         patch.object(TournamentCog, "_refuse_missing_roles", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands._playoff_rounds", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.advance_round", AsyncMock(return_value=None)), \
         patch("cogs.tournament_commands.get_final_placement", AsyncMock(return_value=[MagicMock()])), \
         patch("cogs.tournament_commands.update_standings_message", AsyncMock()), \
         patch.object(TournamentCog, "_drop_team_roles",
                       AsyncMock(side_effect=lambda *a, **k: order.append("cleanup"))):
        await TournamentCog.next_round.callback(cog, ctx)

    assert order == ["announce", "cleanup"]


# ---- drop_team --------------------------------------------------------------------

def test_drop_team_is_open_to_everyone_and_gates_the_target_itself():
    """A captain drops their own team, so the command cannot carry a blanket
    manager check -- naming someone else's team is what needs the permission,
    and that is checked in the body."""
    from cogs.tournament_commands import TournamentCog

    assert "drop_team" in {cmd.name for cmd in TournamentCog.tournament.subcommands}
    assert is_bot_manager not in TournamentCog.drop_team.checks


@pytest.mark.asyncio
async def test_naming_someone_elses_team_requires_a_manager(test_db):  # noqa: F811
    """Dropping is not a roster edit, but it draws the same line: your own team
    either way, anyone else's only with the role."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session
    from services.tournament_service import (
        create_tournament, register_team, set_result, start_tournament,
    )
    import random

    async with db_session() as session:
        tournament = await create_tournament(session, "123", "Spring", 3)
        await session.commit()
        for i in range(4):
            await register_team(session, tournament.id, f"Team{i}", str(900 + i))
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        for match in matches:
            if not match.is_bye:
                await set_result(session, match.id, 2, 0)
        await session.commit()

    cog = TournamentCog.__new__(TournamentCog)
    ctx = _ctx()  # author 456 captains none of them
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.is_bot_manager",
                                  AsyncMock(return_value=False)))
        await TournamentCog.drop_team.callback(cog, ctx, team="Team0")

    reply = ctx.followup.send.await_args.args[0]
    assert "manager" in reply.lower(), reply

    async with db_session() as session:
        from services.tournament_service import find_participant_by_name
        still_in = await find_participant_by_name(session, tournament.id, "Team0")
        assert still_in.dropped_at is None, "a refused drop must not have dropped them"


@pytest.mark.asyncio
async def test_a_captain_dropping_their_own_team_gets_a_confirmation(test_db):  # noqa: F811
    """The happy path all the way through the command. The gate tests above never
    reach the body, so nothing until now drove a drop that actually succeeds."""
    from cogs.tournament_commands import TournamentCog
    from services.tournament_service import (
        create_tournament, register_team, set_result, start_tournament,
    )
    import random
    from database.db_session import db_session

    async with db_session() as session:
        tournament = await create_tournament(session, "123", "Spring", 3)
        await session.commit()
        for i in range(4):
            await register_team(session, tournament.id, f"Team{i}", str(456 + i))
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        for match in matches:
            if not match.is_bye:
                await set_result(session, match.id, 2, 0)
        await session.commit()

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()  # author 456 captains Team0
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        await TournamentCog.drop_team.callback(cog, ctx, team=None)

    reply = ctx.followup.send.await_args.args[0]
    assert "Team0" in reply and "dropped" in reply.lower(), reply
    assert "❌" not in reply, reply


@pytest.mark.asyncio
async def test_a_captain_of_two_teams_can_name_which_one_to_drop(test_db):  # noqa: F811
    """Naming a team you captain is how you disambiguate -- the ambiguity message
    tells you to, so it has to work without the bot-manager role."""
    from cogs.tournament_commands import TournamentCog
    from services.tournament_service import (
        create_tournament, register_team, set_result, start_tournament,
    )
    import random
    from database.db_session import db_session

    async with db_session() as session:
        tournament = await create_tournament(session, "123", "Spring", 3)
        await session.commit()
        for name in ("Alpha", "Bravo"):          # both captained by author 456
            await register_team(session, tournament.id, name, "456")
        for i in range(2):
            await register_team(session, tournament.id, f"Other{i}", str(900 + i))
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        for match in matches:
            if not match.is_bye:
                await set_result(session, match.id, 2, 0)
        await session.commit()

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.is_bot_manager",
                                  AsyncMock(return_value=False)))
        await TournamentCog.drop_team.callback(cog, ctx, team="Alpha")

    reply = ctx.followup.send.await_args.args[0]
    assert "Alpha" in reply and "dropped" in reply.lower(), reply
    assert "❌" not in reply, reply


async def _running_swiss(session, captains):
    """A started swiss tournament with round one reported, ready for a drop.

    ``captains`` gives one captain id per team, so a test can say which of
    Team0..TeamN the invoking author owns.
    """
    import random

    from services.tournament_service import (
        create_tournament, register_team, set_result, start_tournament,
    )

    tournament = await create_tournament(session, "123", "Spring", 3)
    await session.commit()
    for i, captain in enumerate(captains):
        await register_team(session, tournament.id, f"Team{i}", str(captain))
    await session.commit()
    matches = await start_tournament(session, tournament.id, random.Random(7))
    await session.commit()
    for match in matches:
        if not match.is_bye:            # a bye is scored when it is paired
            await set_result(session, match.id, 2, 0)
    await session.commit()
    return tournament


@pytest.mark.asyncio
async def test_dropping_refreshes_the_standings_and_not_only_the_board(test_db):  # noqa: F811
    """The *(dropped)* marker lives in the standings embed. Refreshing only the
    registration board leaves the pinned standings — the one display everyone
    reads — listing the dropped team as an ordinary competitor."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session

    async with db_session() as session:
        tournament = await _running_swiss(session, [456, 900, 901, 902])
        t_id = tournament.id

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()                                  # author 456 captains Team0
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        standings = stack.enter_context(patch(
            "cogs.tournament_commands.update_standings_message", AsyncMock()))
        await TournamentCog.drop_team.callback(cog, ctx, team=None)

    assert "❌" not in ctx.followup.send.await_args.args[0]
    standings.assert_awaited_once()
    assert standings.await_args.args[1] == t_id


@pytest.mark.asyncio
async def test_a_dropped_team_is_not_paid_from_the_pot(test_db):  # noqa: F811
    """A dropped team is still 'paid' -- status answers whether the entry fee is
    held, not whether the team is still competing -- so a status-only filter left
    the team that walked away first in line for the prize pool."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session
    from services.tournament_service import drop_team, get_standings_data

    async with db_session() as session:
        tournament = await _running_swiss(session, [456, 900, 901, 902])
        tournament.entry_fee = 10
        leader = (await get_standings_data(session, tournament.id))[0]
        await drop_team(session, tournament.id, leader.team_name)
        tournament.status = "completed"
        await session.commit()
        gone_name = leader.team_name

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch("cogs.tournament_commands.escrow.is_paid_out",
                                  AsyncMock(return_value=False)))
        stack.enter_context(patch("cogs.tournament_commands.escrow.prize_pool",
                                  AsyncMock(return_value=100)))
        await TournamentCog.payout.callback(cog, ctx, tournament_id=None, structure=None)

    embed = ctx.followup.send.await_args.kwargs["embed"]
    assert "100 tix" in embed.description, embed.description
    assert gone_name not in embed.description, embed.description


@pytest.mark.asyncio
async def test_the_drop_confirmation_says_the_prize_is_forfeit(test_db):  # noqa: F811
    """The drop is irreversible and the money is real, so the one reply a captain
    sees before it lands has to say what it costs. 'Their results stay in the
    standings' reads as the opposite."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session

    async with db_session() as session:
        await _running_swiss(session, [456, 900, 901, 902])

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()                                  # author 456 captains Team0
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.update_standings_message",
                                  AsyncMock()))
        await TournamentCog.drop_team.callback(cog, ctx, team=None)

    reply = ctx.followup.send.await_args.args[0]
    assert "❌" not in reply, reply
    assert "forfeit" in reply.lower(), reply
    # The command picker is the only place this is said BEFORE the drop lands.
    assert "forfeit" in TournamentCog.drop_team.description.lower()


@pytest.mark.asyncio
async def test_the_drop_reply_does_not_name_a_round_that_will_never_exist(test_db):  # noqa: F811
    """At the end of swiss the next swiss round is never paired -- a cut or the
    finish comes instead -- so "from round N+1" points at a round nobody will
    ever see."""
    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session

    async with db_session() as session:
        tournament = await _running_swiss(session, [456, 900, 901, 902])
        tournament.total_rounds = 1               # round one WAS the last one
        tournament.cut_to = 2
        await session.commit()

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()                                  # author 456 captains Team0
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.update_standings_message",
                                  AsyncMock()))
        await TournamentCog.drop_team.callback(cog, ctx, team=None)

    reply = ctx.followup.send.await_args.args[0]
    assert "❌" not in reply, reply
    assert "round 2" not in reply.lower(), reply
    assert "not be paired again" in reply.lower(), reply


@pytest.mark.asyncio
async def test_a_team_that_had_the_bye_is_not_told_to_report_it(test_db):  # noqa: F811
    """A bye is scored the moment it is paired and records no game wins, so it
    looks exactly like an unreported match. Telling the organizer to record it
    sends them to /tournament set_result, which refuses byes outright -- and a
    drop is what makes the field odd, so this is the common case, not the rare
    one."""
    from sqlalchemy import select

    from cogs.tournament_commands import TournamentCog
    from database.db_session import db_session
    from models.tournament import TournamentMatch, TournamentParticipant

    async with db_session() as session:
        tournament = await _running_swiss(session, [900, 901, 902, 903, 904])
        bye = (await session.execute(
            select(TournamentMatch).where(TournamentMatch.is_bye.is_(True))
        )).scalars().one()
        assert bye.team_a_wins is None, "a bye records no game wins -- that is the trap"
        rested = await session.get(TournamentParticipant, bye.team_a_participant_id)
        bye_team = rested.team_name

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    ctx = _ctx()
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.update_standings_message",
                                  AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.is_bot_manager",
                                  AsyncMock(return_value=True)))
        await TournamentCog.drop_team.callback(cog, ctx, team=bye_team)

    reply = ctx.followup.send.await_args.args[0]
    assert "❌" not in reply, reply
    assert "set_result" not in reply, reply


@pytest.mark.asyncio
async def test_prompt_offers_start_with_play_in_when_a_pair_is_proposed():
    from cogs.tournament_commands import PlayoffPromptView
    from services.tournament_service import PlayInProposal
    a, b = SimpleNamespace(id=1, team_name="gypsy caravan"), SimpleNamespace(id=2, team_name="18 lands")
    view = PlayoffPromptView(MagicMock(), 3, 8, proposal=PlayInProposal((a, b), 2))
    labels = [c.label for c in view.children]
    assert "Start with play-in" in labels and "Start without" in labels and "Edit play-in…" in labels


@pytest.mark.asyncio
async def test_prompt_without_a_pair_has_no_start_with_play_in():
    from cogs.tournament_commands import PlayoffPromptView
    from services.tournament_service import PlayInProposal
    view = PlayoffPromptView(MagicMock(), 3, 8, proposal=PlayInProposal(None, 3))
    labels = [c.label for c in view.children]
    assert "Start with play-in" not in labels and "Edit play-in…" in labels


@pytest.mark.asyncio
async def test_prompt_without_a_proposal_is_unchanged():
    from cogs.tournament_commands import PlayoffPromptView
    view = PlayoffPromptView(MagicMock(), 3, 8)
    assert [c.label for c in view.children] == [
        "Start top-8 playoff", "Finish now — crown the Swiss leader"]


@pytest.mark.asyncio
async def test_play_in_modal_refuses_a_duplicate_pair_without_closing_the_prompt():
    from cogs.tournament_commands import PlayInModal, PlayoffPromptView
    team = SimpleNamespace(id=1, team_name="alpha", status="paid", dropped_at=None)
    cog = MagicMock()
    cog._run_playoff = AsyncMock()
    view = PlayoffPromptView(cog, 3, 8)
    modal = PlayInModal(view)
    modal.higher.value = modal.lower.value = "alpha"
    interaction = _playoff_prompt_interaction()
    with patch("cogs.tournament_commands.db_session"), \
         patch("cogs.tournament_commands.find_participant_by_name", AsyncMock(return_value=team)):
        await modal.callback(interaction)
    cog._run_playoff.assert_not_awaited()
    interaction.response.edit_message.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert not any(c.disabled for c in view.children)


@pytest.mark.asyncio
async def test_create_makes_find_a_team_before_the_board_and_points_at_the_button():
    """The board names #find-a-team only once the server has it, so on a server's
    first tournament the channel must exist before the board is drawn."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    order = []
    tournament = MagicMock(id=7, total_rounds=3)
    tournament.name = "Cup"
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
            patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
            patch("cogs.tournament_commands.create_tournament", AsyncMock(return_value=tournament)), \
            patch("cogs.tournament_commands.ensure_lfg_board",
                  AsyncMock(side_effect=lambda *a: order.append("lfg"))), \
            patch("cogs.tournament_commands.post_registration_board",
                  AsyncMock(side_effect=lambda *a: order.append("board"))):
        await TournamentCog.create.callback(
            cog, ctx, name="Cup", format="swiss", rounds=3, entry_fee=0,
            payout="winner_take_all", cut=None)

    assert order == ["lfg", "board"]
    reply = ctx.followup.send.call_args.args[0]
    assert "Register a team" in reply and "/tournament register" not in reply


@pytest.mark.asyncio
async def test_add_team_gives_the_captain_the_roles():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    ctx = _ctx()
    captain = _player(777)
    participant = SimpleNamespace(id=9, team_name="Alpha")

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1))), \
         patch("cogs.tournament_commands.register_team",
               AsyncMock(return_value=(participant, True))), \
         patch("cogs.tournament_commands.escrow.comp", return_value=True), \
         patch("cogs.tournament_commands.reconcile_after_command", AsyncMock()), \
         patch("cogs.tournament_commands.ensure_team_role",
               AsyncMock(return_value="555")) as ensure, \
         patch("cogs.tournament_commands.grant_league_roles",
               AsyncMock(return_value=True)) as grant:
        await TournamentCog.add_team.callback(cog, ctx, team="Alpha", captain=captain)

    ensure.assert_awaited_once_with(ctx.guild, 9)
    grant.assert_awaited_once_with(ctx.guild, ["777"])
    assert "⚠️" not in ctx.followup.send.call_args.args[0]

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1))), \
         patch("cogs.tournament_commands.register_team",
               AsyncMock(return_value=(participant, True))), \
         patch("cogs.tournament_commands.escrow.comp", return_value=True), \
         patch("cogs.tournament_commands.reconcile_after_command", AsyncMock()), \
         patch("cogs.tournament_commands.ensure_team_role", AsyncMock(return_value=None)), \
         patch("cogs.tournament_commands.grant_league_roles", AsyncMock(return_value=True)):
        await TournamentCog.add_team.callback(cog, ctx, team="Alpha", captain=captain)

    assert "Couldn't give every role" in ctx.followup.send.call_args.args[0]


# ---- roles on the way out -----------------------------------------------------------

@pytest.mark.asyncio
async def test_removing_a_teammate_also_releases_league_participant():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"
    player = _player(4242)

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.sync_member", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(return_value=True)) as release:
        await TournamentCog.remove_teammate_cmd.callback(cog, ctx, player=player, team=None)
    release.assert_awaited_once_with(ctx.guild, [str(player.id)])
    assert "⚠️" not in ctx.followup.send.call_args.args[0]

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.sync_member", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(return_value=False)):
        await TournamentCog.remove_teammate_cmd.callback(cog, ctx, player=player, team=None)
    assert "⚠️" in ctx.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_removing_someone_who_was_not_on_the_roster_does_not_release_league_participant():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=False)), \
         patch("cogs.tournament_commands.release_league_role", AsyncMock()) as release:
        await TournamentCog.remove_teammate_cmd.callback(
            cog, _ctx(), player=_player(), team=None)
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_removing_a_team_deletes_its_role_and_releases_league_participant():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    ctx = _ctx()
    res = {"team_name": "Alpha", "refunded": 0, "role_id": "555",
           "user_ids": ["10", "11"]}
    order = []

    async def _delete(guild, role_ids):
        order.append("delete")
        return set(role_ids)

    async def _release(guild, user_ids):
        order.append("release")
        return True

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1))), \
         patch("cogs.tournament_commands.escrow.drop_with_refund",
               AsyncMock(return_value=res)), \
         patch("cogs.tournament_commands.delete_team_roles",
               AsyncMock(side_effect=_delete)) as gone, \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(side_effect=_release)) as release:
        await TournamentCog.remove_team.callback(cog, ctx, team="Alpha")

    gone.assert_awaited_once_with(ctx.guild, ["555"])
    release.assert_awaited_once_with(ctx.guild, ["10", "11"])
    assert order == ["delete", "release"]
    assert "⚠️" not in ctx.followup.send.call_args.args[0]

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.get_active_tournament",
               AsyncMock(return_value=SimpleNamespace(id=1))), \
         patch("cogs.tournament_commands.escrow.drop_with_refund",
               AsyncMock(return_value=res)), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(return_value=set())), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(return_value=True)):
        await TournamentCog.remove_team.callback(cog, ctx, team="Alpha")
    assert "Couldn't take back every role" in ctx.followup.send.call_args.args[0]


async def _started_swiss(captains_by_team, extra_teams=2):
    """A started three-round swiss in guild 123 with round one fully reported.
    ``captains_by_team`` is [(team, captain)]; extra filler teams make it four+."""
    import random
    from database.db_session import db_session
    from services.tournament_service import (
        create_tournament, register_team, set_result, start_tournament,
    )

    async with db_session() as session:
        tournament = await create_tournament(session, "123", "Spring", 3)
        await session.commit()
        for name, captain in captains_by_team:
            await register_team(session, tournament.id, name, captain)
        for i in range(extra_teams):
            await register_team(session, tournament.id, f"Other{i}", str(900 + i))
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        for match in matches:
            if not match.is_bye:
                await set_result(session, match.id, 2, 0)
        await session.commit()
        return tournament.id


async def _set_role_id(tournament_id, team_name, role_id):
    from database.db_session import db_session
    from services.tournament_service import find_participant_by_name

    async with db_session() as session:
        (await find_participant_by_name(session, tournament_id, team_name)).role_id = role_id


async def _role_id_of(tournament_id, team_name):
    from database.db_session import db_session
    from services.tournament_service import find_participant_by_name

    async with db_session() as session:
        return (await find_participant_by_name(session, tournament_id, team_name)).role_id


def _drop_cog():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog.__new__(TournamentCog)
    cog.bot = MagicMock()
    return cog


def _league_guild(user_ids):
    """A guild whose members hold League Participant, for the real release."""
    lp = MagicMock()
    lp.name = "League Participant"
    members = {}
    for uid in user_ids:
        member = MagicMock()
        member.roles = [lp]
        member.remove_roles = AsyncMock()
        members[int(uid)] = member
    guild = MagicMock()
    guild.id = 123
    guild.roles = [lp]
    guild.get_member = MagicMock(side_effect=members.get)
    return guild, lp, members


@pytest.mark.asyncio
async def test_dropping_a_team_deletes_its_role_and_clears_the_id(test_db):  # noqa: F811
    from cogs.tournament_commands import TournamentCog

    t_id = await _started_swiss([("Alpha", "456")])
    await _set_role_id(t_id, "Alpha", "555")
    cog = _drop_cog()
    ctx = _ctx()
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        gone = stack.enter_context(patch("cogs.tournament_commands.delete_team_roles",
                                         AsyncMock(return_value={"555"})))
        release = stack.enter_context(patch("cogs.tournament_commands.release_league_role",
                                            AsyncMock(return_value=True)))
        await TournamentCog.drop_team.callback(cog, ctx, team="Alpha")

    gone.assert_awaited_once_with(ctx.guild, ["555"])
    release.assert_awaited_once_with(ctx.guild, ["456"])
    assert await _role_id_of(t_id, "Alpha") is None
    assert "⚠️" not in ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_dropping_a_team_keeps_the_id_of_a_role_that_did_not_delete(test_db):  # noqa: F811
    from cogs.tournament_commands import TournamentCog

    t_id = await _started_swiss([("Alpha", "456")])
    await _set_role_id(t_id, "Alpha", "555")
    cog = _drop_cog()
    ctx = _ctx()
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.delete_team_roles",
                                  AsyncMock(return_value=set())))
        stack.enter_context(patch("cogs.tournament_commands.release_league_role",
                                  AsyncMock(return_value=True)))
        await TournamentCog.drop_team.callback(cog, ctx, team="Alpha")

    assert await _role_id_of(t_id, "Alpha") == "555"
    reply = ctx.followup.send.await_args.args[0]
    assert "dropped" in reply and "Couldn't take back every role" in reply


@pytest.mark.asyncio
async def test_dropping_one_of_two_teams_keeps_league_participant(test_db):  # noqa: F811
    from cogs.tournament_commands import TournamentCog

    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "456")], extra_teams=2)
    await _set_role_id(t_id, "Alpha", "555")
    guild, lp, members = _league_guild(["456"])
    cog = _drop_cog()
    ctx = _ctx()
    ctx.guild = guild
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.delete_team_roles",
                                  AsyncMock(return_value={"555"})))
        stack.enter_context(patch("services.league_roles.get_config", return_value={}))
        await TournamentCog.drop_team.callback(cog, ctx, team="Alpha")

    members[456].remove_roles.assert_not_called()   # still captains Bravo

    # Dropping the other one as well leaves them on no live team.
    with ExitStack() as stack:
        stack.enter_context(patch.object(TournamentCog, "_check_enabled",
                                         AsyncMock(return_value=True)))
        stack.enter_context(patch.object(TournamentCog, "_refresh_board", AsyncMock()))
        stack.enter_context(patch("cogs.tournament_commands.delete_team_roles",
                                  AsyncMock(return_value=set())))
        stack.enter_context(patch("services.league_roles.get_config", return_value={}))
        await TournamentCog.drop_team.callback(cog, ctx, team="Bravo")
    members[456].remove_roles.assert_awaited_once_with(lp)


@pytest.mark.asyncio
async def test_finishing_releases_league_participant_for_everyone():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    guild = MagicMock()
    participants = [
        SimpleNamespace(role_id="111", captain_user_id="10", roster_user_ids=["11", "12"]),
        SimpleNamespace(role_id=None, captain_user_id="20", roster_user_ids=[]),
    ]
    order = []

    async def _delete(g, role_ids):
        order.append("delete")
        return {"111"}

    async def _release(g, user_ids):
        order.append("release")
        return True

    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants",
               AsyncMock(return_value=participants)), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(side_effect=_delete)), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(side_effect=_release)) as release:
        await cog._drop_team_roles(guild, 1)

    assert order == ["delete", "release"]
    release.assert_awaited_once()
    assert release.await_args.args[0] is guild
    assert sorted(release.await_args.args[1]) == ["10", "11", "12", "20"]


@pytest.mark.asyncio
async def test_drop_team_roles_never_raises_when_the_league_release_fails():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants", AsyncMock(return_value=[])), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock(return_value=set())), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("cogs.tournament_commands.logger") as log:
        await cog._drop_team_roles(MagicMock(), 1)
    log.exception.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("apply, runs, skips", [(False, "plan_role_sync", "apply_role_sync"),
                                                (True, "apply_role_sync", "plan_role_sync")])
async def test_sync_roles_replies_ephemerally_and_only_applies_when_asked(apply, runs, skips):
    from cogs.tournament_commands import TournamentCog
    from services.league_roles import RoleSyncPlan

    cog = TournamentCog.__new__(TournamentCog)
    cog._check_enabled = AsyncMock(return_value=True)
    ctx = _ctx()
    mocks = {n: AsyncMock(return_value=RoleSyncPlan(["Bravo"], ["1"], ["2"]))
             for n in ("plan_role_sync", "apply_role_sync")}
    with patch("cogs.tournament_commands.plan_role_sync", mocks["plan_role_sync"]), \
         patch("cogs.tournament_commands.apply_role_sync", mocks["apply_role_sync"]):
        await TournamentCog.sync_roles.callback(cog, ctx, apply=apply)

    mocks[runs].assert_awaited_once()
    mocks[skips].assert_not_called()
    ctx.defer.assert_awaited_once_with(ephemeral=True)
    text = ctx.followup.send.await_args.args[0]
    assert ctx.followup.send.await_args.kwargs == {"ephemeral": True}
    assert text.startswith("Dry run") is (not apply)
    assert "<@1>" in text and "**Bravo**" in text



@pytest.mark.asyncio
async def test_finish_still_releases_league_participant_when_role_deletion_raises():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    participants = [SimpleNamespace(role_id="111", captain_user_id="10", roster_user_ids=["11"])]
    with patch("cogs.tournament_commands.db_session", lambda: _NullSession()), \
         patch("cogs.tournament_commands.list_participants",
               AsyncMock(return_value=participants)), \
         patch("cogs.tournament_commands.delete_team_roles",
               AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(return_value=True)) as release:
        await cog._drop_team_roles(MagicMock(), 1)  # must not raise

    release.assert_awaited_once()
    assert sorted(release.await_args.args[1]) == ["10", "11"]



@pytest.mark.asyncio
async def test_remove_teammate_replies_even_if_the_release_raises():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    participant = MagicMock()
    participant.role_id = "555"
    participant.team_name = "Alpha"

    with _roster_command_open(participant), \
         patch("cogs.tournament_commands.remove_teammate", AsyncMock(return_value=True)), \
         patch("cogs.tournament_commands.release_league_role",
               AsyncMock(side_effect=RuntimeError("db"))), \
         patch("cogs.tournament_commands.sync_member", AsyncMock(return_value=True)):
        await TournamentCog.remove_teammate_cmd.callback(cog, ctx, player=_player(), team=None)

    reply = ctx.followup.send.call_args.args[0]
    assert "✅" in reply
    assert "⚠️" in reply



@pytest.mark.asyncio
async def test_sync_roles_reports_a_failure_instead_of_hanging():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog.__new__(TournamentCog)
    cog._check_enabled = AsyncMock(return_value=True)
    ctx = _ctx()
    with patch("cogs.tournament_commands.apply_role_sync",
               AsyncMock(side_effect=RuntimeError("gateway"))), \
         patch("cogs.tournament_commands.logger") as log:
        await TournamentCog.sync_roles.callback(cog, ctx, apply=True)

    log.exception.assert_called_once()
    ctx.followup.send.assert_awaited_once_with(
        "❌ Role sync failed partway — check the logs, then run it again "
        "(it only does what is still missing).", ephemeral=True)
