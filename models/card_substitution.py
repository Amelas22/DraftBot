"""What MTGO calls a card that CubeCobra calls something else.

A Universes Beyond card is printed twice: under its crossover name on paper,
and under an in-universe name on MTGO. CubeCobra lists the first, MTGO trades
the second, and the same Scryfall oracle id sits behind both. Ask the serve for
"Norman Osborn" and it will hand over "Goben, Gene-Splice Savant" -- the right
card, under the only name it has for it.

The library books custody BY NAME, so that difference is not cosmetic: a deposit
recorded under the cube's name is custody the serve cannot be asked for again.
That is `409 asked for 1x 'Norman Osborn' but only 0 held`, which is what
/withdraw answered on 2026-09-25 for four cards of a cube that had crossed
perfectly well.

Learned, not looked up. The serve reports each substitution it made on the
finished job, so the pairing is a fact about a trade that happened rather than
an inference from a card database -- no network call on the deposit path, no
table to refresh when the next crossover set lands, and nothing to be stale.
It populates exactly where it is needed, too: the only names worth translating
are the ones the library holds, and it holds only what it has been given.

`mtgo_cat_id` is MTGO's own catalogue number for the card it moved. Nothing
reads it yet. It is kept because it is the one identifier in this row that
cannot be renamed, and a mapping keyed only on names has no way back if one of
them changes.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from database.models_base import Base


class CardSubstitution(Base):
    __tablename__ = 'card_substitution'

    # What the cube list called it -- the name that was asked for.
    cube_name = Column(String(128), primary_key=True)
    # What the serve moved, and what custody is therefore booked under.
    mtgo_name = Column(String(128), nullable=False)
    mtgo_cat_id = Column(Integer, nullable=True)
    # The trade that taught us, so a wrong row can be traced to its evidence.
    learned_from_job = Column(String(64), nullable=True)
    first_seen = Column(DateTime, default=datetime.now)
