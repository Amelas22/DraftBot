"""Reverse a duplicate 20-tix payment from ScrappyDoo96 (Brandon) to Hoaxed.

Brandon owed Hoaxed 20 from a draft on 2026-08-22. That debt was settled TWICE
in the wallet on 2026-08-25:

    19:33  wallet_tx 620/621  debt:repair-80a2b229-...  the intended settlement,
                                                        booked by
                                                        repair_brandon_wallet_settlements.py
    22:56  wallet_tx 644/645  73efb0be-c801-...         a manual `pay to Hoaxed`,
                                                        the accident

The debt ledger is already correct and settled at zero -- the draft debt, an
`external` settlement, the repair's correction, and the wallet settlement net
out. Nothing there needs touching. Only the second wallet transfer is wrong, and
it is a plain transfer that was never linked to a debt, so reversing it cannot
reopen one.

The reversal is booked as a NEW transfer in the opposite direction rather than
by deleting rows: this ledger is append-only everywhere else, and a correction
that erases its own evidence is worse than one that shows its working. Both the
mistaken payment and its reversal stay visible in `/wallet history`.

Uses wallet_service.pay, the same function the pay button calls -- so it takes
MONEY_LOCK, checks Hoaxed has the funds, and is idempotent by `source`. Re-running
it moves nothing a second time.

    Brandon  60 -> 80
    Hoaxed   80 -> 60

Dry run by default. Pass --apply to write.

    pipenv run python scripts/reverse_brandon_duplicate_payment.py
    pipenv run python scripts/reverse_brandon_duplicate_payment.py --apply
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_session import db_session
from services import wallet_service

GUILD = "1355718878298116096"
BRANDON = "294316950081896448"      # ScrappyDoo96
HOAXED = "126581771151212544"
AMOUNT = 20

# The transfer being undone. Naming the source it reverses makes the pairing
# obvious in the ledger, and doubles as the idempotency key: a second run finds
# this source already booked and does nothing.
MISTAKEN_SOURCE = "73efb0be-c801-41e2-acf0-4cd06894603f"
REVERSAL_SOURCE = f"reverse-{MISTAKEN_SOURCE}"

EXPECTED = {BRANDON: 80, HOAXED: 60}


async def main(apply: bool) -> None:
    async with db_session() as session:
        before = {
            BRANDON: await wallet_service.balance_in(session, GUILD, BRANDON),
            HOAXED: await wallet_service.balance_in(session, GUILD, HOAXED),
        }
        # The row being reversed must actually exist, or this is not the
        # situation the script was written for.
        legs = await wallet_service.transfer_legs(session, MISTAKEN_SOURCE)
        already = await wallet_service.transfer_legs(session, REVERSAL_SOURCE)

    print(f"mistaken transfer {MISTAKEN_SOURCE}: "
          f"{'found, ' + str(len(legs)) + ' legs' if legs else 'NOT FOUND'}")
    if not legs:
        sys.exit("refusing to act: the payment this reverses is not in the ledger")
    if already:
        print("already reversed (idempotent) -- nothing to do")
        return

    print(f"\n  Brandon (ScrappyDoo96) {before[BRANDON]:>4} -> {before[BRANDON] + AMOUNT}")
    print(f"  Hoaxed                 {before[HOAXED]:>4} -> {before[HOAXED] - AMOUNT}")
    if before[HOAXED] < AMOUNT:
        sys.exit(f"refusing to act: Hoaxed holds {before[HOAXED]}, cannot return {AMOUNT}")

    if not apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return

    # Hoaxed pays it back. Not an `adjust`: that is one-sided and would change the
    # system total, which would then show up as a discrepancy in reconciliation.
    # This moved between two holders, so it has to move back the same way.
    await wallet_service.pay(
        GUILD, HOAXED, BRANDON, AMOUNT,
        source=REVERSAL_SOURCE,
        notes="Reversal: duplicate payment to Hoaxed (debt already settled)",
    )

    async with db_session() as session:
        after = {
            BRANDON: await wallet_service.balance_in(session, GUILD, BRANDON),
            HOAXED: await wallet_service.balance_in(session, GUILD, HOAXED),
        }
    ok = after == EXPECTED
    print(f"\nBrandon {before[BRANDON]} -> {after[BRANDON]}")
    print(f"Hoaxed  {before[HOAXED]} -> {after[HOAXED]}")
    print("as expected" if ok else f"UNEXPECTED -- expected {EXPECTED}, CHECK BEFORE PROCEEDING")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
