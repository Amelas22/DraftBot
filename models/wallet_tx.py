from sqlalchemy import Column, Integer, String, DateTime, Index
from datetime import datetime
from database.models_base import Base


class WalletTx(Base):
    """
    Append-only audit log for the tix wallet — the *obligation* / claim ledger.

    One row per movement of tix through the custodian vault. There is deliberately
    NO materialized balance column: a player's balance is always SUM(amount) over
    their 'done' rows, exactly like ``DebtLedger``. The log is the single source of
    truth, so a balance can never drift out of sync with its own history — the right
    trade-off when the ledger stands in for real value. (The plan sketched a separate
    ``TixWallet(balance)`` table; a computed balance is strictly safer and a
    materialized cache can be layered on later if reads ever get hot.)

    ``amount`` is SIGNED from the player's perspective:
      + credit  — deposit into the vault, or receiving an internal payment
      - debit   — withdraw out of the vault, or paying another player

    ``status``:
      'done'      settled; counts toward the balance.
      'pending'   a withdraw whose MTGO trade is still running. It RESERVES the tix
                  (counts against ``available``) so they can't be double-spent, but
                  does NOT count toward the settled balance until the job completes.
                  Credits are only ever written as 'done' (never on enqueue).
      'cancelled' a reservation released because its job failed. Ignored by every sum.

    Reconciliation invariant: bot's on-MTGO tix == SUM(amount WHERE status='done').

    ``job_id`` links a row to its MTGO serve job (deposit/withdraw) and is the
    idempotency key, so a replayed job poll can't double-book. NULL for internal pays.
    ``source`` doubles as the idempotency key for internal pays (a settlement uuid or
    'debt:<id>'), mirroring ``DebtLedger.source_id``.
    """
    __tablename__ = 'wallet_tx'

    id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
    guild_id = Column(String(64), nullable=False, index=True)
    player_id = Column(String(64), nullable=False, index=True)

    kind = Column(String(32), nullable=False)   # deposit | withdraw | pay | receive | adjust
    amount = Column(Integer, nullable=False)     # + credit, - debit (player's perspective)
    status = Column(String(16), nullable=False, default='done')  # done | pending | cancelled

    counterparty_id = Column(String(64), nullable=True)  # other player (pay/receive); MTGO user for deposit/withdraw
    job_id = Column(String(64), nullable=True, index=True)  # serve job id; idempotency key
    source = Column(String(64), nullable=True)   # 'serve' | 'internal' | 'debt:<id>' | settlement uuid
    notes = Column(String(256), nullable=True)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_wallet_tx_balance_lookup', 'guild_id', 'player_id', 'status'),
    )

    def __repr__(self):
        return (f"<WalletTx(player={self.player_id}, kind={self.kind}, "
                f"amount={self.amount:+d}, status={self.status}, job={self.job_id})>")
