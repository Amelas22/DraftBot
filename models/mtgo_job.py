"""
Durable record of an in-flight MTGO serve job (tix deposit/withdraw, or a card loan).

Why a table: the cogs poll a job in a fire-and-forget background task. If the
poll times out, or the bot restarts mid-trade, that task is gone — but the MTGO
trade can still complete, moving real tix into or out of the vault with nothing
left to book the ledger side. Persisting every started job lets a startup
resumer (mtgo_resolution_service.resume_pending_jobs) pick up whatever is still
'pending' and poll it to a terminal state, so a completed trade is always booked
eventually. Booking itself stays idempotent by job_id, so a resumed poll racing
a still-alive original poller cannot double-credit.
"""
from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime

from database.models_base import Base


class MtgoJob(Base):
    __tablename__ = 'mtgo_jobs'

    job_id = Column(String(64), primary_key=True)          # the serve's job id
    kind = Column(String(16), nullable=False)              # deposit | withdraw | borrow | return
    guild_id = Column(String(64), nullable=False)
    # Which library a CARD job moves cards for. NULL for tix jobs, and for card
    # jobs written before there was more than one library. Recorded when the job
    # is dispatched rather than resolved from the guild at settle time: several
    # libraries share the MTGO account, so the guild is the only clue as to
    # whose shelf a deposit belongs on, and rebinding a server between dispatch
    # and settlement would file the cards under the wrong library for good.
    library_id = Column(String(64), nullable=True)
    # Which ORDER this trade is part of, as "<kind>:<id>" -- "loan:37".
    #
    # MTGO caps what one trade can carry, so an order of any size becomes
    # several trades run back to back, and each is one of these rows. Something
    # then has to say which rows belong together. Tix and cube deposits get
    # away with never answering that: every chunk settles on its own, so a
    # partial order is simply a partial order. A LOAN cannot -- its state only
    # moves once no batch of it is still open -- and it used to answer by
    # looking up every pending row for the borrower and assuming they were all
    # one order. A row orphaned by a crashed dispatch is then adopted into the
    # next order and its kind applied to the whole set, which settles a return
    # as a borrow and leaves a loan that can never be closed.
    #
    # Recorded rather than inferred. Both CARD writers stamp it -- lending and
    # deposits -- so those paths have one way to ask the question instead of
    # one per caller. The tix paths leave it NULL: they chunk too, but no tix
    # order has a state that waits on all of its chunks, so there is nothing
    # for them to reassemble. NULL therefore means "no order recorded", which
    # `_batches_for` reads as a row to examine rather than one to skip.
    order_id = Column(String(64), nullable=True, index=True)
    player_id = Column(String(64), nullable=False)
    mtgo_user = Column(String(128), nullable=False)
    amount = Column(Integer, nullable=False)                # tix, or COPIES when card_name is set
    # Multi-entity, exactly as DebtLedger.card_name does it: NULL = the job is tix (every
    # row written before house card lending existed), set = the job moves copies of this
    # card and `amount` is the quantity. A card job needs no printing — the serve records
    # which printings crossed and pins them itself on the way back.
    card_name = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default='pending', index=True)  # pending | done | failed
    created_at = Column(DateTime, default=datetime.now)
    resolved_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return (f"<MtgoJob(job={self.job_id}, kind={self.kind}, player={self.player_id}, "
                f"amount={self.amount}{f', card={self.card_name}' if self.card_name else ''}, "
                f"status={self.status})>")
