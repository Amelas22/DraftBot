"""The Register a team wizard, driven through its modals and buttons against a
real test database."""
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

import register_team_wizard as wiz
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.mtgo_account import MtgoAccount
from models.team_finder import LfgTeam, LfgTeamMember
from models.tournament import TournamentParticipant, TournamentTeamMember
from services import tournament_registration as reg
from services import wallet_service
from services.tournament_service import create_tournament, register_team

GUILD = "1"
ROOK, WREN, BEN = 10, 20, 21


def _member(user_id, name, bot=False):
    m = MagicMock()
    m.id = user_id
    m.bot = bot
    m.display_name = name
    m.mention = f"<@{user_id}>"
    return m


MEMBERS = {ROOK: _member(ROOK, "Rook"), WREN: _member(WREN, "Wren"), BEN: _member(BEN, "Ben")}


def _interaction(user_id=ROOK):
    i = MagicMock()
    i.user = MEMBERS[user_id]
    i.guild.id = int(GUILD)
    i.guild.name = "Lotus Lounge"
    i.guild.get_member.side_effect = MEMBERS.get
    i.response.send_modal = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


async def _tournament(fee=0):
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Lotus Open", 3, entry_fee=fee)
        return t.id


async def _link(user_id=ROOK, name="rook_mtgo"):
    await MtgoAccount.link(user_id, name, int(GUILD))


@pytest.fixture
def quiet():
    """Everything that would reach Discord or the money stack, stubbed. ``dm`` is the
    teammate DM sender; tests set its return value."""
    dm = AsyncMock(return_value=True)
    with ExitStack() as stack:
        stack.enter_context(patch.object(reg, "refresh_boards", AsyncMock()))
        stack.enter_context(patch.object(reg, "reconcile_after_command", AsyncMock()))
        stack.enter_context(patch.object(reg, "sync_member", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(wiz, "gate_serve", return_value=None))
        stack.enter_context(patch.object(wiz, "send_dm", dm))
        yield dm


def _sent(i):
    """The (content, view) of the wizard's latest reply, however it was sent."""
    for call in (i.edit_original_response, i.response.edit_message, i.response.send_message):
        if call.await_count:
            kw = call.await_args.kwargs
            content = kw.get("content", call.await_args.args[0] if call.await_args.args else "")
            return content, kw.get("view")
    raise AssertionError("the wizard sent nothing")


def _button(view, label):
    return next(item for item in view.children if getattr(item, "label", None) == label)


async def _name(state_modal_interaction, name="Rook's Rookies"):
    """Submit the name modal that ``start`` (or a Rename button) opened."""
    modal = state_modal_interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, wiz.NameModal)
    modal.children[0].value = name
    i = _interaction()
    await modal.callback(i)
    return i


async def _through_to_confirm(name="Rook's Rookies"):
    start = _interaction()
    await wiz.start(start)
    teammates = await _name(start, name)
    _content, view = _sent(teammates)
    i = _interaction()
    await _button(view, "Next").callback(i)
    return i


async def _press_confirm(confirm_step):
    _content, view = _sent(confirm_step)
    label = next(item.label for item in view.children
                 if getattr(item, "label", "").startswith("Register"))
    i = _interaction()
    await _button(view, label).callback(i)
    return _sent(i)[0]


# --- Where the wizard starts ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_paid_event_asks_an_unlinked_captain_to_link_first(test_db, quiet):  # noqa: F811
    await _tournament(fee=20)
    i = _interaction()
    await wiz.start(i)
    assert isinstance(i.response.send_modal.await_args.args[0], wiz.LinkModal)


@pytest.mark.asyncio
async def test_a_linked_captain_goes_straight_to_the_team_name(test_db, quiet):  # noqa: F811
    await _tournament(fee=20)
    await _link()
    i = _interaction()
    await wiz.start(i)
    assert isinstance(i.response.send_modal.await_args.args[0], wiz.NameModal)


@pytest.mark.asyncio
async def test_a_free_event_never_asks_to_link(test_db, quiet):  # noqa: F811
    await _tournament(fee=0)
    i = _interaction()
    await wiz.start(i)
    assert isinstance(i.response.send_modal.await_args.args[0], wiz.NameModal)


@pytest.mark.asyncio
async def test_no_open_tournament_says_so(test_db, quiet):  # noqa: F811
    i = _interaction()
    await wiz.start(i)
    i.response.send_modal.assert_not_awaited()
    assert "isn't open" in _sent(i)[0]


# --- Linking ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_linking_confirms_and_offers_the_name_step(test_db, quiet):  # noqa: F811
    await _tournament(fee=20)
    start = _interaction()
    await wiz.start(start)
    modal = start.response.send_modal.await_args.args[0]
    modal.children[0].value = "rook_mtgo"
    i = _interaction()
    await modal.callback(i)

    content, view = _sent(i)
    assert "Linked as rook_mtgo" in content
    name_step = _interaction()
    await _button(view, "Name your team").callback(name_step)
    assert isinstance(name_step.response.send_modal.await_args.args[0], wiz.NameModal)


@pytest.mark.asyncio
async def test_a_username_someone_else_linked_names_them_and_stops(test_db, quiet):  # noqa: F811
    await _tournament(fee=20)
    await _link(user_id=WREN, name="rook_mtgo")
    start = _interaction()
    await wiz.start(start)
    modal = start.response.send_modal.await_args.args[0]
    modal.children[0].value = "rook_mtgo"
    i = _interaction()
    await modal.callback(i)

    content, view = _sent(i)
    assert f"<@{WREN}>" in content
    assert view is None


# --- Teammates ----------------------------------------------------------------------

async def _full_lfg_team(*member_ids):
    async with db_session() as session:
        team = LfgTeam(guild_id=GUILD, captain_id=str(ROOK), size=len(member_ids) + 1,
                       status="full", created_utc=datetime(2026, 10, 1))
        session.add(team)
        await session.flush()
        for uid in (ROOK, *member_ids):
            session.add(LfgTeamMember(team_id=team.id, user_id=str(uid),
                                      display_name=MEMBERS[uid].display_name,
                                      joined_utc=datetime(2026, 10, 1)))


@pytest.mark.asyncio
async def test_a_full_find_a_team_team_is_the_default_selection(test_db, quiet):  # noqa: F811
    await _tournament()
    await _full_lfg_team(WREN, BEN)
    start = _interaction()
    await wiz.start(start)
    teammates = await _name(start)

    content, view = _sent(teammates)
    assert "Step 2 of 3" in content and "Find a team" in content
    picker = next(item for item in view.children if isinstance(item, wiz.PrefilledUserSelect))
    defaults = picker.to_component_dict()["default_values"]
    assert defaults == [{"id": str(WREN), "type": "user"}, {"id": str(BEN), "type": "user"}]


@pytest.mark.asyncio
async def test_untouched_defaults_are_who_gets_registered(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    await _full_lfg_team(WREN, BEN)
    confirm = await _through_to_confirm()
    assert "Wren" in _sent(confirm)[0] and "Ben" in _sent(confirm)[0]

    await _press_confirm(confirm)

    async with db_session() as session:
        p = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().one()
        roster = (await session.execute(select(TournamentTeamMember.user_id).where(
            TournamentTeamMember.participant_id == p.id))).scalars().all()
    assert p.captain_user_id == str(ROOK)
    assert sorted(roster) == [str(WREN), str(BEN)]
    assert quiet.await_count == 2


@pytest.mark.asyncio
async def test_picking_drops_bots_and_the_captain(test_db, quiet):  # noqa: F811
    await _tournament()
    start = _interaction()
    await wiz.start(start)
    teammates = await _name(start)
    _content, view = _sent(teammates)
    picker = next(item for item in view.children if isinstance(item, wiz.PrefilledUserSelect))
    robot = _member(99, "Robot", bot=True)
    picker._selected_values = [str(WREN), "99", str(ROOK)]  # what Discord sends back
    i = _interaction()
    i.data = {"resolved": {}}
    with patch.object(type(picker), "values", new=[MEMBERS[WREN], robot, MEMBERS[ROOK]]):
        await picker.callback(i)

    content, view = _sent(i)
    assert view.state.teammate_ids == [str(WREN)]
    assert "Robot" in content


@pytest.mark.asyncio
async def test_no_teammates_is_fine(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    confirm = await _through_to_confirm()
    reply = await _press_confirm(confirm)

    assert "You're in" in reply
    async with db_session() as session:
        p = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().one()
        assert p.team_name == "Rook's Rookies"


# --- Confirming ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_name_taken_before_confirm_registers_nothing(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    confirm = await _through_to_confirm()
    async with db_session() as session:
        await register_team(session, t_id, "Rook's Rookies", str(BEN))

    reply = await _press_confirm(confirm)

    assert "pick another name" in reply
    async with db_session() as session:
        teams = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().all()
    assert [p.captain_user_id for p in teams] == [str(BEN)]


@pytest.mark.asyncio
async def test_a_taken_name_is_caught_at_the_name_step(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    async with db_session() as session:
        await register_team(session, t_id, "Rook's Rookies", str(BEN))
    start = _interaction()
    await wiz.start(start)
    i = await _name(start)

    content, view = _sent(i)
    assert "pick another name" in content
    rename = _interaction()
    await _button(view, "Rename").callback(rename)
    assert isinstance(rename.response.send_modal.await_args.args[0], wiz.NameModal)


@pytest.mark.asyncio
async def test_a_paid_confirm_shows_the_wallet_and_takes_the_fee(test_db, quiet):  # noqa: F811
    await _tournament(fee=20)
    await _link()
    await wallet_service.adjust(GUILD, str(ROOK), 35, "seed", "test")
    confirm = await _through_to_confirm()
    content = _sent(confirm)[0]
    assert "Step 3 of 3" in content
    assert "rook_mtgo" in content and "you have 35, leaving 15" in content

    reply = await _press_confirm(confirm)

    assert "You're in" in reply
    assert await wallet_service.get_balance(GUILD, str(ROOK)) == 15


@pytest.mark.asyncio
async def test_a_short_wallet_holds_the_spot_and_names_the_deposit(test_db, quiet):  # noqa: F811
    t_id = await _tournament(fee=20)
    await _link()
    await wallet_service.adjust(GUILD, str(ROOK), 12, "seed", "test")
    confirm = await _through_to_confirm()

    reply = await _press_confirm(confirm)

    assert "/wallet deposit 8" in reply
    async with db_session() as session:
        p = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().one()
        assert p.status != "paid"


@pytest.mark.asyncio
async def test_a_teammate_the_dm_missed_is_named_in_the_reply(test_db, quiet):  # noqa: F811
    await _tournament()
    await _full_lfg_team(WREN, BEN)
    quiet.side_effect = lambda _bot, user_id, _text, **_kw: user_id != str(BEN)
    confirm = await _through_to_confirm()

    reply = await _press_confirm(confirm)

    assert "You're in" in reply
    assert "couldn't DM Ben" in reply


@pytest.mark.asyncio
async def test_the_teammate_dm_says_who_added_them_to_what(test_db, quiet):  # noqa: F811
    await _tournament()
    await _full_lfg_team(WREN)
    confirm = await _through_to_confirm()
    await _press_confirm(confirm)

    _bot, user_id, text = quiet.await_args.args
    assert user_id == str(WREN)
    assert "Rook added you to **Rook's Rookies** for **Lotus Open** in Lotus Lounge" in text


@pytest.mark.asyncio
async def test_cancel_registers_nothing(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    confirm = await _through_to_confirm()
    _content, view = _sent(confirm)
    i = _interaction()
    await _button(view, "Cancel").callback(i)

    assert "nothing was registered" in _sent(i)[0]
    async with db_session() as session:
        assert (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().all() == []


# --- Pressing again with a team -----------------------------------------------------

async def _existing(t_id, name="Rook's Rookies", status=None):
    async with db_session() as session:
        p, _ = await register_team(session, t_id, name, str(ROOK))
        if status:
            p.status = status
        return p.id


async def _pick_and_add(view, *users):
    picker = next(item for item in view.children if isinstance(item, wiz.PrefilledUserSelect))
    i = _interaction()
    with patch.object(type(picker), "values", new=list(users)):
        await picker.callback(i)
    _content, view = _sent(i)
    add = _interaction()
    await _button(view, "Add").callback(add)
    return _sent(add)[0]


@pytest.mark.asyncio
async def test_a_registered_captain_is_asked_to_add_a_teammate(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    p_id = await _existing(t_id)
    i = _interaction()
    await wiz.start(i)

    i.response.send_modal.assert_not_awaited()
    content, view = _sent(i)
    assert "You captain **Rook's Rookies**" in content and "Add a teammate?" in content
    reply = await _pick_and_add(view, MEMBERS[WREN])

    assert "Added to **Rook's Rookies**" in reply
    async with db_session() as session:
        roster = (await session.execute(select(TournamentTeamMember.user_id).where(
            TournamentTeamMember.participant_id == p_id))).scalars().all()
    assert roster == [str(WREN)]
    assert quiet.await_args.args[1] == str(WREN)


@pytest.mark.asyncio
async def test_a_pending_team_is_told_what_is_left_to_deposit(test_db, quiet):  # noqa: F811
    t_id = await _tournament(fee=20)
    await _existing(t_id, status="pending")
    await wallet_service.adjust(GUILD, str(ROOK), 12, "seed", "test")
    i = _interaction()
    await wiz.start(i)

    assert "Payment pending: deposit 8 tix with `/wallet deposit 8`" in _sent(i)[0]


@pytest.mark.asyncio
async def test_a_paid_team_has_no_payment_line(test_db, quiet):  # noqa: F811
    t_id = await _tournament(fee=20)
    await _existing(t_id, status="paid")
    i = _interaction()
    await wiz.start(i)

    assert "Payment pending" not in _sent(i)[0]


@pytest.mark.asyncio
async def test_a_captain_of_two_teams_picks_one_first(test_db, quiet):  # noqa: F811
    t_id = await _tournament()
    await _existing(t_id, "Alpha")
    bravo = await _existing(t_id, "Bravo")
    i = _interaction()
    await wiz.start(i)

    content, view = _sent(i)
    assert "Which one?" in content
    which = next(item for item in view.children if isinstance(item, discord.ui.Select))
    pick = _interaction()
    with patch.object(type(which), "values", new=[str(bravo)]):
        await which.callback(pick)
    assert "You captain **Bravo**" in _sent(pick)[0]
