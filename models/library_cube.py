"""Which cubes a library offers to draft from.

Support is two separate questions and this answers only the first. "Does this
library OFFER this cube" is a standing decision, made once by an operator or
by a communal deposit, and it is what decides whether a cube is marked in the
picker at all. "Can it cover a draft right now" is a live inventory question
answered per draft by card_library_inventory.

Kept as a list rather than derived from the shelf. A library stocked for one
cube incidentally holds most of the cards for several others, so deriving the
offer from inventory would advertise cubes nobody meant to lend -- and would
cost a CubeCobra fetch per cube to decide, on the path that builds a dropdown
inside Discord's three-second window.

It carries no price. What borrowing costs is a property of the library (see
models/library.py), so there is no per-cube number here for a cheap cube to
become a route into an expensive shelf: every cube a library offers is offered
on that library's terms.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String

from database.models_base import Base


class LibraryCube(Base):
    __tablename__ = 'library_cube'

    library_id = Column(String(64), primary_key=True)
    cube_id = Column(String(128), primary_key=True)
    added_at = Column(DateTime, default=datetime.now)
    # "communal:auto" where a deposit listed it, otherwise whoever ran the tool.
    added_by = Column(String(64), nullable=True)
