"""The stake menu offers 20, 50 and 100; "Other amount…" takes any multiple of 10."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)


def _values(options):
    return [o.value for o in options]


@pytest.mark.parametrize("min_stake, expected", [
    (10, ["20", "50", "100", "other"]),
    (20, ["20", "50", "100", "other"]),
    (50, ["50", "100", "other"]),
    (100, ["100", "other"]),
    (150, ["other"]),
])
def test_the_menu_offers_the_presets_at_or_above_the_minimum(min_stake, expected):
    from views import stake_menu_options

    assert _values(stake_menu_options(min_stake)) == expected


def test_other_amount_is_labelled_for_any_amount():
    from views import stake_menu_options

    other = stake_menu_options(20)[-1]
    assert other.label == "Other amount…"


def test_the_signup_menu_uses_the_shared_options():
    from views import StakeOptionsSelect

    select = StakeOptionsSelect("s1", "link", "Ada", 20)
    assert _values(select.options) == ["20", "50", "100", "other"]


def test_the_change_entry_menu_uses_the_shared_options():
    # The Change Entry panel builds its menu inline; it must not drift from
    # the sign-up menu again.
    import inspect

    import views

    src = inspect.getsource(views)
    assert "Over 100 TIX" not in src
    assert src.count("stake_menu_options(") >= 3  # definition + both menus


def _interaction(user_id=11111):
    i = MagicMock()
    i.user.id = user_id
    i.guild_id = 99
    i.response.send_message = AsyncMock()
    return i


async def _submit(amount):
    """Run the typed box with `amount`. Returns (interaction, reached_funding):
    reaching the funds check means the amount itself was accepted."""
    from conftest import seed_session
    from views import StakeModal

    from sqlalchemy import update

    from database.db_session import db_session
    from models.draft_session import DraftSession

    await seed_session("s1", guild="99", stype="staked", stage=None, sign_ups={})
    async with db_session() as session:
        await session.execute(update(DraftSession)
                              .where(DraftSession.session_id == "s1").values(min_stake=20))
    modal = StakeModal.__new__(StakeModal)
    modal.draft_session_id = "s1"
    modal.has_draftmancer_role = False
    modal.user_display_name = "Ada"
    modal.stake_input = MagicMock(value=str(amount))
    modal.cap_checkbox = MagicMock(value="yes")
    interaction = _interaction()
    funding = AsyncMock(return_value=True)  # stop at the funds check
    with patch("views.refuse_unfunded_stake", new=funding):
        await StakeModal.callback(modal, interaction)
    return interaction, funding.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [20, 30, 40, 70, 90, 100, 110, 120, 150, 330])
async def test_any_multiple_of_ten_at_or_above_the_minimum_is_accepted(test_db, amount):  # noqa: F811
    _, reached = await _submit(amount)
    assert reached, f"{amount} was refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [25, 35, 75, 105, 155])
async def test_an_amount_that_is_not_a_multiple_of_ten_is_refused(test_db, amount):  # noqa: F811
    interaction, reached = await _submit(amount)
    assert not reached
    said = interaction.response.send_message.await_args
    assert said.args[0] == "Your entry must be a multiple of 10 tix, like 30, 70 or 120."
    assert said.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_an_amount_below_the_drafts_minimum_is_refused(test_db):  # noqa: F811
    interaction, reached = await _submit(10)  # the draft's minimum is 20
    assert not reached
    assert "Minimum stake for this draft is" in interaction.response.send_message.await_args.args[0]
