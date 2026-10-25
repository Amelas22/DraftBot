"""What the library panel should say and which face it should wear, without
Discord: who it lists, its embeds, and desired_face as a table."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel_state
from services.library_panel_state import (Borrower, Face, borrow_embed, desired_face,
                                          return_embed)


def _text(embed):
    return "\n".join([embed.title or "", embed.description or ""]
                     + [f"{f.name}\n{f.value}" for f in embed.fields])


def _line(embed, name):
    return next(line for line in _text(embed).splitlines() if name in line)


def _field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), "")


def test_borrow_embed_lists_each_borrower_by_state():
    embed = borrow_embed([
        Borrower("1", "Ravenwood", "borrowed", []),
        Borrower("2", "kestrel_mtg", "assigned", []),
        Borrower("3", "Tomasz88", "out_pending", []),
        Borrower("4", "Ali", None, None),
    ])
    assert embed.title == "🏛️ Library decks"
    assert "collected" in _line(embed, "Ravenwood")
    assert "ready, not collected" in _line(embed, "kestrel_mtg")
    assert "trade on its way" in _line(embed, "Tomasz88")
    assert "being prepared" in _line(embed, "Ali")


def test_borrow_embed_closed_marks_uncollected_back_on_shelf():
    embed = borrow_embed([Borrower("1", "Ravenwood", "expired", [])], closed=True)
    assert "not collected, back on the shelf" in _line(embed, "Ravenwood")


def test_return_embed_names_who_still_holds_a_deck():
    cards = [{"name": "Swamp", "qty": 45}]
    embed = return_embed([
        Borrower("1", "Ravenwood", "borrowed", cards),
        Borrower("2", "kestrel_mtg", "returned", cards),
        Borrower("3", "Tomasz88", "expired", cards),
    ], "Red won 5–3")
    assert embed.title == "🏛️ Library decks"
    assert "Red won 5–3" in (embed.description or "")
    assert "Ravenwood" in _field(embed, "Still out")
    assert "45" in _field(embed, "Still out")
    assert "kestrel_mtg" in _field(embed, "Back")
    assert "Tomasz88" not in _text(embed)
    assert embed.footer.text == "Move the cards into your trade binder first"


@pytest.mark.asyncio
async def test_borrowers_are_the_reserved_plus_loaned_newest_loan_by_name(
        test_db, monkeypatch):  # noqa: F811
    async def users(draft, library_id=None):
        return {"3"}
    monkeypatch.setattr(library_panel_state, "library_users", users)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="g",
                           sign_ups={"1": "Ravenwood", "3": "Tomasz88"}))
        for state in ("returned", "borrowed"):
            s.add(CardLoan(guild_id="g", library_id="lib", borrower_id="1",
                           cards=[], state=state, source="draft:s1"))
        await s.commit()
    got = await library_panel_state.panel_borrowers("s1")
    assert [(b.name, b.state) for b in got] == [("Ravenwood", "borrowed"),
                                                ("Tomasz88", None)]
    assert await library_panel_state.panel_borrowers("nope") == []


def test_faces_round_trip_the_stored_mode():
    for face in Face:
        assert Face.of(face.mode) is face
    assert Face.NONE.mode is None and Face.CLOSED.mode == "closed"


def _who(*states):
    return [Borrower(str(i), f"p{i}", s, []) for i, s in enumerate(states)]


NOBODY: list = []
RESERVED = _who(None)            # reserved, no loan yet
ON_OFFER = _who("assigned")      # offered, not collected
OUT = _who("borrowed")           # a deck is out
OUT_AND_OFFER = _who("borrowed", "assigned")
BACK = _who("returned")          # every deck back
BACK_AND_OFFER = _who("returned", "assigned")
LAPSED = _who("expired")

N, B, R, C = Face.NONE, Face.BORROW, Face.RETURN, Face.CLOSED

# (current, borrowers, settled, chat open) -> desired
TABLE = [
    # No panel yet: posted only while undecided, with a chat and someone in it.
    (N, NOBODY, False, True, N),
    (N, RESERVED, False, True, B),
    (N, OUT, False, True, B),
    (N, OUT, False, False, N),
    (N, OUT, True, True, N),
    (N, BACK, True, True, N),
    # Borrow, undecided: stays, whatever the loans say.
    (B, RESERVED, False, True, B),
    (B, OUT, False, True, B),
    (B, BACK, False, True, B),
    (B, LAPSED, False, True, B),
    # Borrow, decided.
    (B, OUT, True, True, R),
    (B, OUT_AND_OFFER, True, True, R),
    (B, ON_OFFER, True, True, B),
    (B, BACK_AND_OFFER, True, True, B),
    (B, BACK, True, True, C),
    (B, LAPSED, True, True, C),
    (B, RESERVED, True, True, C),
    (B, NOBODY, True, True, C),
    # Return: open while a deck is out or on offer.
    (R, OUT, True, True, R),
    (R, OUT_AND_OFFER, True, True, R),
    (R, BACK_AND_OFFER, True, True, R),
    (R, ON_OFFER, True, True, R),
    (R, BACK, True, True, C),
    (R, LAPSED, True, True, C),
    # The chat is gone: a posted panel closes.
    (B, OUT, False, False, C),
    (B, OUT, True, False, C),
    (R, OUT, True, False, C),
    # Closed is final.
    (C, OUT, True, True, C),
    (C, ON_OFFER, False, True, C),
    (C, NOBODY, False, False, C),
]


@pytest.mark.parametrize("current,borrowers,settled,chat_open,want", TABLE)
def test_desired_face(current, borrowers, settled, chat_open, want):
    assert desired_face(current, borrowers, settled=settled,
                        chat_open=chat_open) is want
