"""One borrower's deck, out on loan from the card library.

The library is a second MTGO account (Team01) holding cards players deposit.
DraftBot is the CLAIM ledger -- who has what out and owes it back -- and the
TradeBot serve is physical reality, exactly the division already settled for the
tix wallet. That division decides what this row can say: the serve identifies
cards by NAME and never reports a catalogue id, and MTGO has no per-copy
identity at all, so a loan records names and quantities. Which printing a
borrower receives is the serve's business, and whose copy it "was" is an
allocation policy this side chooses once deposits exist -- never an observation.

The state machine is the reserve/job correspondence the wallet already uses:

    assigned --lend--> out_pending --job done--> borrowed
                            |                        |
                       job failed                 collect
                            v                        v
                        assigned          return_pending --job done--> returned
                                                     |
                                                job failed
                                                     v
                                                 borrowed

Two rules carry it. The claim moves ONLY when a job reaches a terminal state, so
there is no moment where the row says a player holds cards that never left the
library. And job_id is persisted, so a bot that dies mid-trade can resolve the
job on the way back up instead of stranding the loan forever.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, JSON, String, text

from database.models_base import Base

# Everything that is not finished. A borrower may hold exactly one of these at a
# time -- see the partial unique index below.
# 'dispatch_unknown': a trade we cannot say happened or not -- the request
# reached the serve and only the answer was lost, with no job to adopt. The
# deposit stays held and a human unpicks it. It is ACTIVE on purpose: the
# borrower's slot stays occupied, so they cannot take a second deck while a
# first may be on its way to them, and a retry cannot pass the dispatcher's
# "assigned" check and open a second trade against a live one.
#
# The unique index below needs no change for it: that predicate names the two
# FINISHED states, so anything new is active by default -- which is the safe
# direction for a state that means "we do not know".
ACTIVE_STATES = ("assigned", "out_pending", "borrowed", "return_pending",
                 "dispatch_unknown")



class CardLoan(Base):
    __tablename__ = 'card_loans'

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(64), nullable=False)
    # Which library's cards these are. The guild above still records where the
    # deck was DRAFTED -- useful context, and what the draft's channels belong
    # to -- but every question about stock, price and access is asked of the
    # library, because that is the pool the cards came out of.
    library_id = Column(String(64), nullable=True)
    borrower_id = Column(String(64), nullable=False)

    # [{"name": "Swamp", "qty": 7}, ...] -- the deck file. Per-card quantities,
    # which is why the serve's /trade endpoint is the one we talk to: /request
    # applies a single scalar qty to every card and cannot express a deck.
    cards = Column(JSON, nullable=False)

    # What a borrow is actually offering, when that differs from the deck --
    # a borrower who took a partial deck because the library was short. The deck
    # itself is NOT rewritten until the handover completes: a trade that fails
    # moved nothing, so it must cost nothing, and trimming on dispatch let every
    # failed attempt shrink the deck again.
    pending_cards = Column(JSON, nullable=True)

    state = Column(String(16), nullable=False, default='assigned')
    # The serve job while a trade is in flight; NULL between trades.
    job_id = Column(String(64), nullable=True)
    # Where the deck came from: 'fixture:<who>' now, 'draft:<session_id>' once
    # decks are read from the draft log.
    source = Column(String(128), nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    borrowed_at = Column(DateTime, nullable=True)
    returned_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_card_loans_borrower', 'guild_id', 'borrower_id'),
        # One active loan per borrower, ANYWHERE, enforced here rather than
        # only in the service.
        #
        # The predicate names the two FINISHED states and must stay in step
        # with ACTIVE_STATES above -- it is raw SQL and cannot reference the
        # tuple, so nothing checks that they agree. 'returned' came back;
        # 'expired' never went out, being a deck offered for a draft that is
        # now over and which nobody collected. A finished state left out of
        # here keeps its borrower's only slot occupied by a loan they cannot
        # collect, and they are then silently passed over at every later
        # draft. Not scoped by guild: the library is a single
        # MTGO account, so a borrower holding a deck is holding its only copies
        # of those cards and being in another server does not entitle them to a
        # second. Partial, because finished loans must accumulate: without the
        # predicate a player could borrow exactly once, ever.
        Index('uq_card_loans_one_active_per_borrower', 'borrower_id',
              unique=True, sqlite_where=text("state NOT IN ('returned', 'expired')")),
        Index('ix_card_loans_job', 'job_id',
              sqlite_where=text('job_id IS NOT NULL')),
    )

    @property
    def offered_cards(self):
        """What a trade on this loan is actually moving.

        The pending offer in preference to the deck: a borrower who took a
        partial deck is sent that subset, and `_dispatch` writes it here with
        the job that carries it. Named so that reading a loan's real contents
        off the row is one expression rather than an idiom repeated per site.

        The dispatch does NOT read this -- it is sending the offer it was
        handed, and writes the result here -- and the cog reads the same two
        fields defensively, because the loans it renders may be stubs.
        """
        return self.pending_cards or self.cards or []

    def __repr__(self):
        return (f"<CardLoan(borrower={self.borrower_id}, state={self.state}, "
                f"cards={len(self.cards or [])})>")
