"""Smoke tests for cogs/tournament_commands.py (Slice 1)."""
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

    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 456
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    res = {"tournament_id": 1, "name": "Cup", "pot": 0, "fee": 0}
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.escrow.close_registration_and_seed",
               AsyncMock(return_value=res)):
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
    from cogs.tournament_commands import PlayoffPromptView

    with patch("cogs.tournament_commands.finish_tournament",
               AsyncMock(side_effect=ValueError("'Cup' is not active."))), \
         patch("helpers.permissions.get_config", return_value={}):
        view = PlayoffPromptView(MagicMock(), tournament_id=1, cut_to=8)
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
    cog._post_round_messages = AsyncMock(side_effect=_record)

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
