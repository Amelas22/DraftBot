"""Joining a staked queue takes the money, or refuses the join.

The obligation is funded from the moment it is taken on. With the flag off the
old behaviour must be exactly untouched, because the pool and the legacy debt
path must never both run against one draft.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from views import CombinedStakeSelect, StakeOptionsSelect


def _interaction(user_id=11111):
    i = MagicMock()
    i.user.id = user_id
    i.user.display_name = "Ada"
    i.guild_id = 1
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _said(interaction):
    calls = interaction.response.send_message.await_args_list
    return " ".join(str(c.args[0]) for c in calls if c.args)


@pytest.mark.asyncio
async def test_an_unfunded_player_is_refused_and_shown_the_shortfall(test_db):
    """And nothing is written: no sign-up row, no StakeInfo, nothing to unwind.

    Driven against a real database rather than a mocked session, because the
    charge now happens INSIDE the sign-up transaction -- the thing worth
    checking is that the transaction rolls back, which a mock cannot show.
    """
    from sqlalchemy import select as sa_select

    from conftest import seed_session
    from database.db_session import db_session
    from models.draft_session import DraftSession
    from models.stake import StakeInfo
    from services import wallet_service

    await seed_session("s1", guild="99", stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust("99", "11111", 10, "seed", "t")

    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = "s1"
    chooser.has_draftmancer_role = False
    interaction = _interaction()
    interaction.guild_id = 99

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)):
        await StakeOptionsSelect.handle_stake_submission(chooser, interaction, 40)

    assert "30" in _said(interaction), (
        f"the shortfall was not shown to the player: {_said(interaction)!r}")
    assert await wallet_service.get_balance("99", "11111") == 10, "money moved"
    async with db_session() as s:
        signed = (await s.execute(sa_select(DraftSession.sign_ups)
                  .where(DraftSession.session_id == "s1"))).scalars().first()
        stakes = (await s.execute(sa_select(StakeInfo)
                  .where(StakeInfo.session_id == "s1"))).scalars().all()
    assert not signed, f"an unfunded player was seated: {signed}"
    assert not stakes, "a StakeInfo row survived a refused entry"


async def _revising(balance, held):
    """A player mid-queue with `held` already in the pool and `balance` spare."""
    from conftest import seed_session
    from services import draft_pool_service as pool
    from services import wallet_service

    await seed_session("s1", guild="99", stype="staked", stage=None,
                       sign_ups={"11111": "Ada"})
    await wallet_service.adjust("99", "11111", balance + held, "seed", "t")
    await pool.set_entry("99", "s1", "11111", held)

    chooser = CombinedStakeSelect.__new__(CombinedStakeSelect)
    chooser.draft_session_id = "s1"
    chooser.current_stake = held
    interaction = _interaction()
    interaction.guild_id = 99
    return chooser, interaction


@pytest.mark.asyncio
async def test_raising_a_stake_charges_only_the_gap(test_db):
    """The player already holds their old stake, so only the difference moves."""
    from services import draft_pool_service as pool
    from services import wallet_service

    chooser, interaction = await _revising(balance=100, held=20)

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.update_draft_message", new=AsyncMock()):
        await CombinedStakeSelect.handle_stake_submission(chooser, interaction, 50)

    assert await pool.held_by("99", "s1", "11111") == 50
    assert await wallet_service.get_balance("99", "11111") == 70, (
        "the whole 50 was charged instead of the 30 gap")


@pytest.mark.asyncio
async def test_a_raise_they_cannot_afford_leaves_the_stake_alone(test_db):
    from services import draft_pool_service as pool
    from services import wallet_service

    chooser, interaction = await _revising(balance=10, held=20)

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.update_draft_message", new=AsyncMock()):
        await CombinedStakeSelect.handle_stake_submission(chooser, interaction, 50)

    assert "20" in _said(interaction)
    assert "unchanged" in _said(interaction).lower()
    assert await pool.held_by("99", "s1", "11111") == 20, "the old stake moved"
    assert await wallet_service.get_balance("99", "11111") == 10


@pytest.mark.asyncio
async def test_the_over_100_modal_also_charges(test_db):
    """The modal behind "Over 100 TIX" is a fourth signup writer. Missing it
    meant the LARGEST stakes in the game were the only ones entering free."""
    from views import StakeModal

    from conftest import seed_session
    from services import wallet_service
    from services.draft_pool_service import pool_balance

    await seed_session("s1", guild="99", stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust("99", "11111", 60, "seed", "t")

    modal = StakeModal.__new__(StakeModal)
    modal.draft_session_id = "s1"
    modal.over_100 = True
    modal.has_draftmancer_role = False
    modal.user_display_name = "Ada"
    modal.stake_input = MagicMock(value="150")
    modal.cap_checkbox = MagicMock(value="yes")
    interaction = _interaction()
    interaction.guild_id = 99

    with patch("views.refuse_unfunded_stake", new=AsyncMock(return_value=False)), \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("preference_service.update_player_bet_capping_preference",
               new=AsyncMock()):
        await StakeModal.callback(modal, interaction)

    # 150 wanted, 60 held: refused, nothing taken, and the gap is named.
    assert "90" in _said(interaction), _said(interaction)
    assert await wallet_service.get_balance("99", "11111") == 60
    assert await pool_balance("99", "s1") == 0


@pytest.mark.asyncio
async def test_an_admin_removing_a_player_returns_their_entry(test_db):
    """Removal is a leave. Without the refund their tix sit in the pool with no
    owner and no later path that attributes them."""
    from views import UserRemovalSelect

    from discord import ComponentType

    select = UserRemovalSelect.__new__(UserRemovalSelect)
    select.session_id = "s1"
    select._selected_values = ["777"]
    select._interaction = object()
    # Select.values reads _underlying.type to decide how to resolve; without it
    # the callback dies before it reaches the refund.
    select._underlying = SimpleNamespace(type=ComponentType.string_select)
    interaction = _interaction()
    interaction.client = MagicMock()

    from conftest import seed_session
    from services import draft_pool_service as pool
    from services import wallet_service

    await seed_session("s1", guild="99", stype="staked", stage=None,
                       sign_ups={"777": "Zed"})
    await wallet_service.adjust("99", "777", 100, "seed", "t")
    await pool.set_entry("99", "s1", "777", 40)

    session = await __import__("utils", fromlist=["x"]).get_draft_session("s1")

    with patch("views.get_draft_session", new=AsyncMock(return_value=session)), \
         patch("views.update_draft_message", new=AsyncMock()):
        try:
            await UserRemovalSelect.callback(select, interaction)
        except Exception:
            pass  # the Discord half is stubbed; the refund is what is asserted

    # The refund and the roster change committed together.
    assert await pool.held_by("99", "s1", "777") == 0, "the entry was not returned"
    assert await wallet_service.get_balance("99", "777") == 100


def test_the_over_100_modal_validates_the_minimum_before_charging():
    """A rejected stake must not have already taken the money.

    The min-stake check lived inside the sign-up transaction, which opens after
    the charge. A player entering below a draft's minimum through the "Over 100
    TIX" modal was told no with their tix already in the holder and their name
    not in the queue -- and since the modal is the path for the LARGEST stakes,
    it was the largest amounts that could be stranded that way.
    """
    import ast
    import inspect

    import views

    import textwrap

    src = textwrap.dedent(inspect.getsource(views.StakeModal.callback))
    fn = ast.parse(src).body[0]

    charges = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)
               and n.func.id in ("set_entry", "entry_in")]
    guards = [n.lineno for n in ast.walk(fn)
              if isinstance(n, ast.Name) and n.id == "min_stake"]
    assert charges, "the modal no longer charges; re-check this guard"
    assert guards, "the modal no longer checks the draft's minimum stake"
    assert min(guards) < min(charges), (
        "the charge runs before the minimum-stake check, so a stake below the "
        "minimum is refused with the money already taken")
