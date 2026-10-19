"""A time-boxed claim on a card library's copies of a cube.

Each row says "from starts_at until released, this much of the library's stock of
cube_id is spoken for". The ledger counts rows overlapping a moment to decide
whether another draft can be promised a copy.

kind is who the claim is for: 'server' (a draft played on the server's set-aside
copy), 'match' or 'sitting' (league play). unit is what the claim counts: a whole
'copy' of the cube, or a 'deck' listed in cards. Phase 1 writes only server holds
in copies.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, JSON, String, text

from database.models_base import Base


class LibraryHold(Base):
    __tablename__ = 'library_hold'

    id = Column(Integer, primary_key=True)
    library_id = Column(String(64), nullable=False, index=True)
    cube_id = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False)
    unit = Column(String(8), nullable=False)
    # [{"name": ..., "qty": ...}] for unit='deck'; unused for copies.
    cards = Column(JSON, nullable=True)

    starts_at = Column(DateTime, nullable=False, index=True)
    ends_at = Column(DateTime, nullable=False)

    draft_session_id = Column(String, nullable=True, index=True)
    tournament_match_id = Column(Integer, nullable=True)
    player_id = Column(String(64), nullable=True)

    released_at = Column(DateTime, nullable=True)
    released_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # At most one live server hold per draft, so a retried reserve cannot
        # book the same draft's copy twice. Partial: released holds accumulate.
        Index('uq_library_hold_one_live_server_per_draft', 'draft_session_id',
              unique=True,
              sqlite_where=text("released_at IS NULL AND kind = 'server'")),
    )

    def __repr__(self):
        return (f"<LibraryHold(library={self.library_id}, cube={self.cube_id}, "
                f"kind={self.kind}, draft={self.draft_session_id})>")
