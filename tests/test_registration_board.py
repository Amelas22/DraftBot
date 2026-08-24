"""The registration board: renders roster + payment state, and edits in place."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from models.tournament import (
    Tournament,
    TournamentParticipant,
    TournamentTeamMember,
)
from services.tournament_formatter import create_registration_embed


def _tournament(name="Fee Cup", fee=1, status="registration", cut_to=None):
    return Tournament(id=1, guild_id="g1", name=name, total_rounds=0, format="manual",
                      status=status, entry_fee=fee, payout_structure="winner_take_all",
                      cut_to=cut_to)


def _team(pid, name, captain, status):
    return TournamentParticipant(id=pid, tournament_id=1, team_id=pid, team_name=name,
                                 captain_user_id=captain, status=status)


def _text(embed):
    return embed.description + "".join(f.name + f.value for f in embed.fields)


def test_paid_board_marks_paid_and_pending_with_a_deficit():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1})
    text = _text(embed)

    assert "Registration open" in embed.title
    assert "Teams (1/2 paid)" in text
    assert "✅ **Alpha**" in text and "⏳ **Gamma**" in text
    assert "needs 1 more tix" in text and "/wallet deposit 1" in text
    assert "Entry fee:** 1 tix/team" in text and "Prize pool:** 1 tix" in text


def test_free_board_is_a_plain_roster():
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(fee=0), teams)
    text = _text(embed)

    assert "Teams (1)" in text
    assert "Entry fee" not in text and "Prize pool" not in text
    assert "needs" not in text and "⏳" not in text


def test_free_board_with_a_cut_has_no_leading_separator():
    """A cut can be declared with no entry fee, so desc starts empty ("").
    The '· Cut: top N' bullet must not carry a stray leading separator when
    there was no payout line in front of it."""
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(fee=0, cut_to=8), teams)
    text = _text(embed)

    assert "**Cut:** top 8" in text
    assert not embed.description.startswith(" ·")
    assert not embed.description.startswith("·")


def test_paid_board_with_a_cut_appends_after_the_payout_line():
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(fee=1, cut_to=4), teams, pot=1)

    assert "Payout:" in embed.description
    assert "· **Cut:** top 4" in embed.description


def test_all_paid_board_shows_no_deficit_lines():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Beta", "20", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=2, deficits={}))

    assert "Teams (2/2 paid)" in text
    assert "needs" not in text and "⏳" not in text


def test_closed_board_drops_deficits_and_says_closed():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1}, closed=True)

    assert "Registration closed" in embed.title
    assert "needs" not in _text(embed)


def test_empty_board_invites_registration():
    assert "/tournament register" in _text(create_registration_embed(_tournament(), []))


def test_join_instructions_stay_visible_once_teams_have_registered():
    """They used to show only on an empty board, so they vanished exactly when
    newcomers start reading it."""
    teams = [_team(1, "Alpha", "10", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=1))

    assert "How to join" in text
    assert "/tournament register" in text


def test_paid_join_instructions_name_the_link_and_deposit_steps():
    """Paying an entry isn't one step: the fee comes from the captain's wallet, so a
    newcomer needs the MTGO link and the deposit amount, not just the register command."""
    text = _text(create_registration_embed(_tournament(fee=3), [], pot=0))

    assert "/link_mtgo" in text
    assert "/wallet deposit 3" in text


def test_free_join_instructions_are_just_the_one_command():
    text = _text(create_registration_embed(_tournament(fee=0), []))

    assert "/tournament register" in text
    assert "/link_mtgo" not in text and "/wallet deposit" not in text


def test_a_closed_board_drops_the_join_instructions():
    teams = [_team(1, "Alpha", "10", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=1, closed=True))

    assert "How to join" not in text


def test_large_roster_splits_across_fields_under_the_discord_cap():
    """Discord caps a single embed field's value at 1024 characters. Roster lines
    are ~52 chars and deficit lines ~43, so a paid tournament breaks at roughly 10
    pending teams — the board must split the roster across multiple fields instead
    of overflowing one, and every team must still be listed somewhere."""
    teams = []
    deficits = {}
    for i in range(1, 26):
        status = "paid" if i % 2 == 0 else "pending"
        teams.append(_team(i, f"Team Number Twenty-Five Roster Entry {i:02d}", str(1000 + i), status))
        if status == "pending":
            deficits[i] = 3

    embed = create_registration_embed(_tournament(), teams, pot=10, deficits=deficits)

    assert len(embed.fields) > 1, "a 25-team paid roster should overflow a single field"
    for f in embed.fields:
        assert len(f.value) < 1024, f"field {f.name!r} is {len(f.value)} chars"

    full_text = "".join(f.value for f in embed.fields)
    for t in teams:
        assert t.team_name in full_text


from unittest.mock import AsyncMock, MagicMock

import discord

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from services.tournament_formatter import update_registration_board


async def _seed_tournament(posted=True):
    async with db_session() as session:
        t = Tournament(guild_id="g1", name="Fee Cup", total_rounds=0, format="manual",
                       status="registration", entry_fee=1,
                       board_channel_id="777" if posted else None,
                       board_message_id="888" if posted else None)
        session.add(t)
        await session.flush()
        return t.id


@pytest.mark.asyncio
async def test_updater_is_a_no_op_when_the_board_was_never_posted(test_db):  # noqa: F811
    t_id = await _seed_tournament(posted=False)
    bot = MagicMock()
    bot.get_channel = MagicMock()

    await update_registration_board(bot, t_id)

    bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_updater_edits_the_stored_message(test_db):  # noqa: F811
    t_id = await _seed_tournament()
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)

    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deleted_board_clears_its_ids_so_it_stops_retrying(test_db):  # noqa: F811
    t_id = await _seed_tournament()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)

    async with db_session() as session:
        t = await session.get(Tournament, t_id)
        assert t.board_message_id is None and t.board_channel_id is None


def _member(user_id, name="Player"):
    return TournamentTeamMember(participant_id=1, user_id=user_id, display_name=name)


def test_board_lists_the_full_team_not_just_the_captain():
    teams = [_team(1, "Alpha", "10", "paid")]
    rosters = {1: [_member("11", "Bob"), _member("12", "Cara")]}
    text = _text(create_registration_embed(_tournament(), teams, pot=1, rosters=rosters))

    assert "👑 <@10>" in text
    assert "<@11>" in text and "<@12>" in text


def test_board_explains_the_crown():
    """The crown is only legible if something says what it means."""
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(), teams, pot=1,
                                      rosters={1: [_member("11")]})

    assert embed.footer.text and "captain" in embed.footer.text.lower()


def test_board_renders_a_team_with_no_teammates_yet():
    """A captain who hasn't added anyone must not leave a dangling separator."""
    teams = [_team(1, "Alpha", "10", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=1, rosters={}))

    assert "👑 <@10>" in text
    assert "👑 <@10> ·" not in text


def test_board_keeps_each_teams_members_on_its_own_line():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Beta", "20", "paid")]
    rosters = {1: [_member("11")], 2: [_member("21")]}
    text = _text(create_registration_embed(_tournament(), teams, pot=2, rosters=rosters))

    alpha_line = next(ln for ln in text.split("\n") if "Alpha" in ln)
    assert "<@11>" in alpha_line and "<@21>" not in alpha_line


def test_join_instructions_name_the_add_teammate_step():
    """Registering only records the captain, so the roster step has to be told."""
    text = _text(create_registration_embed(_tournament(fee=0), []))

    assert "/tournament add_teammate" in text


def test_large_roster_with_members_still_splits_under_the_cap():
    """Members make each line ~3x longer, so the field-splitting has to hold."""
    teams = []
    rosters = {}
    for i in range(1, 26):
        teams.append(_team(i, f"Team Number Twenty-Five Roster Entry {i:02d}", str(1000 + i), "paid"))
        rosters[i] = [_member(str(2000 + i)), _member(str(3000 + i)), _member(str(4000 + i))]

    embed = create_registration_embed(_tournament(), teams, pot=10, rosters=rosters)

    assert len(embed.fields) > 1
    for f in embed.fields:
        assert len(f.value) < 1024, f"field {f.name!r} is {len(f.value)} chars"
    full_text = "".join(f.value for f in embed.fields)
    for t in teams:
        assert t.team_name in full_text


# ---- the board's phase follows the tournament, not the caller ------------------

async def _refresh_and_capture(status):
    """Run update_registration_board against a tournament in `status`, return the embed."""
    from unittest.mock import AsyncMock, MagicMock

    from database.db_session import db_session
    from services.tournament_formatter import update_registration_board
    from services.tournament_service import create_tournament, register_team

    async with db_session() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, t.id, "Alpha", "42")
        t.status = status
        t.board_channel_id = "111"
        t.board_message_id = "222"
        await session.commit()
        t_id = t.id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)
    return message.edit.call_args.kwargs["embed"]


@pytest.mark.asyncio
async def test_board_of_a_started_tournament_refreshes_as_closed(test_db):  # noqa: F811
    """Rosters stay editable after the start, and every roster edit refreshes the
    board. If the phase came from the caller's flag it would flip a started
    tournament back to 'Registration open' and re-advertise the join steps."""
    embed = await _refresh_and_capture("active")

    assert "Registration closed" in embed.title
    assert "How to join" not in "".join(f.name for f in embed.fields)


@pytest.mark.asyncio
async def test_board_still_open_while_registration_is(test_db):  # noqa: F811
    embed = await _refresh_and_capture("registration")

    assert "Registration open" in embed.title
    assert "How to join" in "".join(f.name for f in embed.fields)


def test_one_huge_roster_cannot_overflow_its_field():
    """The chunker splits BETWEEN lines, so it cannot rescue a single line that is
    itself over Discord's 1024-char field cap. A team's members all share one line,
    so an unbounded roster would freeze the board on a rejected edit."""
    teams = [_team(1, "Alpha", "10", "paid")]
    rosters = {1: [_member(str(100000000000000000 + i)) for i in range(60)]}

    embed = create_registration_embed(_tournament(), teams, pot=1, rosters=rosters)

    for f in embed.fields:
        assert len(f.value) < 1024, f"field {f.name!r} is {len(f.value)} chars"
    text = "".join(f.value for f in embed.fields)
    assert "more" in text, "the dropped members should be accounted for, not silently lost"
