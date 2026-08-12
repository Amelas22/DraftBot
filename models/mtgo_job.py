"""
Durable record of an in-flight MTGO serve job (deposit/withdraw trade).

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
    kind = Column(String(16), nullable=False)              # deposit | withdraw
    guild_id = Column(String(64), nullable=False)
    player_id = Column(String(64), nullable=False)
    mtgo_user = Column(String(128), nullable=False)
    amount = Column(Integer, nullable=False)
    in_flight_source = Column(String(80), nullable=True)   # withdraws: the commit transfer's source tag
    status = Column(String(16), nullable=False, default='pending')  # pending | done | failed
    created_at = Column(DateTime, default=datetime.now)
    resolved_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return (f"<MtgoJob(job={self.job_id}, kind={self.kind}, player={self.player_id}, "
                f"amount={self.amount}, status={self.status})>")
