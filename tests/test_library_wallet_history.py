"""What a card-library deposit looks like in /wallet show.

wallet_history derives display text from the ledger source, so a writer whose
source shape the table has never heard of falls through to the plain
player-to-player transfer: the borrower who paid a 5 tix deposit sees "-5 Sent"
and later "+5 Received", with no counterparty (the holder is synthetic, so no
mention is rendered) and nothing saying what happened to their money.

The categories also have to keep partitioning the ledger. A new prefix that is
claimed by its own category but not subtracted from TRANSFER's residual is
counted twice; one that is claimed by nobody is lost from every filter.
"""
import pytest

from database.db_session import db_session
from services import wallet_service, wallet_history as wh
import services.card_lending_service as svc

GUILD, BORROWER = "g1", "u1"


async def _rows():
    page = await wh.get_history_page(GUILD, BORROWER, size=50)
    return page.rows


@pytest.mark.asyncio
async def test_paying_a_deposit_reads_as_a_deposit_not_a_transfer(test_db):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
    await svc.set_collateral(GUILD, BORROWER, 7, 5)

    paid = [r for r in await _rows() if r.amount == -5]
    assert paid, "the deposit leg should be there"
    origin = wh.classify(paid[0])
    assert origin.category == wh.LIBRARY, f"classified as {origin.category}"
    assert origin.ref == "7", "and it names the loan it belongs to"


@pytest.mark.asyncio
async def test_getting_it_back_reads_differently_from_paying_it(test_db):
    """Both legs share a prefix family, but a borrower reading their history
    needs to tell "I paid a deposit" from "I got it back"."""
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
    await svc.set_collateral(GUILD, BORROWER, 7, 5)
    await svc.set_collateral(GUILD, BORROWER, 7, 0)

    events = {wh.classify(r).event for r in await _rows() if abs(r.amount) == 5
              and wh.classify(r).category == wh.LIBRARY}
    assert len(events) == 2, f"paying and being repaid read the same: {events}"


@pytest.mark.asyncio
async def test_both_library_events_have_display_text(test_db):
    """An event with no entry falls back to capitalising its own name, which is
    how internal vocabulary leaks onto a page someone is reading."""
    for event in ("deposit", "returned"):
        assert (wh.LIBRARY, event) in wh._EVENT_TEXT, f"no text for {event}"


def test_the_library_is_a_selectable_category():
    assert wh.LIBRARY in wh.CATEGORIES


def test_no_prefix_is_a_prefix_of_another():
    """The table's own stated invariant, which a new pair of prefixes is the
    most likely thing to break."""
    prefixes = [p for p, *_ in wh.KNOWN_PREFIXES]
    for a in prefixes:
        for b in prefixes:
            if a is not b:
                assert not a.startswith(b), f"{a!r} starts with {b!r}"


@pytest.mark.asyncio
async def test_a_deposit_under_an_unrecognised_key_still_reads_as_a_loan(test_db):
    """The ledger is append-only and permanent, so rows outlive the key shape
    they were written with. What makes a row a library row is not the text of
    its source but WHO it moved to -- the collateral holder -- and that cannot
    drift when a key format changes underneath it.

    The key below is deliberately not one this code writes: the property being
    pinned is that the holder decides, whatever the source says.
    """
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
        await wallet_service.transfer_in(
            s, GUILD, BORROWER, svc.collateral_holder(GUILD), 5,
            "loan:16:collateral", notes="a shape this build does not write")

    paid = [r for r in await _rows() if r.source == "loan:16:collateral"]
    origin = wh.classify(paid[0])

    assert origin.category == wh.LIBRARY, "an old key must not read as 'Sent'"
    assert origin.event == "deposit"
    assert origin.ref == "16", "and the loan is still identifiable"


@pytest.mark.asyncio
async def test_the_holder_defines_the_category_for_the_query_too(test_db):
    """classify() and the SQL filter have to agree about what a library row is,
    or a row shows as a library deposit and then vanishes when you filter for
    library deposits."""
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", BORROWER, 5,
                                         "seed", notes="opening")
        await wallet_service.transfer_in(
            s, GUILD, BORROWER, svc.collateral_holder(GUILD), 5,
            "loan:16:collateral", notes="a shape this build does not write")

    page = await wh.get_history_page(GUILD, BORROWER, category=wh.LIBRARY, size=50)

    assert [r.source for r in page.rows] == ["loan:16:collateral"]
