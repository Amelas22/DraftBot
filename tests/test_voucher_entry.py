"""Entering a staked draft with a voucher: DraftBot pays the 20 tix entry.

Driven through the real stake select against a scratch database, because what
matters is what the ledger holds afterwards and that a refusal writes nothing.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from conftest import seed_session, test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.draft_session import DraftSession
from models.stake import StakeInfo
from models.wallet_tx import TIX, WalletTx
from services import draft_pool_service as pool
from services import voucher_service as vs
from services import wallet_service

GUILD = "99"
BOT = "bot"
PID = "11111"
SID = "s1"


def _interaction(user_id=11111):
    i = MagicMock()
    i.user.id = user_id
    i.user.display_name = "Ada"
    i.guild_id = int(GUILD)
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _said(interaction):
    calls = interaction.response.send_message.await_args_list
    return " ".join(str(c.args[0]) for c in calls if c.args)


async def _give_vouchers(count, to=PID):
    await wallet_service.adjust(GUILD, BOT, vs.VOUCHER_VALUE * count, "seed", "t")
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            await vs.mint_in(session, GUILD, BOT, to, count, f"mint:{to}:{count}")
            await session.commit()


async def _bal(holder):
    async with db_session() as session:
        return await wallet_service.balance_in(session, GUILD, holder, asset=TIX)


async def _rows():
    async with db_session() as session:
        return (await session.execute(select(func.count()).select_from(WalletTx))).scalar_one()


async def _enter_through_the_menu():
    from views import StakeOptionsSelect

    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = SID
    chooser.has_draftmancer_role = False
    interaction = _interaction()
    with patch("config.vouchers_enabled", return_value=True), \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"):
        await StakeOptionsSelect.handle_stake_submission(
            chooser, interaction, 20, is_capped=True, voucher=True)
    return interaction


async def _enter_direct():
    with patch("config.vouchers_enabled", return_value=True):
        async with wallet_service.MONEY_LOCK:
            async with db_session() as session:
                async with session.begin():
                    return await vs.enter_with_voucher_in(session, GUILD, SID, PID)


@pytest.mark.asyncio
async def test_voucher_entry_moves_the_voucher_and_puts_twenty_in_the_pot_from_the_reserve(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PID, 7, "seed", "t")
    await _give_vouchers(2)

    interaction = await _enter_through_the_menu()

    assert "Entered with a voucher" in _said(interaction), _said(interaction)
    assert await _bal(PID) == 7, "the player's own tix moved"
    assert await pool.pool_balance(GUILD, SID) == 20
    assert (await pool.contributions(GUILD, SID))[PID] == 20
    assert await vs.vouchers_held(GUILD, PID) == 1
    assert await _bal(vs.RESERVE) == 20
    async with db_session() as session:
        assert await vs.entered_with_voucher_in(session, GUILD, SID, PID)
        signed = (await session.execute(select(DraftSession.sign_ups)
                  .where(DraftSession.session_id == SID))).scalars().first()
        stake = (await session.execute(select(StakeInfo)
                 .where(StakeInfo.session_id == SID))).scalars().one()
    assert PID in signed
    assert stake.max_stake == 20 and stake.is_capped is True
    assert await vs.entered_with_voucher(GUILD, SID, PID)


@pytest.mark.asyncio
async def test_voucher_entry_skips_the_funds_check(test_db):  # noqa: F811
    from models.debt_ledger import DebtLedger

    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    async with db_session() as db:
        db.add(DebtLedger(guild_id=GUILD, player_id=PID, counterparty_id="other",
                          amount=-50, source_type="draft", source_id="old-draft"))
        db.add(DebtLedger(guild_id=GUILD, player_id="other", counterparty_id=PID,
                          amount=50, source_type="draft", source_id="old-draft"))
        await db.commit()

    interaction = await _enter_through_the_menu()

    assert "Entered with a voucher" in _said(interaction), _said(interaction)
    assert await pool.held_by(GUILD, SID, PID) == 20
    assert await _bal(PID) == 0


@pytest.mark.asyncio
async def test_voucher_entry_refuses_a_second_entry_or_a_double_click(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(3)

    assert await _enter_direct() is None
    rows = await _rows()
    second = await _enter_direct()

    assert second == ("You're already entered in this draft. "
                      "Cancel your sign-up first to use a voucher.")
    assert await _rows() == rows, "a refused entry wrote rows"
    assert await vs.vouchers_held(GUILD, PID) == 2
    assert await pool.held_by(GUILD, SID, PID) == 20


@pytest.mark.asyncio
async def test_voucher_entry_refuses_a_player_already_in_with_tix(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    await wallet_service.adjust(GUILD, PID, 10, "seed", "t")
    await pool.set_entry(GUILD, SID, PID, 10)

    assert (await _enter_direct()).startswith("You're already entered")
    assert await vs.vouchers_held(GUILD, PID) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["donation", "tournament", "min_stake"])
async def test_voucher_entry_refuses_donation_tournament_and_min_stake_above_twenty(test_db, kind):  # noqa: F811
    from sqlalchemy import update

    await seed_session(SID, guild=GUILD, stype="premade" if kind == "tournament" else "staked",
                       stage=None, sign_ups={}, donation=(kind == "donation"))
    if kind == "min_stake":
        async with db_session() as session:
            await session.execute(update(DraftSession)
                                  .where(DraftSession.session_id == SID)
                                  .values(min_stake=50))
            await session.commit()
    await _give_vouchers(1)
    rows = await _rows()

    refusal = await _enter_direct()

    expected = ("This draft's minimum entry is above 20 tix, so a voucher can't cover it."
                if kind == "min_stake" else
                "Vouchers can only be used for staked drafts.")
    assert refusal == expected
    assert await _rows() == rows
    assert await vs.vouchers_held(GUILD, PID) == 1


@pytest.mark.asyncio
async def test_voucher_entry_refuses_when_off_unheld_or_the_reserve_is_short(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    rows = await _rows()

    with patch("config.vouchers_enabled", return_value=False):
        async with db_session() as session:
            assert (await vs.enter_with_voucher_in(session, GUILD, SID, PID)
                    == "Draft vouchers aren't available on this server.")
    assert await _rows() == rows

    assert await _enter_direct() == "You don't have a draft voucher."
    assert await _rows() == rows

    await _give_vouchers(1)
    async with db_session() as session:
        # Drain the reserve behind the voucher's back.
        await wallet_service.transfer_in(session, GUILD, vs.RESERVE, BOT, 5, "drain",
                                         asset=TIX)
        await session.commit()
    rows = await _rows()
    assert (await _enter_direct()
            == "Vouchers can't be used right now. Please let an admin know.")
    assert await _rows() == rows


def _labels(**kw):
    from views import StakeOptionsSelect

    select_ = StakeOptionsSelect(SID, "link", "Ada", **kw)
    return [(o.label, o.value) for o in select_.options]


def test_voucher_option_shown_only_to_holders_where_min_stake_allows():
    label = "🎟️ Use a voucher: 20 tix entry, paid by DraftBot (you have 2)"
    assert _labels(min_stake=10, vouchers=2)[0] == (label, "voucher")
    assert _labels(min_stake=20, vouchers=2)[0] == (label, "voucher")
    assert all(v != "voucher" for _, v in _labels(min_stake=50, vouchers=2))
    assert all(v != "voucher" for _, v in _labels(min_stake=10, vouchers=0))
    assert all(v != "voucher" for _, v in _labels(min_stake=10))


@pytest.mark.asyncio
async def test_choosing_the_voucher_option_enters_with_a_voucher(test_db):  # noqa: F811
    from views import StakeOptionsSelect

    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = SID
    chooser.handle_stake_submission = AsyncMock()
    interaction = _interaction()
    with patch("preference_service.get_player_bet_capping_preference",
               new=AsyncMock(return_value=False)), \
         patch.object(StakeOptionsSelect, "values", new=["voucher"]):
        await StakeOptionsSelect.callback(chooser, interaction)

    chooser.handle_stake_submission.assert_awaited_once_with(
        interaction, 20, is_capped=False, voucher=True)


@pytest.mark.asyncio
async def test_change_entry_is_refused_for_a_voucher_entry(test_db):  # noqa: F811
    from views import BetCapToggleButton

    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    await _enter_through_the_menu()

    button = BetCapToggleButton.__new__(BetCapToggleButton)
    button.draft_session_id = SID
    interaction = _interaction()
    await BetCapToggleButton.bet_cap_callback(button, interaction, MagicMock())

    assert _said(interaction) == ("You entered with a voucher. To change your entry, "
                                  "cancel your sign-up (your voucher comes back) and join again.")


CHANGE_REFUSAL = ("You entered with a voucher. To change your entry, cancel your "
                  "sign-up (your voucher comes back) and join again.")


async def _voucher_entered_with_spare_tix():
    """A voucher entry by a player who also holds 100 tix of their own."""
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, PID, 100, "seed", "t")
    await _give_vouchers(1)
    await _enter_through_the_menu()
    assert await pool.held_by(GUILD, SID, PID) == 20
    return await _rows()


async def _assert_untouched(rows, interaction):
    assert _said(interaction) == CHANGE_REFUSAL, _said(interaction)
    assert await _rows() == rows, "a refused stake change wrote rows"
    assert await pool.pool_balance(GUILD, SID) == 20
    assert await _bal(PID) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [50, 10])
async def test_a_second_stale_menu_cannot_top_up_or_cash_out_a_voucher_entry(test_db, amount):  # noqa: F811
    from views import StakeOptionsSelect

    rows = await _voucher_entered_with_spare_tix()
    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = SID
    chooser.has_draftmancer_role = False
    interaction = _interaction()
    with patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"):
        await StakeOptionsSelect.handle_stake_submission(chooser, interaction, amount)

    await _assert_untouched(rows, interaction)


@pytest.mark.asyncio
async def test_the_over_100_modal_cannot_top_up_a_voucher_entry(test_db):  # noqa: F811
    from views import StakeModal

    rows = await _voucher_entered_with_spare_tix()
    await wallet_service.adjust(GUILD, PID, 50, "seed", "t")  # enough to pass the funds gate
    rows += 1
    modal = StakeModal.__new__(StakeModal)
    modal.draft_session_id = SID
    modal.over_100 = True
    modal.has_draftmancer_role = False
    modal.user_display_name = "Ada"
    modal.stake_input = MagicMock(value="150")
    modal.cap_checkbox = MagicMock(value="yes")
    interaction = _interaction()
    with patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"), \
         patch("preference_service.update_player_bet_capping_preference", new=AsyncMock()):
        await StakeModal.callback(modal, interaction)

    assert _said(interaction) == CHANGE_REFUSAL, _said(interaction)
    assert await _rows() == rows, "a refused stake change wrote rows"
    assert await pool.pool_balance(GUILD, SID) == 20
    assert await _bal(PID) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [50, 10])
async def test_a_stale_change_entry_panel_cannot_revise_a_voucher_entry(test_db, amount):  # noqa: F811
    from views import CombinedStakeSelect

    rows = await _voucher_entered_with_spare_tix()
    chooser = CombinedStakeSelect.__new__(CombinedStakeSelect)
    chooser.draft_session_id = SID
    chooser.current_stake = 0  # opened before the voucher entry
    interaction = _interaction()
    with patch("views.update_draft_message", new=AsyncMock()):
        await CombinedStakeSelect.handle_stake_submission(chooser, interaction, amount)

    await _assert_untouched(rows, interaction)


@pytest.mark.asyncio
async def test_a_pool_refusal_of_a_voucher_entry_rolls_it_all_back(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    rows = await _rows()

    with patch("services.draft_pool_service.entry_in",
               new=AsyncMock(return_value={"ok": False, "deficit": 0})):
        interaction = await _enter_through_the_menu()

    assert _said(interaction) == ("Your voucher entry couldn't be completed, so nothing "
                                  "moved. The draft may no longer be taking sign-ups.")
    assert await _rows() == rows
    assert await vs.vouchers_held(GUILD, PID) == 1


async def _enter_ordinary_through_the_menu(is_capped):
    from views import StakeOptionsSelect

    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = SID
    chooser.has_draftmancer_role = False
    interaction = _interaction()
    with patch("config.vouchers_enabled", return_value=True), \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"):
        await StakeOptionsSelect.handle_stake_submission(
            chooser, interaction, 20, is_capped=is_capped)
    return interaction


@pytest.mark.asyncio
async def test_the_voucher_confirmation_points_nowhere_but_an_ordinary_one_keeps_its_pointer(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    said = _said(await _enter_through_the_menu())
    assert "Entered with a voucher" in said
    assert "Your entry is **capped** to your share of your team." in said
    assert "Change Entry" not in said

    await seed_session("s2", guild=GUILD, stype="staked", stage=None, sign_ups={})
    await wallet_service.adjust(GUILD, "22222", 100, "seed", "t")
    from views import StakeOptionsSelect
    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = "s2"
    chooser.has_draftmancer_role = False
    interaction = _interaction(22222)
    with patch("config.vouchers_enabled", return_value=True), \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"):
        await StakeOptionsSelect.handle_stake_submission(chooser, interaction, 20, is_capped=True)
    ordinary = _said(interaction)
    assert "use **Change Entry / Settings** to turn it off." in ordinary, ordinary


@pytest.mark.asyncio
async def test_an_uncapped_voucher_confirmation_says_nothing_about_the_cap(test_db):  # noqa: F811
    from views import StakeOptionsSelect

    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    chooser = StakeOptionsSelect.__new__(StakeOptionsSelect)
    chooser.draft_session_id = SID
    chooser.has_draftmancer_role = False
    interaction = _interaction()
    with patch("config.vouchers_enabled", return_value=True), \
         patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.library_after_sign_up_soon"):
        await StakeOptionsSelect.handle_stake_submission(
            chooser, interaction, 20, is_capped=False, voucher=True)
    said = _said(interaction)
    assert "Entered with a voucher" in said
    assert "capped" not in said and "Change Entry" not in said


@pytest.mark.asyncio
async def test_a_negative_wallet_is_refused_up_front_with_nothing_written(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    await wallet_service.adjust(GUILD, PID, -5, "seed", "t")
    rows = await _rows()
    assert (await _enter_direct()
            == "Your wallet is below zero, so a voucher can't be used until that's settled.")
    assert await _rows() == rows


@pytest.mark.asyncio
async def test_a_retired_voucher_is_not_handed_back_by_a_later_refund(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={})
    await _give_vouchers(1)
    assert await _enter_direct() is None
    async with db_session() as session:
        await vs.retire_in(session, GUILD, SID)
        await session.commit()
    # The pot is refunded after the voucher was retired: the sweep still runs,
    # but there is no voucher left to hand back.
    async with wallet_service.MONEY_LOCK:
        async with db_session() as session:
            async with session.begin():
                await pool.entry_in(session, GUILD, SID, PID, 0, "left")
    async with db_session() as session:
        assert await wallet_service.balance_in(session, GUILD, PID, asset=vs.VOUCHER) == 0
        assert await wallet_service.balance_in(session, GUILD, vs.RETIRED, asset=vs.VOUCHER) == 1
        assert await wallet_service.balance_in(session, GUILD, PID, asset=TIX) == 0
        assert await wallet_service.balance_in(session, GUILD, vs.RESERVE, asset=TIX) == 20


async def _cancel(user_id=11111):
    from views import PersistentView

    view = PersistentView.__new__(PersistentView)
    view.draft_session_id = SID
    interaction = _interaction(user_id)
    with patch("views.update_draft_message", new=AsyncMock()), \
         patch("views.SignUpHistory.record_signup_event", new=AsyncMock()):
        await PersistentView.cancel_sign_up_callback(view, interaction, MagicMock())
    return interaction


@pytest.mark.asyncio
async def test_cancelling_a_voucher_entry_says_the_voucher_is_back(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={PID: "Ada"})
    await _give_vouchers(1)
    assert await _enter_direct() is None
    said = _said(await _cancel())
    assert said == "Your sign up has been canceled! Your draft voucher is back in your wallet."
    assert await vs.vouchers_held(GUILD, PID) == 1


@pytest.mark.asyncio
async def test_cancelling_an_ordinary_entry_does_not_mention_a_voucher(test_db):  # noqa: F811
    await seed_session(SID, guild=GUILD, stype="staked", stage=None, sign_ups={PID: "Ada"})
    assert _said(await _cancel()) == "Your sign up has been canceled!"
