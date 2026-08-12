from sqlalchemy import Column, Integer, String, DateTime, Index
from datetime import datetime
from database.models_base import Base


class WalletTx(Base):
    """
    Append-only double-entry log for the tix wallet — the *obligation* / claim ledger.

    One row per movement of a claim. There is deliberately NO materialized balance
    column and NO row status: a holder's balance is always SUM(amount) over their
    rows, exactly like ``DebtLedger``. Nothing is mutated once written — a reversal
    is a new compensating row — so the balance at any past moment is reconstructible
    from the log alone, and it can never drift out of sync with its own history.

    ``amount`` is SIGNED from the holder's perspective (+ credit, - debit). Rows come
    in exactly two shapes:

      * **transfer** — a (pay, receive) PAIR sharing one ``source``, summing to zero.
        Moves a claim between holders (player→player, entry fee→prize wallet, a
        refund back out, a withdraw's tix→``system:in-flight``). The system total is
        unchanged, so the reconciliation invariant holds by construction.
      * **boundary crossing** — a LONE row, and the only thing that changes the system
        total, because value physically entered ('deposit', +n) or left ('withdraw',
        -n) the vault. Written only once the MTGO job reports 'done'.

    Holders are Discord ids, plus synthetic ones for claims no person holds yet:
    ``system:in-flight`` (tix committed to an open withdraw trade) and
    ``prize:tourney:<id>`` (a tournament pot). See wallet_service.is_system_account.

    Reconciliation invariant: bot's on-MTGO tix == SUM(amount).

    ``job_id`` links a boundary row to its MTGO serve job and is that row's idempotency
    key (a replayed poll can't double-book); ``source`` is the transfer pair's key. Both
    are enforced by unique indexes, so double-booking is impossible rather than unlikely.
    """
    __tablename__ = 'wallet_tx'

    id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
    guild_id = Column(String(64), nullable=False)
    player_id = Column(String(64), nullable=False)  # holder (may be synthetic)

    kind = Column(String(32), nullable=False)   # deposit | withdraw | pay | receive | adjust
    amount = Column(Integer, nullable=False)     # + credit, - debit (holder's perspective)

    counterparty_id = Column(String(64), nullable=True)  # other holder (transfers); MTGO user (boundary)
    job_id = Column(String(64), nullable=True)   # serve job id; idempotency key (uq index)
    source = Column(String(64), nullable=True)   # transfer idempotency key ('tourney:…', 'payout:…', uuid)
    notes = Column(String(256), nullable=True)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_wallet_tx_balance_lookup', 'guild_id', 'player_id'),
    )

    def __repr__(self):
        return (f"<WalletTx(holder={self.player_id}, kind={self.kind}, "
                f"amount={self.amount:+d}, job={self.job_id})>")
