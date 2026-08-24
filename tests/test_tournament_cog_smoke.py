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
            "recover_draft", "open_rooms"} <= subcommands


def test_admin_commands_are_gated_by_bot_manager_check():
    from cogs.tournament_commands import TournamentCog

    for command in ("create", "start", "set_result", "next_round", "finish",
                    "add_team", "remove_team", "add_match", "refresh_standings",
                    "recover_draft", "open_rooms"):
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
    # start() now opens a session for role creation before anything else --
    # without _registration_open() this hits the real drafts.db on disk (or,
    # on a fresh checkout with no such file, raises OperationalError).
    with _registration_open(), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value=res)), \
         patch("cogs.tournament_commands.create_team_roles",
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
    """Stands in for db_session(): an async context manager yielding a mock."""
    async def __aenter__(self):
        return MagicMock()

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

    Without this they read the real drafts.db. `_refresh_board` and
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
    stack.enter_context(patch("cogs.tournament_commands.other_teams_for_user",
                              AsyncMock(return_value=[])))
    return stack


def _registration_open(paid_teams=2, pending_teams=0):
    """Patch what `start` reads before it touches Discord: the feature gate
    (`tournament_enabled`) and the two reads `_create_roles_for_start` makes
    (`get_active_tournament`, `list_participants`) -- so the command reaches
    Discord instead of hitting the real database.

    `pending_teams` seeds extra participants with status="pending" alongside
    the `paid_teams` paid ones, so a test can assert the paid-only filter
    actually filters (`_registration_open()` with no pending teams cannot
    distinguish "filtered" from "didn't need to").
    """
    stack = ExitStack()
    stack.enter_context(patch("cogs.tournament_commands.tournament_enabled",
                              return_value=True))
    stack.enter_context(patch("cogs.tournament_commands.db_session",
                              lambda: _NullSession()))
    stack.enter_context(patch("cogs.tournament_commands.get_active_tournament",
                              AsyncMock(return_value=SimpleNamespace(
                                  id=1, status="registration"))))
    participants = [
        SimpleNamespace(id=i, status="paid", team_name=f"T{i}",
                        captain_user_id=str(100 + i), roster_user_ids=[])
        for i in range(paid_teams)
    ] + [
        SimpleNamespace(id=1000 + i, status="pending", team_name=f"P{i}",
                        captain_user_id=str(900 + i), roster_user_ids=[])
        for i in range(pending_teams)
    ]
    stack.enter_context(patch("cogs.tournament_commands.list_participants",
                              AsyncMock(return_value=participants)))
    return stack


@pytest.mark.asyncio
async def test_start_creates_a_role_per_team_and_stores_the_ids():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    # As in test_start_freezes_the_board: this test is about role creation, not
    # the rest of start()'s tail, so the board/schedule/standings posts are
    # stubbed out rather than run for real against a fake session.
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock()
    cog._post_standings = AsyncMock()
    ctx = _ctx()
    with _registration_open(), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value={"tournament_id": 1, "name": "Cup", "fee": 0, "pot": 0})), \
         patch("cogs.tournament_commands.create_team_roles",
               AsyncMock(return_value={7: "555"})) as mk, \
         patch("cogs.tournament_commands.store_role_ids", AsyncMock()) as store:
        await TournamentCog.start.callback(cog, ctx)

    mk.assert_awaited_once()
    # Not just "some call happened": the ids create_team_roles returned must be
    # the exact ids handed to store_role_ids, or the persistence half of this
    # feature could silently drop or scramble them.
    store.assert_awaited_once_with(mk.return_value)


@pytest.mark.asyncio
async def test_start_only_creates_roles_for_paid_teams():
    """Mirrors start_tournament's own eligibility rule (status == "paid" in
    services/tournament_service.py): a pending team must not get a real
    Discord role handed to players who never entered."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock()
    cog._post_standings = AsyncMock()
    ctx = _ctx()
    with _registration_open(paid_teams=2, pending_teams=1), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value={"tournament_id": 1, "name": "Cup", "fee": 0, "pot": 0})), \
         patch("cogs.tournament_commands.create_team_roles",
               AsyncMock(return_value={})) as mk, \
         patch("cogs.tournament_commands.store_role_ids", AsyncMock()):
        await TournamentCog.start.callback(cog, ctx)

    passed = list(mk.await_args.args[1])
    assert len(passed) == 2
    assert all(p.status == "paid" for p in passed)


@pytest.mark.asyncio
async def test_start_deletes_the_roles_it_just_made_if_the_start_fails():
    """The Discord half of a rollback is not free: close_registration_and_seed
    raising after roles exist must delete them explicitly, or real roles --
    and the player role-assignments that come with them -- are stranded with
    nothing recording their ids."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    with _registration_open(), \
         patch("cogs.tournament_commands.create_team_roles",
               AsyncMock(return_value={7: "555", 8: "556"})), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(side_effect=ValueError("'Cup' has already started."))), \
         patch("cogs.tournament_commands.delete_team_roles", AsyncMock()) as delete, \
         patch("cogs.tournament_commands.store_role_ids", AsyncMock()) as store:
        await TournamentCog.start.callback(cog, ctx)

    assert list(delete.await_args.args[1]) == ["555", "556"]
    store.assert_not_awaited()         # never reached: the start failed first
    sent = ctx.followup.send.call_args.args[0]
    assert "already started" in sent   # the TO still sees the real reason


@pytest.mark.asyncio
async def test_start_refuses_when_roles_cannot_be_created():
    """Failing at start is far cheaper than discovering it when the first match
    room opens. create_team_roles is mocked here (its own rollback is Task 2's
    to test), so this only checks that a failure from it stops the start and
    reaches the TO -- not that anything gets unwound."""
    import discord
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    with _registration_open(), \
         patch("cogs.tournament_commands.create_team_roles",
               AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions"))), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock()) as seed:
        await TournamentCog.start.callback(cog, ctx)

    seed.assert_not_awaited()          # the tournament never started
    sent = " ".join(str(c) for c in ctx.followup.send.await_args_list)
    assert "role" in sent.lower()


@pytest.mark.asyncio
async def test_start_does_not_misreport_a_post_start_discord_failure_as_a_role_failure():
    """Regression (review round 1): the HTTPException handler used to wrap the
    whole try body, so a Discord failure from _post_schedule -- which only
    runs AFTER close_registration_and_seed has committed the start -- was
    reported to the TO as "Could not create team roles" and swallowed instead
    of reaching py-cord's on_error. The handler must be scoped to
    _create_roles_for_start alone."""
    import discord
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access"))
    cog._post_standings = AsyncMock()
    ctx = _ctx()
    with _registration_open(), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value={"tournament_id": 1, "name": "Cup", "fee": 0, "pot": 0})), \
         patch("cogs.tournament_commands.create_team_roles",
               AsyncMock(return_value={})), \
         patch("cogs.tournament_commands.store_role_ids", AsyncMock()):
        with pytest.raises(discord.Forbidden):
            await TournamentCog.start.callback(cog, ctx)

    sent = " ".join(str(c) for c in ctx.followup.send.await_args_list)
    assert "team roles" not in sent.lower()


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
         patch("cogs.tournament_commands.advance_round",
               AsyncMock(side_effect=SwissComplete(4, eligible))):
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
    cog._destination = MagicMock(return_value=MagicMock())
    cog._post_round_messages = AsyncMock()
    tournament = MagicMock()
    tournament.cut_to = 4
    new_round = MagicMock()
    new_round.id, new_round.round_number = 11, 4

    with patch("cogs.tournament_commands.db_session",
               _fake_db_session(_session_stub(tournament))), \
         patch("cogs.tournament_commands.start_playoff",
               AsyncMock(return_value=new_round)), \
         patch("cogs.tournament_commands.update_standings_message",
               AsyncMock()) as refresh, \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(cog, tournament_id=7, cut_to=4)
        await view.start_playoff_button.callback(_manager_interaction())

    cog._post_round_messages.assert_awaited_once()
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
         patch("helpers.permissions.get_config", return_value={}):
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
         patch("cogs.tournament_commands.add_teammate",
               AsyncMock(return_value=(MagicMock(), True))), \
         patch("cogs.tournament_commands.sync_member", AsyncMock()) as sync:
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
         patch("cogs.tournament_commands.add_teammate",
               AsyncMock(return_value=(MagicMock(), False))), \
         patch("cogs.tournament_commands.sync_member", AsyncMock()) as sync:
        await TournamentCog.add_teammate_cmd.callback(cog, ctx, player=player, team=None)

    sync.assert_awaited_once()
    assert sync.await_args.kwargs["add"] is True
    reply = ctx.followup.send.call_args.args[0]
    assert "already on" in reply


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
         patch("cogs.tournament_commands.add_teammate",
               AsyncMock(return_value=(MagicMock(), True))), \
         patch("cogs.tournament_commands.sync_member", AsyncMock(return_value=False)):
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
