"""Taking back more cards than one MTGO trade can carry.

A cube goes IN across as many trades as it takes, and until now it could not
come back out: /withdraw asked for the whole position in a single order and was
refused above the trade limit, with no way to ask for part of it. Everything
above the limit went in and nothing came back -- a one-way door reported to the
depositor as `too_large`, with no action available to them.

The whole-position withdraw is kept for a position that fits. It names no
cards, which lets the serve settle from its own movement record, and it is the
path that has actually been exercised against live MTGO. Only a position too
big to ask for at once is split.
"""
import pytest

from services.mtgo_tradebot_client import _settle_body

# --- the request the serve is sent -----------------------------------------


def test_asking_for_nothing_settles_the_whole_position():
    """The existing shape, unchanged: no cards named at all."""
    body = _settle_body("me", None, None, True, 10)

    assert "items" not in body and "cards" not in body


def test_one_named_card_with_a_quantity_is_an_item():
    body = _settle_body("me", "Swamp", 4, True, 10)

    assert body["items"] == [{"name": "Swamp", "qty": 4}]


def test_a_bare_name_settles_every_copy_of_it():
    body = _settle_body("me", "Swamp", None, True, 10)

    assert body["cards"] == ["Swamp"]


def test_a_list_of_items_goes_as_items():
    """What makes a chunked withdrawal expressible at all. The body always had
    an items[] list in it; nothing could ever put more than one thing in it."""
    body = _settle_body("me", [{"name": "Swamp", "qty": 4},
                               {"name": "Island", "qty": 2}], None, True, 10)

    assert body["items"] == [{"name": "Swamp", "qty": 4},
                             {"name": "Island", "qty": 2}]
    assert "qty" not in body, "items carry their own amounts"


def test_a_list_of_bare_names_still_works():
    body = _settle_body("me", ["Swamp", "Island"], None, True, 10)

    assert body["cards"] == ["Swamp", "Island"]


# --- asking for a position in pieces ----------------------------------------

from database.db_session import AsyncSessionLocal  # noqa: E402
from services import debt_service, wallet_service  # noqa: E402
import services.card_deposit_service as svc  # noqa: E402
from conftest import FakeLendingServe, stub_library  # noqa: E402

GUILD, OWNER, HANDLE, LIB = "g1", "u1", "Depositor01", "lib"


@pytest.fixture
def rig(monkeypatch):
    client = FakeLendingServe()
    monkeypatch.setattr(svc, "get_lending_client", lambda: client)
    # Which library this server draws on: a deposit or withdrawal has to name
    # one, because the custody it books belongs to a library and not to a room.
    stub_library(monkeypatch, svc, library_id=LIB)

    async def handle(_):
        return HANDLE
    monkeypatch.setattr(svc, "_mtgo_handle", handle)

    async def free():
        return None
    monkeypatch.setattr(svc, "library_busy_reason", free)
    return client


async def _hold(cards):
    """Put cards in the library's custody for OWNER, as a settled deposit does."""
    for c in cards:
        await debt_service.create_card_loan(
            guild_id=wallet_service.library_scope(LIB), lender_id=OWNER,
            borrower_id=wallet_service.HOUSE_LIBRARY, card_name=c["name"],
            quantity=c["qty"], created_by="test", source_id=f"d:{c['name']}")


def _stocked(monkeypatch, cards):
    async def _avail():
        return {c["name"]: c["qty"] for c in cards}
    monkeypatch.setattr(svc, "available_now", _avail)


@pytest.mark.asyncio
async def test_a_position_that_fits_still_names_its_cards(
        test_db, rig, monkeypatch):
    """A single-trade withdrawal used to name nothing and let the serve settle
    from its own record. That record is the whole MTGO account, which every
    library shares -- so an unnamed ask is an ask for everything on the shelf,
    and one depositor's withdrawal could carry away another library's cards.
    Every order names exactly what it is owed, however small."""
    cards = [{"name": "Swamp", "qty": 3}]
    await _hold(cards)
    _stocked(monkeypatch, cards)

    assert (await svc.start_withdrawal(GUILD, OWNER))[0] == "dispatched"
    handle, asked = rig.withdrawn[0]
    assert handle == HANDLE
    assert asked == cards, "the order names what it is owed"


@pytest.mark.asyncio
async def test_a_position_too_big_for_one_trade_is_planned_as_pieces(
        test_db, rig, monkeypatch):
    """The dead end this fixes: everything above the trade limit went in and
    nothing came back out.

    The PLAN is produced here; the trades are run one at a time by the caller.
    Dispatching them all at once would open several MTGO trades before the
    depositor had accepted any, which is what the deposit side takes care not
    to do.
    """
    cards = [{"name": f"Card{i}", "qty": 1} for i in range(11)]
    await _hold(cards)
    monkeypatch.setattr(svc, "max_cards_per_trade", lambda: 10)

    orders = await svc.withdrawal_orders(OWNER, LIB)

    assert len(orders) == 2, "one order per trade"
    assert all(o is not None for o in orders), "each names its own cards"
    assert sum(sum(c["qty"] for c in o) for o in orders) == 11


@pytest.mark.asyncio
async def test_a_position_that_fits_is_planned_as_one_named_order(
        test_db, rig, monkeypatch):
    """One trade, and it still says what it is for -- see above for why an
    unnamed order is not a smaller version of a named one."""
    cards = [{"name": "Swamp", "qty": 3}]
    await _hold(cards)

    assert await svc.withdrawal_orders(OWNER, LIB) == [cards]


@pytest.mark.asyncio
async def test_nothing_held_is_no_orders(test_db, rig):
    assert await svc.withdrawal_orders(OWNER, LIB) == []


@pytest.mark.asyncio
async def test_every_card_is_asked_for_exactly_once(test_db, rig, monkeypatch):
    """A card dropped between orders is a card the depositor never gets back;
    a card in two orders is asked for twice and the second fails."""
    cards = [{"name": f"Card{i}", "qty": 2} for i in range(8)]
    await _hold(cards)
    monkeypatch.setattr(svc, "max_cards_per_trade", lambda: 5)

    asked = {}
    for order in await svc.withdrawal_orders(OWNER, LIB):
        for c in order:
            asked[c["name"]] = asked.get(c["name"], 0) + c["qty"]

    assert asked == {f"Card{i}": 2 for i in range(8)}


@pytest.mark.asyncio
async def test_an_order_names_its_cards_to_the_serve(test_db, rig, monkeypatch):
    """Naming cards does not choose printings -- the serve settles from its own
    movement record, oldest first -- so a piece of a position can be asked for
    without disturbing which copies come back."""
    cards = [{"name": "Swamp", "qty": 2}, {"name": "Island", "qty": 1}]
    await _hold(cards)
    _stocked(monkeypatch, cards)

    status, _ = await svc.start_withdrawal(GUILD, OWNER, cards=cards)

    assert status == "dispatched"
    assert rig.withdrawn == [(HANDLE, cards)]


@pytest.mark.asyncio
async def test_the_reported_dead_end_is_gone(test_db, rig, monkeypatch):
    """The reproduction from the issue: deposit 11 cards at a limit of 10, and
    /withdraw answered `too_large` with no subset argument to fall back on.
    Everything above the limit went in and nothing came back out.
    """
    monkeypatch.setattr(svc, "max_cards_per_trade", lambda: 10)
    cube = [{"name": f"Card{i}", "qty": 1} for i in range(11)]

    for n, chunk in enumerate(svc.chunk_cards(cube, 10)):
        rig.job_id = f"in-{n}"
        rig.jobs[f"in-{n}"] = {"state": "done", "receive": chunk}
        assert (await svc.start_deposit(GUILD, OWNER, chunk))[0] == "dispatched"
    await svc.settle_deposits(GUILD)
    assert sum(c["qty"] for c in await svc.held_for(OWNER, LIB)) == 11

    _stocked(monkeypatch, cube)
    orders = await svc.withdrawal_orders(OWNER, LIB)
    assert orders, "there has to be a way to ask for them"

    for n, order in enumerate(orders):
        rig.job_id = f"out-{n}"
        rig.jobs[f"out-{n}"] = {"state": "done", "give": order}
        status, _ = await svc.start_withdrawal(GUILD, OWNER, cards=order)
        assert status == "dispatched", f"order {n} refused: {status}"
        await svc.settle_deposits(GUILD)

    assert await svc.held_for(OWNER, LIB) == [], "every card came back"
