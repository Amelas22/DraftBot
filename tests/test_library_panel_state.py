"""What the library panel should say and which face it should wear, without
Discord: who it lists, its embeds, and desired_face as a table."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel_state
from services.library_panel_state import (TITLE, Borrower, Face, ask_embed, borrow_embed,
                                          desired_face, return_embed)


def _text(embed):
    return "\n".join([embed.title or "", embed.description or ""]
                     + [f"{f.name}\n{f.value}" for f in embed.fields])


def _line(embed, name):
    return next(line for line in _text(embed).splitlines() if name in line)


def _field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), "")


def test_borrow_embed_says_returning_between_matches_is_fine():
    text = library_panel_state.borrow_embed(_who("borrowed")).description
    assert "**Return my deck** hands it back between matches" in text
    assert "borrow it again while you have a match to play" in text


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
    assert Face.of("ask") is Face.ASK and Face.ASK.mode == "ask"


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

N, B, R, C, A = Face.NONE, Face.BORROW, Face.RETURN, Face.CLOSED, Face.ASK

# (current, borrowers, settled, chat open, offered, anyone may request) -> desired
TABLE = [
    # No panel yet: posted only while undecided, with a chat and someone in it.
    (N, NOBODY, False, True, False, False, N),
    (N, RESERVED, False, True, False, False, B),
    (N, OUT, False, True, False, False, B),
    (N, OUT, False, False, False, False, N),
    (N, OUT, True, True, False, False, N),
    (N, BACK, True, True, False, False, N),
    # Borrow, undecided: stays, whatever the loans say.
    (B, RESERVED, False, True, False, False, B),
    (B, OUT, False, True, False, False, B),
    (B, BACK, False, True, False, False, B),
    (B, LAPSED, False, True, False, False, B),
    # Borrow, decided.
    (B, OUT, True, True, False, False, R),
    (B, OUT_AND_OFFER, True, True, False, False, R),
    (B, ON_OFFER, True, True, False, False, B),
    (B, BACK_AND_OFFER, True, True, False, False, B),
    (B, BACK, True, True, False, False, C),
    (B, LAPSED, True, True, False, False, C),
    (B, RESERVED, True, True, False, False, C),
    (B, NOBODY, True, True, False, False, C),
    # Return: open while a deck is out or on offer.
    (R, OUT, True, True, False, False, R),
    (R, OUT_AND_OFFER, True, True, False, False, R),
    (R, BACK_AND_OFFER, True, True, False, False, R),
    (R, ON_OFFER, True, True, False, False, R),
    (R, BACK, True, True, False, False, C),
    (R, LAPSED, True, True, False, False, C),
    # The chat is gone: a posted panel closes.
    (B, OUT, False, False, False, False, C),
    (B, OUT, True, False, False, False, C),
    (R, OUT, True, False, False, False, C),
    # Closed is final.
    (C, OUT, True, True, False, False, C),
    (C, ON_OFFER, False, True, False, False, C),
    (C, NOBODY, False, False, False, False, C),
    # Decided, every deck back, but somebody listed still has a match: stays
    # open with Borrow, so a dead rubber can be played.
    (B, BACK, True, True, False, True, R),
    (R, BACK, True, True, False, True, R),
    (R, LAPSED, True, True, False, True, R),
    (R, OUT, True, False, False, True, C),     # the chat is gone regardless
    # No panel yet, nobody listed: ask when the library offers the cube.
    (N, NOBODY, False, True, True, False, A),
    (N, NOBODY, False, True, False, False, N),
    (N, RESERVED, False, True, True, False, B),      # listed beats ask
    (N, NOBODY, True, True, True, True, N),          # never first posted on a decided draft
    (N, NOBODY, False, False, True, False, N),
    # Ask: becomes borrow when someone is listed, closes when nobody may request.
    (A, NOBODY, False, True, True, False, A),
    (A, NOBODY, False, True, False, False, A),       # offered only matters before posting
    (A, RESERVED, False, True, True, False, B),
    (A, OUT, False, True, True, False, B),
    (A, NOBODY, True, True, True, True, A),
    (A, NOBODY, True, True, True, False, C),
    (A, NOBODY, False, False, True, False, C),
    # Borrow with nobody listed: stays borrow, never return, never ask.
    (B, NOBODY, False, True, False, False, B),
    (B, NOBODY, True, True, False, True, B),
]


@pytest.mark.parametrize(
    "current,borrowers,settled,chat_open,offered,anyone_may_request,want", TABLE)
def test_desired_face(current, borrowers, settled, chat_open, offered,
                      anyone_may_request, want):
    assert desired_face(current, borrowers, settled=settled, chat_open=chat_open,
                        anyone_may_request=anyone_may_request,
                        offered=offered) is want


def test_return_embed_invites_a_dead_rubber_to_borrow_again():
    embed = return_embed(_who("returned"), None, anyone_may_request=True)
    assert "**Borrow my deck** takes your deck out again" in embed.description


def test_return_embed_says_nothing_about_borrowing_when_nobody_may():
    embed = return_embed(_who("returned"), None, anyone_may_request=False)
    assert "Borrow my deck" not in embed.description


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["out_pending", "dispatch_unknown"])
async def test_a_promised_loan_is_described_by_the_cards_on_offer(state):
    """Both states have a trade that may be moving the pending offer, not the
    pool; a dispatch_unknown loan of three cards must not read as forty."""
    pool = [{"name": "Swamp", "qty": 4}, {"name": "Island", "qty": 4}]
    offer = [{"name": "Swamp", "qty": 1}]
    loan = CardLoan(state=state, cards=pool, pending_cards=offer)
    assert await library_panel_state._cards_described(loan) == offer


def test_ask_embed_invites_a_change_of_mind():
    embed = ask_embed()
    assert embed.title == TITLE
    assert embed.description == ("Nobody here asked for a library deck. Changed your mind? "
                                 "Press **Use the library** to see this draft's borrowing options.")


def test_borrow_embed_with_nobody_listed_says_so():
    embed = borrow_embed([])
    assert _text(embed).splitlines()[1] == "Nobody has a library deck yet."
    assert "Press **Borrow my deck**" in _text(embed)
