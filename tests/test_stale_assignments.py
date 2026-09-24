"""A deck offered for a draft that is over stops being collectable.

An assignment is a promise to a drafter who might still collect it. Once the
draft is underway -- the first match result is in -- whoever was going to
borrow has borrowed, and anyone still holding an uncollected assignment played
without it. The offer is dead.

Leaving it alive costs three things at once: the cards stay reserved so nobody
else can borrow them, the drafter's one active-loan slot stays occupied so they
are silently passed over at the NEXT draft, and the deck stays collectable long
after the draft it belonged to.
"""
from datetime import datetime

import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.match import MatchResult
import services.card_lending_service as svc
import services.card_library_inventory as inv

pytestmark = pytest.mark.asyncio

GUILD, BORROWER, SESSION = "g1", "u1", "s1"
DECK = [{"name": "Swamp", "qty": 4}]


async def _draft(stage="pairings", results=0, submitted=0, session_id=SESSION):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id=session_id, guild_id=GUILD, cube="c",
                           session_stage=stage))
        for i in range(results):
            s.add(MatchResult(
                session_id=session_id, match_number=i + 1,
                player1_id="a", player2_id="b",
                result_submitted_at=datetime.now() if i < submitted else None))
        await s.commit()


LIBRARY = "lib"


async def _assigned(borrower=BORROWER, source=f"draft:{SESSION}"):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=borrower, cards=DECK,
                        library_id=LIBRARY, state="assigned", source=source)
        s.add(loan)
        await s.commit()
        return loan.id


async def _state(loan_id):
    async with AsyncSessionLocal() as s:
        return (await s.get(CardLoan, loan_id)).state


async def test_a_deck_expires_when_its_draft_has_a_result(test_db):
    await _draft(results=9, submitted=1)
    loan_id = await _assigned()

    assert await svc.expire_stale_assignments() == 1
    assert await _state(loan_id) == "expired"


async def test_a_draft_that_has_not_started_keeps_its_offer(test_db):
    """Decks are assigned when the log is captured, which is BEFORE anyone has
    played a match. Expiring then would retract every offer at birth."""
    await _draft(results=9, submitted=0)
    loan_id = await _assigned()

    assert await svc.expire_stale_assignments() == 0
    assert await _state(loan_id) == "assigned"


async def test_a_finished_draft_expires_its_offers_even_with_no_results(test_db):
    """A draft can complete without every result being recorded, and an
    abandoned one records none at all. Waiting for a result that is never
    coming would reserve the cards for good."""
    await _draft(stage="completed")
    loan_id = await _assigned()

    assert await svc.expire_stale_assignments() == 1
    assert await _state(loan_id) == "expired"


async def test_a_collected_deck_is_never_expired(test_db):
    """The cards really are out. Expiring this would lose the claim on them."""
    await _draft(stage="completed")
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK,
                        state="borrowed", source=f"draft:{SESSION}")
        s.add(loan)
        await s.commit()
        loan_id = loan.id

    assert await svc.expire_stale_assignments() == 0
    assert await _state(loan_id) == "borrowed"


async def test_an_expired_deck_is_no_longer_collectable(test_db):
    await _draft(stage="completed")
    await _assigned()
    await svc.expire_stale_assignments()

    assert await svc.active_loan(BORROWER) is None


async def test_an_expired_deck_stops_reserving_its_cards(test_db):
    await _draft(stage="completed")
    await _assigned()
    before = await inv._on_loan(LIBRARY)
    await svc.expire_stale_assignments()

    assert before.get("Swamp") == 4
    assert (await inv._on_loan(LIBRARY)).get("Swamp") is None


async def test_expiring_frees_the_borrower_for_the_next_draft(test_db):
    """The slot matters as much as the cards. A player left holding a dead
    assignment is silently passed over at every later draft, told nothing, and
    their pool ages out -- which is exactly what happened before this existed.
    """
    await _draft(stage="completed")
    await _assigned()
    await svc.expire_stale_assignments()

    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=BORROWER, cards=DECK,
                       state="assigned", source="draft:s2"))
        await s.commit()          # must not raise on the unique index

    assert (await svc.active_loan(BORROWER)).source == "draft:s2"


async def test_a_loan_with_no_draft_behind_it_is_left_alone(test_db):
    """A hand-made assignment has no draft to be over. Nothing here can say
    whether it is stale, so it is not this function's to retract."""
    loan_id = await _assigned(source="fixture:seed")

    assert await svc.expire_stale_assignments() == 0
    assert await _state(loan_id) == "assigned"


# --- the race with a borrow already under way -------------------------------

async def test_an_offer_collected_mid_sweep_is_not_retracted_under_the_borrower(
        test_db, monkeypatch):
    """The sweep reads its offers, then runs two more queries before it writes.

    A borrow can dispatch in that gap. Assigning `state` on the objects read
    earlier wrote "expired" straight over the loan that had just become
    out_pending -- keeping the live job_id, because only the one column was
    flushed. The cards then leave against a loan settlement has stopped
    looking at: the deposit stays held, and the borrower's one active slot is
    handed back while they are holding a deck.
    """
    import services.card_lending_service as svc

    await _draft(stage="completed", session_id="s-race")
    loan_id = await _assigned(source="draft:s-race")

    real = svc._drafts_that_are_over

    async def collect_it_first(session, sessions):
        """Stand in for a /borrow landing between the read and the write."""
        async with AsyncSessionLocal() as other:
            loan = await other.get(CardLoan, loan_id)
            loan.state, loan.job_id = "out_pending", "live-race"
            await other.commit()
        return await real(session, sessions)

    monkeypatch.setattr(svc, "_drafts_that_are_over", collect_it_first)

    assert await svc.expire_stale_assignments() == 0, "nothing was retractable"

    async with AsyncSessionLocal() as s:
        loan = await s.get(CardLoan, loan_id)
    assert loan.state == "out_pending", "the collected deck is left alone"
    assert loan.job_id == "live-race", "and its trade is still tracked"
