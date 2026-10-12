"""Every sign-up path starts the library step, and only on a sign-up that held.

The library is asked about, or its copy reserved, right after a sign-up is
confirmed (services/library_prompt.library_after_sign_up_soon). There are four
places a player ends up signed up -- a non-staked Sign Up, a preset stake, the
over-100 stake modal, and joining a premade team -- and losing the call from
any of them is silent: those players are never asked, nothing is reserved, and
they get no deck. So each is driven here through the real view code, with the
board redraw that follows it made to fail: the step is started before it, and a
redraw that breaks must not take the library with it.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import seed_session
from services import wallet_service

pytestmark = pytest.mark.asyncio

GUILD = "99"
PLAYER = "11111"


def _interaction(user_id=PLAYER):
    i = MagicMock()
    i.user.id = int(user_id)
    i.user.display_name = "Ada"
    i.user.roles = []
    i.guild_id = int(GUILD)
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _started(mock):
    """The draft ids the library step was started for."""
    return [str(c.args[1].session_id) for c in mock.call_args_list]


def _redraw_fails():
    return patch("views.update_draft_message",
                 new=AsyncMock(side_effect=RuntimeError("discord said no")))


# --- staked: the preset stakes and the over-100 modal -------------------------

async def test_a_preset_stake_starts_the_library_step_even_if_the_redraw_fails(test_db):
    from views import StakeOptionsSelect

    await seed_session("s1", guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PLAYER, 100, "seed", "t")
    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = "s1"
    chooser.has_draftmancer_role = False

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.library_after_sign_up_soon") as library, _redraw_fails():
        with pytest.raises(RuntimeError):
            await StakeOptionsSelect.handle_stake_submission(chooser, _interaction(), 20)

    assert _started(library) == ["s1"]


async def test_a_stake_that_was_refused_starts_nothing(test_db):
    from views import StakeOptionsSelect

    await seed_session("s1", guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PLAYER, 10, "seed", "t")
    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = "s1"
    chooser.has_draftmancer_role = False

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.library_after_sign_up_soon") as library, \
         patch("views.update_draft_message", new=AsyncMock()):
        await StakeOptionsSelect.handle_stake_submission(chooser, _interaction(), 40)

    library.assert_not_called()


async def test_the_over_100_modal_starts_the_library_step_even_if_the_redraw_fails(test_db):
    from views import StakeModal

    await seed_session("s1", guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PLAYER, 200, "seed", "t")
    modal = StakeModal.__new__(StakeModal)
    modal.draft_session_id = "s1"
    modal.over_100 = True
    modal.has_draftmancer_role = False
    modal.user_display_name = "Ada"
    modal.stake_input = MagicMock(value="150")
    modal.cap_checkbox = MagicMock(value="yes")

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.library_after_sign_up_soon") as library, _redraw_fails(), \
         patch("preference_service.update_player_bet_capping_preference",
               new=AsyncMock()):
        await StakeModal.callback(modal, _interaction())

    assert _started(library) == ["s1"]


async def test_the_over_100_modal_refused_starts_nothing(test_db):
    from views import StakeModal

    await seed_session("s1", guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PLAYER, 60, "seed", "t")
    modal = StakeModal.__new__(StakeModal)
    modal.draft_session_id = "s1"
    modal.over_100 = True
    modal.has_draftmancer_role = False
    modal.user_display_name = "Ada"
    modal.stake_input = MagicMock(value="150")
    modal.cap_checkbox = MagicMock(value="yes")

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.library_after_sign_up_soon") as library, \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("preference_service.update_player_bet_capping_preference",
               new=AsyncMock()):
        await StakeModal.callback(modal, _interaction())

    library.assert_not_called()


# --- non-staked Sign Up --------------------------------------------------------

def _board(session_type):
    from views import PersistentView
    return PersistentView(bot=None, draft_session_id="s1", session_type=session_type)


async def test_a_plain_sign_up_starts_the_library_step_even_if_the_redraw_fails(test_db):
    from datetime import datetime
    await seed_session("s1", guild=GUILD, stype="random", stage=None, sign_ups={},
                       start=datetime.now())
    view = _board("random")

    with patch("config.get_config", lambda gid: {"roles": {}}), \
         patch("views.library_after_sign_up_soon") as library, _redraw_fails():
        with pytest.raises(RuntimeError):
            await view.sign_up_callback(_interaction(), MagicMock())

    assert _started(library) == ["s1"]


async def test_signing_up_twice_starts_nothing_the_second_time(test_db):
    from datetime import datetime
    await seed_session("s1", guild=GUILD, stype="random", stage=None,
                       sign_ups={PLAYER: "Ada"}, start=datetime.now())
    view = _board("random")

    with patch("config.get_config", lambda gid: {"roles": {}}), \
         patch("views.library_after_sign_up_soon") as library, \
         patch("views.update_draft_message", new=AsyncMock()):
        await view.sign_up_callback(_interaction(), MagicMock())

    library.assert_not_called()


async def test_a_staked_sign_up_waits_for_the_stake(test_db):
    """The staked Sign Up only shows the stake picker; the library step comes
    with the stake, once the money is in."""
    from datetime import datetime
    await seed_session("s1", guild=GUILD, stype="staked", stage=None, sign_ups={},
                       start=datetime.now())
    view = _board("staked")

    with patch("config.get_config", lambda gid: {"roles": {}}), \
         patch("views.library_after_sign_up_soon") as library:
        await view.sign_up_callback(_interaction(), MagicMock())

    library.assert_not_called()


# --- premade: joining a team ---------------------------------------------------

async def _premade(fee=0, team_a=()):
    from sqlalchemy import update
    from database.db_session import db_session
    from models.draft_session import DraftSession
    await seed_session("s1", guild=GUILD, stype="premade", stage=None,
                       teams=(list(team_a), []),
                       sign_ups={p: p for p in team_a})
    if fee:
        async with db_session() as s:
            await s.execute(update(DraftSession)
                            .where(DraftSession.session_id == "s1")
                            .values(entry_fee=fee))
    return _board("premade")


def _team_button(view, team):
    return next(b for b in view.children
                if getattr(b, "custom_id", None) == f"{team}_s1")


def _redraw_team_fails(view):
    return patch.object(type(view), "update_team_view",
                        new=AsyncMock(side_effect=RuntimeError("message gone")))


async def test_joining_a_team_starts_the_library_step_even_if_the_redraw_fails(test_db):
    view = await _premade()

    with patch("views.library_after_sign_up_soon") as library, _redraw_team_fails(view):
        with pytest.raises(RuntimeError):
            await view.team_assignment_callback(_interaction(), _team_button(view, "Team_A"))

    assert _started(library) == ["s1"]


async def test_leaving_a_team_starts_nothing(test_db):
    view = await _premade(team_a=(PLAYER,))

    with patch("views.library_after_sign_up_soon") as library, \
         patch.object(type(view), "update_team_view", new=AsyncMock()):
        await view.team_assignment_callback(_interaction(), _team_button(view, "Team_A"))

    library.assert_not_called()


async def test_a_team_join_that_cannot_pay_the_fee_starts_nothing(test_db):
    view = await _premade(fee=50)
    await wallet_service.adjust(GUILD, PLAYER, 10, "seed", "t")

    with patch("views.library_after_sign_up_soon") as library, \
         patch.object(type(view), "update_team_view", new=AsyncMock()):
        await view.team_assignment_callback(_interaction(), _team_button(view, "Team_A"))

    library.assert_not_called()
