"""Borrowing only a few named cards from a drafted pool.

Driven through the real loan flows against a real database, with only the serve
stood in -- the same seam as the workflow arcs. The things that matter here are
STATE, not wording: matching must write nothing (a cancelled confirm leaves the
drafter as if they had never asked), the trade must carry only the named cards,
and a small borrow that fails on the no-deck path must take back the loan it
created, while a deck that was already waiting stays waiting.
"""
from typing import Any, Optional

import pytest
import pytest_asyncio

import services.card_lending_service as lending_svc
import services.library_loan_actions as actions
import services.wallet_service as wallet_service
from conftest import FakeLendingServe, a_library
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from models.mtgo_account import MtgoAccount
from services.card_name_match import NameMatch
from services.draft_deck_assignment import assign_on_request, loan_source

pytestmark = pytest.mark.asyncio

GUILD, CUBE, LIB, SESSION = "g-some", "somecube", "lib-some", "sess-some"
ALICE, BOB = "2001", "2002"

POOL = ["Lightning Bolt", "Counterspell", "Island", "Snapcaster Mage",
        "Brainstorm"]
STOCK = {name: 4 for name in POOL}


class Caller:
    """A LibraryCaller that records what the player was told."""

    def __init__(self, user_id: str = ALICE, guild_id: Optional[str] = GUILD):
        self._user_id, self._guild_id = user_id, guild_id
        self.said: "list[str]" = []

    @property
    def user_id(self) -> Any:
        return self._user_id

    @property
    def guild_id(self) -> Any:
        return self._guild_id

    async def defer(self) -> None:
        return None

    async def send(self, content: str, *, view: Any = None) -> None:
        self.said.append(content)

    @property
    def text(self) -> str:
        return "\n".join(self.said)


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    """A stocked library offering the cube, a linked drafter, a finished draft."""
    serve = FakeLendingServe(stock=STOCK, job={"state": "done"})
    for mod in (actions, lending_svc):
        monkeypatch.setattr(mod, "get_lending_client", lambda: serve)
    pending: list = []
    monkeypatch.setattr(actions, "spawn_followup",
                        lambda label, coro: pending.append(coro))
    await a_library(LIB, guild=GUILD, collateral=0, cubes=(CUBE,), stock=STOCK)
    await MtgoAccount.link(ALICE, "AliceMTGO")
    await MtgoAccount.link(BOB, "BobMTGO")

    carddata = {f"c{i}": {"name": name} for i, name in enumerate(POOL)}
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=SESSION, guild_id=GUILD, cube=CUBE,
            draft_data={"carddata": carddata, "users": {
                "dm_a": {"userName": "Alice", "seatNum": 0, "cards": list(carddata)},
                "dm_b": {"userName": "Bob", "seatNum": 1, "cards": []}}},
            sign_ups={ALICE: "Alice", BOB: "Bob"}))
        await s.commit()

    async def drain():
        while pending:
            await pending.pop(0)

    serve.drain = drain
    return serve


async def _alices_loans():
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        return list((await s.scalars(select(CardLoan).where(
            CardLoan.borrower_id == ALICE))).all())


def _names(cards):
    return sorted(c["name"] for c in cards)


TWO = [{"name": "Lightning Bolt", "qty": 1}, {"name": "Snapcaster Mage", "qty": 1}]


# --- matching writes nothing --------------------------------------------------

async def test_match_for_borrow_with_a_waiting_deck_matches_its_cards(library):
    assert await assign_on_request(SESSION, ALICE) == "assigned"

    plan = await actions.match_for_borrow(GUILD, ALICE, SESSION,
                                          ["bolt", "snapcaster", "Black Lotus"])

    assert isinstance(plan, actions.SomeCardsPlan), plan
    assert _names(plan.available) == ["Lightning Bolt", "Snapcaster Mage"]
    assert plan.match.unknown == ["Black Lotus"]
    assert plan.reserved == []


async def test_match_for_borrow_with_no_deck_creates_no_loan(library):
    plan = await actions.match_for_borrow(GUILD, ALICE, SESSION, ["bolt"])

    assert isinstance(plan, actions.SomeCardsPlan), plan
    assert _names(plan.available) == ["Lightning Bolt"]
    assert await lending_svc.active_loan(ALICE) is None, "cancel must leave nothing"
    assert await _alices_loans() == []


async def test_match_for_borrow_refuses_while_another_drafts_deck_is_out(library):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=ALICE, state="borrowed",
                       library_id=LIB, cards=[{"name": "Island", "qty": 1}],
                       source="draft:other"))
        await s.commit()

    said = await actions.match_for_borrow(GUILD, ALICE, SESSION, ["bolt"])

    assert isinstance(said, str) and "/library return" in said, said


async def test_match_for_borrow_with_no_log_says_the_picks_are_not_in(library):
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        row = await s.scalar(select(DraftSession).where(
            DraftSession.session_id == SESSION))
        row.draft_data = None
        await s.commit()

    said = await actions.match_for_borrow(GUILD, ALICE, SESSION, ["bolt"])

    assert said == actions.REQUEST_MESSAGES["no_log"]


async def test_a_card_reserved_elsewhere_is_listed_not_offered(library):
    # Every Counterspell the library holds is on Bob's deck from another draft.
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id=GUILD, borrower_id=BOB, state="assigned",
                       library_id=LIB, cards=[{"name": "Counterspell", "qty": 4}],
                       source="draft:other"))
        await s.commit()

    plan = await actions.match_for_borrow(GUILD, ALICE, SESSION,
                                          ["counterspell", "bolt"])

    assert isinstance(plan, actions.SomeCardsPlan), plan
    assert _names(plan.available) == ["Lightning Bolt"], "never offered"
    assert plan.reserved == ["Counterspell"], "but named, so they know why"


# --- borrowing sends only what was named --------------------------------------

async def test_borrow_some_with_a_waiting_deck_sends_only_the_named_cards(
        library, monkeypatch):
    assert await assign_on_request(SESSION, ALICE) == "assigned"
    # The trade is left open -- nobody has accepted it yet -- so what the
    # dispatch staged is still on the loan to be read.
    async def still_open(*_a, **_k):
        return ("running", None)

    monkeypatch.setattr(actions, "poll_until_settled", still_open)

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    assert len(library.sent) == 1 and _names(library.sent[0]) == _names(TWO), \
        library.sent
    loan = await lending_svc.active_loan(ALICE)
    assert loan is not None and loan.state == "out_pending"
    assert _names(loan.pending_cards) == _names(TWO)
    assert len(loan.cards) == len(POOL), "the drafted pool stays the draft record"


async def test_borrow_some_with_no_deck_asks_then_sends_only_the_named_cards(library):
    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    assert len(library.sent) == 1 and _names(library.sent[0]) == _names(TWO)
    loan = await lending_svc.active_loan(ALICE)
    assert loan is not None and loan.state == "borrowed", caller.text
    assert loan.source == loan_source(SESSION)


async def test_a_failed_small_borrow_with_no_deck_retracts_the_loan(library):
    library.response = None     # the serve refuses the trade outright

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    assert await lending_svc.active_loan(ALICE) is None, caller.text
    [loan] = await _alices_loans()
    assert loan.state == "expired", "left as if they had never asked"


async def test_a_small_borrow_that_fails_in_the_trade_with_no_deck_retracts_too(
        library):
    """Dispatched, then the trade failed during the poll: settlement put the
    loan back to `assigned`, and nobody asked for the whole pool."""
    library.job = {"state": "failed", "detail": "declined"}

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    [loan] = await _alices_loans()
    assert loan.state == "expired", (loan.state, caller.text)
    assert "Nothing is being held for you" in caller.text, caller.text
    assert "**Borrow only some cards** again" in caller.text
    assert "still reserved" not in caller.text, "it was taken back"
    assert "/library borrow" not in caller.text, "that would ask for the whole pool"


async def test_a_small_borrow_that_fails_in_the_trade_keeps_a_waiting_deck(library):
    assert await assign_on_request(SESSION, ALICE) == "assigned"
    library.job = {"state": "failed", "detail": "declined"}

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    loan = await lending_svc.active_loan(ALICE)
    assert loan is not None and loan.state == "assigned", caller.text
    assert "still reserved" in caller.text, caller.text
    assert "**Borrow only some cards** again" in caller.text
    assert "**Borrow my deck**" in caller.text


async def test_a_raise_during_the_trade_does_not_retract_the_loan(library, monkeypatch):
    """A raise can come after the serve accepted the job but before the loan
    shows it; expiring the loan then would strand a live trade. Raised from
    the dispatch here, where the loan still reads `assigned` -- the one case
    the retraction would actually bite."""
    async def boom(*_a, **_k):
        raise RuntimeError("lost the connection after the serve accepted")

    monkeypatch.setattr(actions, "borrow_when_free", boom)

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    with pytest.raises(RuntimeError):
        await library.drain()

    [loan] = await _alices_loans()
    assert loan.state == "assigned", "left for the watchdog, not retracted"


async def test_a_non_drafter_is_told_they_have_no_pool(library):
    said = await actions.match_for_borrow(GUILD, "3003", SESSION, ["bolt"])
    assert said == actions.REQUEST_MESSAGES["not_drafter"]

    caller = Caller(user_id="3003")
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    assert caller.said == [actions.REQUEST_MESSAGES["not_drafter"]]
    assert library.sent == []


async def test_a_failed_small_borrow_with_a_waiting_deck_keeps_the_loan(library):
    assert await assign_on_request(SESSION, ALICE) == "assigned"
    library.response = None

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    loan = await lending_svc.active_loan(ALICE)
    assert loan is not None and loan.state == "assigned", "they had it before asking"


async def test_short_funds_on_a_small_borrow_quotes_the_small_hold(
        library, monkeypatch):
    """The offering has to reach hold_shortfall: it reads the loan before
    pending_cards is written, so without it the quote is the deck's hold."""
    from models.library import Library
    async with AsyncSessionLocal() as s:
        lib = await s.get(Library, LIB)
        lib.collateral_tix, lib.small_loan_tix_untrusted = 500, 50
        await s.commit()
    monkeypatch.setattr(lending_svc, "is_money_server", lambda gid: True)
    seen: list = []
    real = actions.hold_shortfall

    async def recording(guild_id, borrower_id, offering=None):
        seen.append(offering)
        return await real(guild_id, borrower_id, offering=offering)

    monkeypatch.setattr(actions, "hold_shortfall", recording)

    caller = Caller()
    await actions.borrow_some(caller, SESSION, TWO)
    await library.drain()

    assert seen == [TWO], seen
    assert "**50 tix**" in caller.text, caller.text
    assert library.sent == [], "nothing went out"
    [loan] = await _alices_loans()
    assert loan.state == "expired", "and the loan it asked for is taken back"


# --- the confirm text ---------------------------------------------------------

async def test_describe_plan_names_found_unknown_ambiguous_reserved_and_the_hold():
    plan = actions.SomeCardsPlan(
        available=[{"name": "Lightning Bolt", "qty": 1}],
        reserved=["Counterspell"],
        match=NameMatch(
            found=[{"name": "Lightning Bolt", "qty": 1},
                   {"name": "Counterspell", "qty": 1}],
            unknown=["Black Lotus"],
            ambiguous=[("mage", ["Snapcaster Mage", "Voidmage Prodigy"])]))

    text = actions.describe_plan(plan, 50)

    assert text.startswith("You'll borrow:")
    assert "Lightning Bolt" in text.split("\n")[0]
    assert "Not in your pool:" in text and "Black Lotus" in text
    assert "mage" in text and "Snapcaster Mage" in text and "Voidmage Prodigy" in text
    assert "Not available from the library right now: Counterspell" in text
    assert "another draft" not in text and "back" not in text.split("\n")[1]
    assert "**50 tix**" in text
    assert len(text) < 2000


async def test_describe_plan_says_no_hold_when_it_is_free():
    plan = actions.SomeCardsPlan(
        available=[{"name": "Island", "qty": 1}], reserved=[],
        match=NameMatch(found=[{"name": "Island", "qty": 1}]))

    assert "no hold" in actions.describe_plan(plan, 0).lower()


async def test_describe_plan_stays_one_message_however_ambiguous_the_names():
    many = [f"Card Of The Very Long Name Number {i}" for i in range(60)]
    plan = actions.SomeCardsPlan(
        available=[], reserved=[],
        match=NameMatch(ambiguous=[("x" * 100, many)] * 3, unknown=["y" * 100] * 3))

    assert len(actions.describe_plan(plan, 50)) < 2000


# --- a collected loan is described by what crossed ----------------------------

THREE = TWO + [{"name": "Brainstorm", "qty": 1}]


async def _collected(library, offering):
    """Alice's loan after the serve moved `offering` to her, settled for real."""
    from sqlalchemy import select
    from services.library_panel_state import borrowers_of
    caller = Caller()
    await actions.borrow_some(caller, SESSION, offering)
    await library.drain()
    loan = await lending_svc.active_loan(ALICE)
    assert loan is not None and loan.state == "borrowed", caller.text
    assert len(loan.cards) == len(POOL), "the draft record keeps the whole pool"
    async with AsyncSessionLocal() as s:
        draft = (await s.scalars(select(DraftSession))).first()
    mine = next(b for b in await borrowers_of(draft) if b.user_id == ALICE)
    return loan, mine


async def test_panel_shows_a_collected_small_loan_as_its_cards(library):
    from services.library_reminders import deck_summary
    loan, mine = await _collected(library, THREE)

    assert deck_summary(mine.cards) == deck_summary(THREE)
    assert deck_summary(mine.cards) != deck_summary(loan.cards)


async def test_return_reminder_counts_what_was_borrowed(library):
    from services.library_reminders import deck_summary, return_message
    loan, _ = await _collected(library, THREE)
    owed = await lending_svc.owed_cards(
        wallet_service.library_scope(loan.library_id), ALICE)

    msg = return_message(loan, cards=owed)

    assert deck_summary(THREE) in msg and deck_summary(loan.cards) not in msg, msg


async def test_take_what_is_there_loan_reports_its_partial_size(library):
    """The vault holds only some of the deck: the offer is what
    trim_to_available says it can cover, which is what the view sends."""
    from sqlalchemy import select
    from services.library_panel_state import borrowers_of
    from services.library_reminders import deck_summary
    assert await assign_on_request(SESSION, ALICE) == "assigned"
    loan = await lending_svc.active_loan(ALICE)
    # Every card is on the shelf except the last two names.
    library.stock = {n: (0 if n in ("Island", "Brainstorm") else 4) for n in POOL}
    partial = await lending_svc.trim_to_available(GUILD, loan.id)
    assert 0 < len(partial) < len(POOL), partial

    await lending_svc.borrow_when_free(GUILD, ALICE, offering=partial)
    await lending_svc.settle_in_flight(GUILD)
    async with AsyncSessionLocal() as s:
        draft = (await s.scalars(select(DraftSession))).first()
    mine = next(b for b in await borrowers_of(draft) if b.user_id == ALICE)

    assert mine.state == "borrowed"
    assert deck_summary(mine.cards) == deck_summary(partial)
    assert deck_summary(mine.cards) != deck_summary(loan.cards)


async def test_the_reminder_sweep_counts_what_was_borrowed(library, monkeypatch):
    """The sweep, not just the message: it must hand return_message the ledger's
    view of a loan that settled three of the pool's five cards."""
    import services.library_reminders as reminders
    from datetime import datetime
    loan, _ = await _collected(library, THREE)
    sent: list = []

    async def record(_loan, message, _what):
        sent.append(message)
        return True

    async def decided(_session_id):
        return reminders.DraftState(done_at={}, settled=True, ended_at=None,
                                    played_at={})

    monkeypatch.setattr(reminders, "_dm", record)
    monkeypatch.setattr(reminders, "draft_state", decided)

    assert await reminders.send_due_reminders(now=datetime(2030, 1, 1)) == 1
    [message] = sent
    assert reminders.deck_summary(THREE) in message, message
    assert reminders.deck_summary(loan.cards) not in message, message


async def test_a_loan_with_no_library_does_not_break_the_panel(library):
    from sqlalchemy import select
    from services.library_panel_state import borrowers_of
    loan, _ = await _collected(library, THREE)
    async with AsyncSessionLocal() as s:
        row = await s.get(CardLoan, loan.id)
        row.library_id = None
        draft = (await s.scalars(select(DraftSession))).first()
        await s.commit()

    mine = next(b for b in await borrowers_of(draft) if b.user_id == ALICE)

    assert mine.cards == loan.cards
