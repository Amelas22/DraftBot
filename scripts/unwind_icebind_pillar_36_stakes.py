"""Unwind the stake settlement for draft icebind-pillar-36, which was a DRAW.

Match 9 was first reported as a win for team A. That made the draft 7-5, so the
summary render settled the stakes at 2026-08-28 00:58:39 and booked five stake
debts. Thirty-four seconds later, at 00:59:13, match 9 was corrected to a team B
win, making the draft 6-6.

A draw pays nobody. `utils.generate_draft_summary_embed` only settles stakes when
`team_a_wins > half_matches or team_b_wins > half_matches`; with 12 matches
half_matches is 6, so 6-6 fails both tests and no stake debt should exist. Every
row booked at 00:58:39 is therefore wrong and must come back out.

WHAT MOVED, AND WHAT IS LEFT TO UNDO

  pairing                          stake   settled from wallet   left to undo
  WilliamRegal / Luis Salvatto      100    100 (full)            nothing - see below
  Timr0d       / jasper              50    -                     50 debt
  Slax         / sandydog            90    20 (partial)          70 debt + 20 tix
  iomatic      / Nauseea             50    -                     50 debt
  iomatic      / sandydog            10    -                     10 debt

The WilliamRegal / Luis Salvatto pair was already unwound BY THE PLAYERS: the
debt auto-drew 100 tix from Luis's wallet (wallet_tx 803/804), and at 01:00:06
WilliamRegal paid the 100 straight back (wallet_tx 805/806, "pay to Luis
Salvatto"). Both the ledger and both wallets net to zero, so this script leaves
that pair alone. Re-reversing it would take 100 tix off WilliamRegal twice.

ORDER MATTERS. The debts are forgiven BEFORE the 20 tix goes back to sandydog.
A wallet credit triggers `settle_inflow`, so returning the tix first would let
the auto-draw immediately take it again to pay down the 70 he still owed Slax on
paper. Debts first, cash second.

Forgiveness is booked as compensating `admin` entries rather than by deleting the
draft rows: this ledger is append-only everywhere else, and both the mistaken
settlement and its reversal should stay visible in the debt history. Idempotent
by source_id - a second run finds its own rows and does nothing.

    sandydog  0 -> 20
    Slax     20 -> 0

Dry run by default. Pass --apply to write.

    pipenv run python scripts/unwind_icebind_pillar_36_stakes.py
    pipenv run python scripts/unwind_icebind_pillar_36_stakes.py --apply
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func

from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service, wallet_service

GUILD = "1355718878298116096"
SESSION_ID = "546791165580673025-1787877775"
ACTOR = "unwind-script"
NOTE = (f"Draft #{SESSION_ID} (icebind-pillar-36) was a 6-6 draw; "
        f"its stake settlement was booked in error and is reversed here")

# (debtor, creditor, amount still owed, debtor name, creditor name)
FORGIVE = [
    ("1142175571242131598", "688956996443045944", 50, "jasper",   "Timr0d"),
    ("432006921730392066",  "494009412554457100", 70, "sandydog", "Slax"),
    ("546791165580673025",  "154863938272493571", 50, "Nauseea",  "iomatic"),
    ("432006921730392066",  "154863938272493571", 10, "sandydog", "iomatic"),
]

# The one settlement that actually moved tix and was NOT repaid by the players.
CASH_FROM = "494009412554457100"   # Slax
CASH_TO = "432006921730392066"     # sandydog
CASH_AMOUNT = 20
CASH_SOURCE = "reverse-debt:76ef2a00-192d-4582-94d5-265f60a932b0"

# The pair the players already squared themselves. Verified, never touched.
SELF_UNWOUND = ("634539048534278166", "491700834850177024", "WilliamRegal", "Luis Salvatto")

# Before the cash leg runs, Slax holds the 20 and sandydog holds nothing; after it
# runs the two swap. Both are valid states to start from, because `pay` is
# idempotent by source: a re-run after a partial failure must resume, not refuse.
WALLETS_BEFORE_CASH = {CASH_FROM: 20, CASH_TO: 0}
WALLETS_AFTER_CASH = {CASH_FROM: 0, CASH_TO: 20}


async def tix_debt(session, debtor, creditor):
    """What `debtor` owes `creditor` in TIX. Card rows are excluded because their
    `amount` counts COPIES, and netting those against money is how a lent card
    silently cancels a tix debt."""
    total = (await session.execute(
        select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
            DebtLedger.guild_id == GUILD,
            DebtLedger.player_id == debtor,
            DebtLedger.counterparty_id == creditor,
            debt_service.TIX_ONLY,
        ))).scalar() or 0
    return -int(total)


def unwind_source(debtor, creditor):
    return f"unwind:{SESSION_ID}:{debtor}:{creditor}"


async def already_unwound(session, debtor, creditor):
    found = (await session.execute(
        select(DebtLedger.id).where(
            DebtLedger.guild_id == GUILD,
            DebtLedger.source_type == "admin",
            DebtLedger.source_id == unwind_source(debtor, creditor),
        ).limit(1))).scalar()
    return found is not None


async def main(apply: bool):
    print(f"{'APPLYING' if apply else 'DRY RUN'} — unwinding stakes for {SESSION_ID}\n")

    async with db_session() as session:
        print("debts to forgive:")
        pending = []
        for debtor, creditor, amount, dn, cn in FORGIVE:
            owed = await tix_debt(session, debtor, creditor)
            done = await already_unwound(session, debtor, creditor)
            flag = "ALREADY UNWOUND" if done else ""
            print(f"  {dn:>9} -> {cn:<9} owes {owed:>3}  (expect {amount:>3})  {flag}")
            if done:
                continue
            if owed != amount:
                sys.exit(f"REFUSING: expected {dn} to owe {cn} {amount}, found {owed}. "
                         f"Something has moved since this was written — re-check before running.")
            pending.append((debtor, creditor, amount, dn, cn))

        # The pair the players squared themselves must read as zero, or my
        # assumption that they fully unwound it is wrong.
        a, b, an, bn = SELF_UNWOUND
        self_owed = await tix_debt(session, b, a)
        print(f"\nalready unwound by the players:")
        print(f"  {bn:>9} -> {an:<9} owes {self_owed:>3}  (expect   0)")
        if self_owed != 0:
            sys.exit(f"REFUSING: expected {bn}/{an} to net to zero, found {self_owed}. "
                     f"The players' own reversal is not in the state this script assumes.")

        cash_done = bool(await wallet_service.transfer_legs(session, CASH_SOURCE))
        expected = WALLETS_AFTER_CASH if cash_done else WALLETS_BEFORE_CASH
        print(f"\nwallet balances{' (cash leg ALREADY RETURNED)' if cash_done else ''}:")
        for pid, want in expected.items():
            bal = await wallet_service.balance_in(session, GUILD, pid)
            print(f"  {pid}  {bal:>3}  (expect {want:>3})")
            if bal != want:
                sys.exit(f"REFUSING: wallet {pid} is {bal}, expected {want}. "
                         f"Something has moved since this was written — re-check before running.")

    total = sum(a for _d, _c, a, _dn, _cn in pending)
    cash_note = ("already returned" if cash_done
                 else f"then return {CASH_AMOUNT} tix from Slax to sandydog")
    print(f"\nwould forgive {total} tix of debt across {len(pending)} pair(s), {cash_note}")
    if not pending and cash_done:
        print("nothing left to do — this unwind is already complete")
        return

    if not apply:
        print("\n(dry run — nothing written; pass --apply to do it)")
        return

    # Debts first: a wallet credit triggers settle_inflow, so the 20 tix must not
    # land while sandydog still owes Slax 70 on paper.
    for debtor, creditor, amount, dn, cn in pending:
        async with db_session() as session:
            if await already_unwound(session, debtor, creditor):
                print(f"  skip {dn} -> {cn}: already unwound")
                continue
            src = unwind_source(debtor, creditor)
            session.add(DebtLedger(
                guild_id=GUILD, player_id=debtor, counterparty_id=creditor,
                amount=amount, source_type="admin", source_id=src,
                notes=NOTE, created_by=ACTOR))
            session.add(DebtLedger(
                guild_id=GUILD, player_id=creditor, counterparty_id=debtor,
                amount=-amount, source_type="admin", source_id=src,
                notes=NOTE, created_by=ACTOR))
        print(f"  forgave {amount:>3} tix: {dn} -> {cn}")

    # Cash second. Idempotent by source, and it re-checks Slax's funds itself.
    if cash_done:
        print(f"  skip cash leg: {CASH_AMOUNT} tix already returned")
    else:
        legs = await wallet_service.pay(GUILD, CASH_FROM, CASH_TO, CASH_AMOUNT,
                                        source=CASH_SOURCE, notes=NOTE)
        print(f"  returned {CASH_AMOUNT} tix: Slax -> sandydog "
              f"(wallet_tx {legs[0].id}/{legs[1].id})")

    async with db_session() as session:
        print("\nfinal state:")
        for debtor, creditor, _a, dn, cn in FORGIVE:
            print(f"  {dn:>9} -> {cn:<9} owes {await tix_debt(session, debtor, creditor):>3}")
        for pid, nm in ((CASH_FROM, "Slax"), (CASH_TO, "sandydog")):
            print(f"  {nm:>9} wallet {await wallet_service.balance_in(session, GUILD, pid):>3}")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
