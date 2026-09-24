"""Somebody trusted to borrow from a library that is not open to everyone.

A communal library lends to whoever is in the servers it serves -- the cards
are the members' own. A rental library stocked by sponsors starts with people
they trust, so the first losses are conversations rather than write-offs.

Listing anybody is what closes a library. A library with no rows here lends to
everyone in every server it serves, which is the communal case and needs no
setup; naming one person restricts borrowing to the named. That makes adding
the first member a consequential act, and the tool says so.

Membership follows the LIBRARY, not the server. Somebody trusted with a
sponsor's cards is trusted with them wherever that library lends, and a
server-scoped list would have meant re-inviting the same people per room --
and, worse, would have opened the library in any room nobody had listed yet.

Set from a shell tool, never from Discord: a server admin deciding who may take
cards out of somebody else's library is the thing this exists to prevent.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, String

from database.models_base import Base


class LibraryMember(Base):
    __tablename__ = 'library_member'

    library_id = Column(String(64), primary_key=True)
    player_id = Column(String(64), primary_key=True)
    added_at = Column(DateTime, default=datetime.now)
    added_by = Column(String(64), nullable=True)

    def __repr__(self):
        return f"<LibraryMember({self.player_id} in {self.library_id})>"
