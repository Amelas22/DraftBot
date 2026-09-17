"""Re-settle three debts that were booked as `external` when the wallet was meant.

On 2026-08-24 23:32-23:33 ScrappyDoo96 (Brandon Pascal) marked three draft debts
settled, choosing `external` -- "paid outside the wallet" -- for all three. His
wallet was therefore never debited. He believes he intended to pay them from the
wallet, which held 100 tix at the time; his balance is 70 higher than he expects,
and 70 is exactly those three debts.

    Hoaxed              126581771151212544   20
    401730574005436426                       30
    509545111722524710                       20

This restores each debt and re-settles it through settle_debt_from_wallet -- the
same function the wallet button calls -- so the tix actually move and the ledger
records `wallet` rather than `external`.

The restoration is a compensating entry, not a delete: this ledger is
append-only everywhere else (a refund books a reversing pair and leaves the
original standing), and a correction that erases its own evidence is worse than
one that shows its working.

Dry run by default. Pass --apply to write.

    pipenv run python scripts/repair_brandon_wallet_settlements.py
    pipenv run python scripts/repair_brandon_wallet_settlements.py --apply
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service, wallet_service
from services.mtgo_resolution_service import settle_debt_from_wallet, settle_inflow

GUILD = "1355718878298116096"
PAYER = "294316950081896448"          # ScrappyDoo96 / Brandon Pascal
ACTOR = "repair-script"

# (creditor, amount, source_id of the external settlement being corrected)
CASES = [
    ("126581771151212544", 20, "80a2b229-3414-47a8-a62f-ebcaa7733772"),
    ("401730574005436426", 30, "4506fd36-40c5-46b1-abda-6857f1142d21"),
    ("509545111722524710", 20, "1881d153-7e41-4af9-85c9-330bc9791dad"),
]
EXPECTED_BEFORE = 150
EXPECTED_AFTER = 80


async def tix_debt(session, payer, creditor):
    """What `payer` owes `creditor` in TIX. Card rows are excluded because their
    `amount` counts COPIES, and netting those against money is how a lent card
    silently cancels a tix debt."""
    total = (await session.execute(
        select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
            DebtLedger.guild_id == GUILD,
            DebtLedger.player_id == payer,
            DebtLedger.counterparty_id == creditor,
            debt_service.TIX_ONLY,
        ))).scalar() or 0
    return -int(total)


async def already_repaired(session, source_id):
    """This correction is tagged with the source_id it corrects, so a second run
    is a no-op rather than a second 70 tix."""
    found = (await session.execute(
        select(DebtLedger.id).where(
            DebtLedger.guild_id == GUILD,
            DebtLedger.source_type == "admin",
            DebtLedger.source_id == f"repair:{source_id}",
        ).limit(1))).scalar()
    return found is not None


async def main(apply: bool):
    print(f"{'APPLYING' if apply else 'DRY RUN'} — payer {PAYER}\n")

    async with db_session() as session:
        before = await wallet_service.balance_in(session, GUILD, PAYER)
        print(f"payer balance now: {before}")
        if before != EXPECTED_BEFORE:
            sys.exit(f"REFUSING: expected {EXPECTED_BEFORE}, found {before}. "
                     f"Something has moved since this was written — re-check before running.")
        for creditor, amount, src in CASES:
            owed = await tix_debt(session, PAYER, creditor)
            done = await already_repaired(session, src)
            print(f"  {creditor}  {amount:>3} tix   currently owed {owed:>3}"
                  f"   {'ALREADY REPAIRED' if done else ''}")
            if owed != 0 and not done:
                sys.exit(f"REFUSING: expected the debt to {creditor} to read as settled "
                         f"(0), found {owed}. The ledger is not in the state this repair "
                         f"assumes.")
    total = sum(a for _c, a, _s in CASES)
    print(f"\nwould move {total} tix out of the payer's wallet, leaving {before - total}")

    if not apply:
        print("\n(dry run — nothing written; pass --apply to do it)")
        return

    moved = 0
    for creditor, amount, src in CASES:
        async with db_session() as session:
            if await already_repaired(session, src):
                print(f"  skip {creditor}: already repaired")
                continue
            note = (f"Correction: settlement {src} was booked as external; "
                    f"the payer intended to pay from the wallet")
            # Restore the debt so settle_debt_from_wallet has something to settle.
            session.add(DebtLedger(
                guild_id=GUILD, player_id=PAYER, counterparty_id=creditor,
                amount=-amount, source_type="admin", source_id=f"repair:{src}",
                notes=note, created_by=ACTOR))
            session.add(DebtLedger(
                guild_id=GUILD, player_id=creditor, counterparty_id=PAYER,
                amount=amount, source_type="admin", source_id=f"repair:{src}",
                notes=note, created_by=ACTOR))
        # Outside that session, and NOT holding MONEY_LOCK: settle_debt_from_wallet
        # takes the lock itself and it is not reentrant.
        res = await settle_debt_from_wallet(GUILD, PAYER, creditor, amount,
                                            link_id=f"repair-{src}")
        if not res.get("ok"):
            sys.exit(f"FAILED on {creditor}: {res}. Earlier cases already applied — "
                     f"re-run to continue from here (it is idempotent).")
        moved += amount
        print(f"  settled {amount} tix to {creditor}")

    # What would have happened had he paid from the wallet on the night: the tix
    # arriving at each creditor draw against whatever THEY owe.
    for creditor, _a, _s in CASES:
        drawn = await settle_inflow(GUILD, creditor, "a repaired settlement")
        if drawn:
            print(f"  {creditor}: inflow auto-settled {len(drawn)} of their own debts")

    async with db_session() as session:
        after = await wallet_service.balance_in(session, GUILD, PAYER)
    print(f"\nmoved {moved} tix. payer balance {before} -> {after}"
          f"  {'as expected' if after == EXPECTED_AFTER else 'UNEXPECTED — CHECK'}")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
