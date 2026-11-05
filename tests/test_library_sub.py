"""Offering an absent teammate's pool to a sub.

A pool is lent to one person at a time, so every check here is about who
already holds what: the absent player, the sub, or another sub. The caller
holds the draft's panel lock around check_sub and offer_to_sub; offer_to_sub
re-runs the two pool checks so two Confirms for one pool cannot both land.
"""
from unittest.mock import patch

import pytest
from sqlalchemy import update

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from services.card_lending_service import active_loan, assign_deck, sub_loan_for
from services.library_sub import (
    chat_line, check_sub, confirm_reply, offer_to_sub, sub_context,
    sub_return_texts)
from test_draft_deck_assignment import (  # noqa: F401 -- library_on is a fixture
    ALICE, BOB, GUILD, LIBRARY, SESSION, _loans, _log, _open_chat, _reported,
    _seed, _stamp_draft, library_on)

pytestmark = pytest.mark.asyncio

CY = "1003"


def _log_with_cy():
    """The sibling file's log plus a third seat, so three sign-ups line up."""
    log = _log()
    log["users"]["dm_c"] = {"userName": "Cy", "seatNum": 2, "cards": ["c2"]}
    return log


async def _ctx(sub=ALICE, absent=BOB, name="Alice"):
    return await sub_context(SESSION, absent, sub, name)


async def _pod():
    await _seed(draft_data=_log_with_cy(), sign_ups={ALICE: "Alice", BOB: "Bob", CY: "Cy"})
    await _stamp_draft(team_a=[ALICE, BOB], team_b=[CY])


async def _ready():
    await _pod()
    await _reported(SESSION, [(ALICE, "x", False), (BOB, CY, False)])
    await _open_chat()


async def _set_state(loan_id, state):
    async with AsyncSessionLocal() as s:
        await s.execute(update(CardLoan).where(CardLoan.id == loan_id).values(state=state))
        await s.commit()


async def test_a_clean_sub_offer_is_made_for_the_absent_players_pool(test_db, library_on):
    await _ready()
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    loan_id = await offer_to_sub(ctx)
    assert isinstance(loan_id, int)
    found = await sub_loan_for(f"draft:{SESSION}", BOB)
    assert found is not None
    assert found.borrower_id == ALICE and found.state == "assigned"
    assert found.claimed_at is not None and found.ready_dm_at is None
    assert found.cards == [{"name": "Island", "qty": 1}]


async def test_confirm_retracts_the_absent_players_uncollected_offer(test_db, library_on):
    await _ready()
    await assign_deck(GUILD, BOB, [{"name": "Island", "qty": 1}],
                      source=f"draft:{SESSION}", library_id=LIBRARY)
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    await offer_to_sub(ctx)
    rows = await _loans()           # {borrower: newest row}
    assert rows[BOB].state == "expired" and rows[ALICE].state == "assigned"


async def test_absent_player_with_deck_out_is_refused(test_db, library_on):
    await _ready()
    loan_id = await assign_deck(GUILD, BOB, [{"name": "Island", "qty": 1}],
                                source=f"draft:{SESSION}", library_id=LIBRARY)
    await _set_state(loan_id, "borrowed")
    assert await check_sub(await _ctx()) == (
        "📭 **Bob** still has their deck out. It has to come back before a sub can take it.")


async def test_sub_holding_a_deck_is_refused(test_db, library_on):
    await _ready()
    loan_id = await assign_deck(GUILD, ALICE, [{"name": "Lightning Bolt", "qty": 1}],
                                source=f"draft:{SESSION}", library_id=LIBRARY)
    await _set_state(loan_id, "borrowed")
    assert await check_sub(await _ctx()) == (
        "📭 **Alice** still has a deck out. Press **Return my deck** first, then **Sub for a player** again. Their own pool stays theirs to borrow back later.")


async def test_sub_with_own_uncollected_offer_is_refused(test_db, library_on):
    await _ready()
    await assign_deck(GUILD, ALICE, [{"name": "Lightning Bolt", "qty": 1}],
                      source=f"draft:{SESSION}", library_id=LIBRARY)
    assert await check_sub(await _ctx()) == (
        "📭 **Alice** has their own deck waiting. Press **I don't need my deck** first, then **Sub for a player** again; the pool stays theirs to ask for later.")


async def test_pool_already_with_another_sub_is_refused(test_db, library_on):
    await _ready()
    await assign_deck(GUILD, CY, [{"name": "Island", "qty": 1}],
                      source=f"draft:{SESSION}", library_id=LIBRARY, on_behalf_of=BOB)
    assert await check_sub(await _ctx()) == (
        "📭 **Cy** already has **Bob**'s deck as their sub. It has to come back first.")


async def test_sub_on_the_other_team_is_refused(test_db, library_on):
    await _ready()
    assert await check_sub(await _ctx(sub=CY, name="Cy")) == "📭 **Cy** plays for the other team."


async def test_sub_is_the_absent_player_is_refused(test_db, library_on):
    await _ready()
    assert await check_sub(await _ctx(sub=BOB, name="Bob")) == "📭 Pick somebody else to sub for **Bob**."


async def test_absent_player_with_everything_reported_is_over(test_db, library_on):
    await _pod()
    await _reported(SESSION, [(ALICE, "x", False), (BOB, CY, True)])
    await _open_chat()
    assert await check_sub(await _ctx()) == (
        "📭 Every match of **Bob**'s in this draft is reported, so there's nothing left to sub for.")


async def test_nothing_on_the_shelf_is_refused(test_db, library_on):
    await _ready()
    await assign_deck(GUILD, "2000", [{"name": "Island", "qty": 1}],
                      source="draft:other", library_id=LIBRARY)
    assert await check_sub(await _ctx()) == (
        "📭 None of **Bob**'s deck is on the shelf right now — try again once the other decks are back.")


async def test_confirm_twice_for_the_same_pool_refuses_the_second(test_db, library_on):
    await _ready()
    first = await _ctx()
    assert isinstance(await offer_to_sub(first), int)
    second = await _ctx(sub=CY, name="Cy")   # other team, but offer_to_sub is the backstop
    assert await offer_to_sub(second) == (
        "📭 **Alice** already has **Bob**'s deck as their sub. It has to come back first.")


async def test_an_outside_sub_is_named_from_the_member(test_db, library_on):
    await _ready()
    ctx = await _ctx(sub="7000", name="Sal")
    assert (ctx.sub_in_draft, ctx.sub_name, ctx.team_key) == (False, "Sal", "A")
    assert chat_line(ctx) == "**Sal** is subbing for **Bob** and has been offered **Bob**'s deck."
    assert confirm_reply(ctx) == "✅ **Bob**'s deck is offered to **Sal**. They collect it with **Borrow my deck** here or in their DM; the offer lasts 15 minutes."


async def test_a_player_whose_own_pool_is_with_a_sub_may_still_sub(test_db, library_on):
    await _ready()
    held = await assign_deck(GUILD, CY, [{"name": "Lightning Bolt", "qty": 1}],
                             source=f"draft:{SESSION}", library_id=LIBRARY,
                             on_behalf_of=ALICE)
    await _set_state(held, "borrowed")
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    loan_id = await offer_to_sub(ctx)
    assert isinstance(loan_id, int)
    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
    assert loan is not None
    assert (loan.borrower_id, loan.on_behalf_of, loan.state) == (ALICE, BOB, "assigned")


async def test_an_unexpected_failure_says_the_deck_cannot_be_put_together(
        test_db, library_on):
    await _ready()
    with patch("services.library_sub.assign_deck", side_effect=RuntimeError("boom")):
        assert await offer_to_sub(await _ctx()) == (
            "⚠️ The library can't put **Bob**'s deck together — ask an admin.")


async def test_return_texts_for_a_drafter_sub_with_a_match_left(test_db, library_on):
    await _ready()
    await offer_to_sub(await _ctx())
    loan = await active_loan(ALICE)
    pressed, landed = await sub_return_texts(loan)
    assert pressed == ("📬 Returning **Bob**'s deck. That's the end of your turn as Bob's sub: it goes back on the shelf for Bob. "
                       "Want your own pool back? Press **Borrow my deck** once the return lands.")
    assert landed == ("✅ **Bob**'s deck is back on the shelf. You're no longer subbing for Bob. "
                      "Your own pool is still yours to borrow back: press **Borrow my deck** in the draft chat.")


async def test_return_landed_for_an_outside_sub_offers_no_reborrow(test_db, library_on):
    await _ready()
    await offer_to_sub(await _ctx(sub="7000", name="Sal"))
    loan = await active_loan("7000")
    pressed, landed = await sub_return_texts(loan)
    assert pressed == "📬 Returning **Bob**'s deck. That's the end of your turn as Bob's sub: it goes back on the shelf for Bob."
    assert landed == "✅ **Bob**'s deck is back on the shelf. You're no longer subbing for Bob."


async def test_own_loan_has_no_sub_return_texts(test_db, library_on):
    await _ready()
    await assign_deck(GUILD, ALICE, [{"name": "Lightning Bolt", "qty": 1}], source=f"draft:{SESSION}", library_id=LIBRARY)
    assert await sub_return_texts(await active_loan(ALICE)) is None


async def test_an_offer_collected_after_the_checks_is_never_offered_to_the_sub(
        test_db, library_on):
    """Bob presses Borrow my deck while the Confirm is queued: his offer goes
    out_pending between check_sub and offer_to_sub, and the sub is refused."""
    await _ready()
    own = await assign_deck(GUILD, BOB, [{"name": "Island", "qty": 1}],
                            source=f"draft:{SESSION}", library_id=LIBRARY)
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    with patch("services.library_sub._absent_holds",
               side_effect=_collected_after_first_read(own)):
        assert await offer_to_sub(ctx) == (
            "📭 **Bob** still has their deck out. It has to come back before a sub can take it.")
    assert await sub_loan_for(f"draft:{SESSION}", BOB) is None
    assert (await _loans())[BOB].state == "out_pending"


def _collected_after_first_read(loan_id):
    """offer_to_sub's first read sees Bob's offer still assigned; the borrow
    lands right after it, before the retract."""
    from services import library_sub
    real = library_sub._absent_holds
    calls = []

    async def fake(ctx):
        result = await real(ctx)
        if not calls:
            await _set_state(loan_id, "out_pending")
        calls.append(result)
        return result
    return fake


async def test_the_offer_retracted_for_the_sub_is_the_absent_players_own(
        test_db, library_on):
    await _ready()
    own = await assign_deck(GUILD, BOB, [{"name": "Island", "qty": 1}],
                            source=f"draft:{SESSION}", library_id=LIBRARY)
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    with patch("services.library_sub.decline_offer", create=True) as declined:
        assert isinstance(await offer_to_sub(ctx), int)
    declined.assert_not_called()
    async with AsyncSessionLocal() as s:
        assert (await s.get(CardLoan, own)).state == "expired"


async def test_a_sub_who_took_a_deck_meanwhile_leaves_the_absent_offer_alone(
        test_db, library_on):
    await _ready()
    own = await assign_deck(GUILD, BOB, [{"name": "Island", "qty": 1}],
                            source=f"draft:{SESSION}", library_id=LIBRARY)
    ctx = await _ctx()
    assert await check_sub(ctx) is None
    busy = await assign_deck(GUILD, ALICE, [{"name": "Lightning Bolt", "qty": 1}],
                             source="draft:other", library_id=LIBRARY)
    await _set_state(busy, "borrowed")
    assert await offer_to_sub(ctx) == (
        "📭 **Alice** still has a deck out. Press **Return my deck** first, then **Sub for a player** again. Their own pool stays theirs to borrow back later.")
    async with AsyncSessionLocal() as s:
        assert (await s.get(CardLoan, own)).state == "assigned"


async def test_a_sub_with_an_offer_from_another_draft_is_told_to_decline_it(
        test_db, library_on):
    await _ready()
    await assign_deck(GUILD, ALICE, [{"name": "Lightning Bolt", "qty": 1}],
                      source="draft:other", library_id=LIBRARY)
    assert await check_sub(await _ctx()) == (
        "📭 **Alice** has a deck offer waiting from another draft. Press **I don't "
        "need my deck** on that draft's library panel first, then **Sub for a "
        "player** again.")


async def test_a_sub_the_library_does_not_lend_to_is_named_in_the_refusal(
        test_db, library_on):
    await _ready()
    with patch("services.library_sub.may_borrow_many", return_value=set()), \
            patch("services.library_sub.why_not",
                  return_value="🔒 This card library lends to players with 3+ drafts here, and you have 0."):
        assert await check_sub(await _ctx()) == (
            "📭 **Alice** can't borrow from this library: This card library lends "
            "to players with 3+ drafts here, and you have 0.")
