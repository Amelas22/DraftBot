"""TEST FIXTURE: give each named Discord user a deck to borrow.

This stands in for a part of the flow that does not exist yet. In the real thing
the library is built by DEPOSITS -- a player hands cards to the library, and
DraftBot records who owns them -- and a borrower's deck comes from what they
drafted. Neither of those is built, so this writes the loan rows directly to
make the borrow/return half testable on its own.

It is deliberately not a way to run the library. Nothing here records who owns
the cards, so a deck seeded by this script has no depositor to give them back
to, and the vault is not decremented. Use it against the test guild.

Decks are drawn from what Team01 actually holds, because a deck naming cards the
library does not have produces a trade MTGO can never complete: the job sits for
its ten-minute wait and fails, which reads like a bug rather than a bad fixture.

    pipenv run python scripts/seed_card_library.py --guild <id> --user <discord_id> [...]
    pipenv run python scripts/seed_card_library.py --guild <id> --user <id> --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from database.db_session import AsyncSessionLocal  # noqa: E402
from models.card_loan import ACTIVE_STATES, CardLoan  # noqa: E402
from services.card_lending_service import assign_deck  # noqa: E402
from services.mtgo_tradebot_client import get_lending_client  # noqa: E402

# One copy of each single, so two borrowers wanting the same one is a real
# collision; basics are deep enough to pad a deck without contending.
SINGLES = ["Cathar's Companion", "Confront the Unknown", "Emissary of the Sleepless",
           "Fork in the Road", "Ghostly Wings", "Gibbering Fiend"]
# One shared basic on purpose. The singles are distinct so each borrower can see
# they got THEIR deck; the basic is common so several live loans draw on the same
# stack, which is what exercises contention -- concurrent borrows of one card,
# and the serve's one-trade-at-a-time queue behind it.
SHARED_BASIC = "Swamp"


def deck_for(index: int, taken=(), basic_qty: int = 4, single_qty: int = 1):
    """A small deck per user: one single nobody else holds, plus lands.

    The single must be one no other live loan has claimed. Team01 holds exactly
    ONE of each, so two borrowers assigned the same card means the second trade
    asks the library for something already out -- which fails in MTGO, not here,
    long after the fixture looked fine. `taken` is what the guild's active loans
    already claim, so seeding users one at a time still produces distinct decks
    (the argument list's own order cannot know about earlier runs).
    """
    free = [c for c in SINGLES if c not in taken]
    if not free:
        return None
    single = free[index % len(free)]
    return [{"name": single, "qty": single_qty}, {"name": SHARED_BASIC, "qty": basic_qty}]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild", required=True, help="guild id to seed in")
    parser.add_argument("--user", action="append", dest="users", required=True,
                        help="Discord user id to give a deck to (repeatable)")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    parser.add_argument("--basic-qty", type=int, default=4, dest="basic_qty",
                        help="copies of the shared basic per deck. Raise it past "
                             "what the library holds to make borrowers genuinely "
                             "compete for the same stack.")
    parser.add_argument("--single-qty", type=int, default=1, dest="single_qty",
                        help="copies of the deck's single. The library holds ONE of "
                             "each, so anything above 1 makes every borrow short.")
    parser.add_argument("--avoid", action="append", dest="avoid", default=[],
                        help="a card name not to assign (repeatable). Use for cards "
                             "the vault still lists but a borrower may physically "
                             "hold -- a deck naming one produces a trade MTGO "
                             "cannot complete.")
    args = parser.parse_args()

    client = get_lending_client()
    if not client.enabled:
        print("The lending client is disabled -- set MTGO_LENDING_URL and "
              "MTGO_LENDING_TOKEN. Seeding anyway would create decks nothing can trade.")
        return 1

    vault = await client.vault()
    if not vault or not vault.get("available"):
        print("The library's MTGO account is not reachable; not seeding decks "
              "against a vault we cannot see.")
        return 1
    held = {c["name"] for c in (vault.get("top") or [])}
    print(f"library custodian: {vault.get('custodian')}  ({vault.get('distinct')} distinct)")

    async with AsyncSessionLocal() as session:
        live = (await session.scalars(
            select(CardLoan).where(CardLoan.guild_id == str(args.guild),
                                   CardLoan.state.in_(ACTIVE_STATES)))).all()
    taken = {c["name"] for loan in live for c in (loan.cards or []) if c["name"] in SINGLES}
    taken.update(args.avoid)
    if taken:
        print(f"already claimed by live loans: {sorted(taken)}")

    plan = []
    for i, user in enumerate(args.users):
        deck = deck_for(i, taken, args.basic_qty, args.single_qty)
        if deck is None:
            print(f"REFUSED: no unclaimed single left for {user}; "
                  f"the library has {len(SINGLES)} and they are all out.")
            return 1
        # Only singles are exclusive. The shared basic is meant to overlap.
        taken.update(c["name"] for c in deck if c["name"] in SINGLES)
        missing = [c["name"] for c in deck if c["name"] not in held]
        plan.append((user, deck, missing))

    for user, deck, missing in plan:
        line = ", ".join(f"{c['qty']}x {c['name']}" for c in deck)
        warn = f"   ⚠ not visible in the vault: {missing}" if missing else ""
        print(f"  {user}: {line}{warn}")

    if live:
        # One active loan per borrower is a database constraint; seeding over a
        # live loan would fail halfway and leave the guild half-seeded.
        print(f"\n{len(live)} active loan(s) already in this guild: "
              f"{[(l.borrower_id, l.state) for l in live]}")
        blocked = {l.borrower_id for l in live} & {str(u) for u in args.users}
        if blocked:
            print(f"REFUSED: these users already hold an active loan: {sorted(blocked)}")
            return 1

    if not args.apply:
        print("\nDry run -- nothing written. Re-run with --apply.")
        return 0

    for user, deck, _ in plan:
        loan_id = await assign_deck(args.guild, user, deck, source=f"fixture:{user}")
        print(f"  assigned loan {loan_id} to {user}")
    print("\nSeeded. Each user can now run /borrow.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
