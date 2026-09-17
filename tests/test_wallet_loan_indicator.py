"""The wallet panel says when the card library is holding something of yours.

A borrower whose deposit is out sees the tix leave their balance, and until now
nothing on the panel said why it was gone or when it was coming back. The cards
themselves are not the wallet's business -- `/mydeck` lists them and a deck is
far too long for an embed field -- so this is one line: that a loan is
outstanding, and the reference to find it by.

The category filter is pinned here too. Its options are built from
CATEGORY_LABELS while the ledger query is built from CATEGORIES, so a category
added to one and not the other is a filter the ledger can express and the UI
cannot offer -- silently, with no error anywhere.
"""
import pytest

from database.db_session import AsyncSessionLocal, db_session
from models.card_loan import CardLoan
from services import wallet_history, wallet_service
from wallet_history_view import CATEGORY_LABELS, wallet_embed

GUILD, PLAYER = "g1", "p1"


@pytest.fixture
def library_on(monkeypatch):
    """A guild that runs a library. Absent config means the feature is off, so
    without this the panel correctly skips the loan lookup entirely."""
    import wallet_history_view as view
    monkeypatch.setattr(view, "library_enabled", lambda gid: True)


def test_every_ledger_category_can_be_chosen_in_the_panel():
    missing = [c for c in wallet_history.CATEGORIES if c not in CATEGORY_LABELS]
    assert not missing, f"no filter option for {missing}"


def test_no_label_names_a_category_the_ledger_does_not_have():
    stray = [c for c in CATEGORY_LABELS if c is not None
             and c not in wallet_history.CATEGORIES]
    assert not stray, f"labels for categories the query cannot select: {stray}"


async def _seed_loan(state, source="fixture:someone"):
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id=GUILD, borrower_id=PLAYER, state=state,
                        cards=[{"name": "Swamp", "qty": 8}], source=source)
        s.add(loan)
        await s.commit()
        return loan.id


def _fields(embed):
    return {f.name: str(f.value) for f in embed.fields}


@pytest.mark.asyncio
async def test_a_borrowed_deck_is_flagged_with_its_reference(test_db, library_on):
    await _seed_loan("borrowed", source="draft:abc123")

    embed, _ = await wallet_embed(GUILD, PLAYER, "Someone")

    fields = _fields(embed)
    assert any("library" in name.lower() for name in fields), \
        f"no card-library line on the panel: {list(fields)}"
    said = " ".join(fields.values())
    assert "draft:abc123" in said, "the loan's source is the reference to quote"


@pytest.mark.asyncio
async def test_the_panel_does_not_list_the_cards(test_db, library_on):
    """A deck is 40+ lines. The panel says a loan exists; /mydeck says what."""
    await _seed_loan("borrowed")

    embed, _ = await wallet_embed(GUILD, PLAYER, "Someone")

    said = " ".join(_fields(embed).values())
    assert "Swamp" not in said, "the deck belongs in /mydeck, not the wallet"


@pytest.mark.asyncio
async def test_a_deck_merely_waiting_is_not_called_outstanding(test_db, library_on):
    """An assigned deck has taken nothing: no cards have moved and no deposit
    has been charged, so there is nothing for the wallet to explain."""
    await _seed_loan("assigned")

    embed, _ = await wallet_embed(GUILD, PLAYER, "Someone")

    assert not any("library" in name.lower() for name in _fields(embed))


@pytest.mark.asyncio
async def test_a_wallet_with_no_loan_is_unchanged(test_db):
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", PLAYER, 3,
                                         "seed", notes="opening")

    embed, _ = await wallet_embed(GUILD, PLAYER, "Someone")

    assert not any("library" in name.lower() for name in _fields(embed))


@pytest.mark.asyncio
async def test_a_broken_library_does_not_take_the_wallet_down(test_db, monkeypatch):
    """The loan line is an adornment on someone's money. If reading it fails,
    they still get their balance -- the alternative is a wallet that cannot be
    opened because an unrelated feature is unwell."""
    import wallet_history_view as view

    async def broken(*a, **k):
        raise RuntimeError("card_loans is unavailable")
    monkeypatch.setattr(view, "_outstanding_loan", broken)

    embed, _ = await wallet_embed(GUILD, PLAYER, "Someone")

    assert "Balance" in _fields(embed)


@pytest.mark.asyncio
async def test_a_guild_without_a_library_is_not_queried_at_all(test_db, monkeypatch):
    """This panel is rebuilt on every page turn and every filter change, not
    just when it is opened. A guild that runs no library should not pay a
    card_loans lookup each time to be told so."""
    import wallet_history_view as view
    monkeypatch.setattr(view, "library_enabled", lambda gid: False)
    looked = []

    async def watched(*a, **k):
        looked.append(a)
        return None
    monkeypatch.setattr("services.card_lending_service.active_loan", watched)

    await wallet_embed(GUILD, PLAYER, "Someone")

    assert looked == [], "the lending service should not be consulted"
