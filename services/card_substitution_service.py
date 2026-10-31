"""Learning, and then speaking, the serve's name for a card.

See models/card_substitution.py for why the two vocabularies exist. This is the
half that records what the serve reported and translates a cube list before
anything compares it to custody or sends it as an order.

The translation is deliberately one-way. Custody, orders and the ledger all
speak MTGO's vocabulary, because MTGO is the party that has to recognise a name
-- it refuses an order naming a card it does not know, and refuses it whole. A
cube list is the only thing that arrives in the other vocabulary, so it is
translated once, at the edge, and nothing downstream has to know there were
ever two names.

What is NOT translated back: the depositor is shown the cube's own names, so a
cube owner reading "3 cards aren't on MTGO" sees the names their list uses.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.card_substitution import CardSubstitution


async def learn_substitutions(reported: "Optional[list[dict[str, Any]]]",
                              job_id: "Optional[str]" = None) -> int:
    """Record the pairings a finished trade reported. Returns how many are new.

    `reported` is the serve's own `substitutions[]`: each entry says what it
    `got` and which asked-for name that `satisfies`. An entry missing either
    half is not a pairing and is skipped -- half a mapping translates a cube
    name into nothing, which drops the card from the next order silently.

    Safe to call repeatedly with the same job: several settlers read one
    finished trade, and the watchdog re-reads what the poller already saw.
    """
    if not reported:
        return 0
    learned = 0
    twins = {id(e) for e in await _twin_substitutions(reported)}
    async with AsyncSessionLocal() as session:
        for entry in reported:
            cube_name = entry.get("satisfies")
            mtgo_name = entry.get("got")
            if not cube_name or not mtgo_name:
                logger.warning("substitution from job {} names only one side, "
                               "ignoring: {}", job_id, entry)
                continue
            if id(entry) in twins:
                # Asked for by a name that is already MTGO's: a second printing,
                # not a cube name to translate. booked_names books it as that name.
                continue
            row = await session.get(CardSubstitution, str(cube_name))
            if row is not None:
                if row.mtgo_name != mtgo_name:
                    # The serve changed its mind about a card. Worth saying out
                    # loud: custody booked under the old name is now unreachable
                    # by a list translated with the new one.
                    logger.warning("substitution for {} changed: {} -> {} (job {})",
                                   cube_name, row.mtgo_name, mtgo_name, job_id)
                    row.mtgo_name = str(mtgo_name)
                    row.mtgo_cat_id = entry.get("gotCatId")
                    row.learned_from_job = job_id
                continue
            session.add(CardSubstitution(
                cube_name=str(cube_name), mtgo_name=str(mtgo_name),
                mtgo_cat_id=entry.get("gotCatId"), learned_from_job=job_id))
            learned += 1
            logger.info("library: MTGO calls {!r} {!r} (learned from job {})",
                        cube_name, mtgo_name, job_id)
        await session.commit()
    return learned


async def _twin_substitutions(
        reported: "Optional[list[dict[str, Any]]]") -> "list[dict[str, Any]]":
    """The reported substitutions whose asked-for name is already a learned MTGO
    name, so what moved is that name's other printing.

    A crossover card has two MTGO printings, one under each name, and the serve
    treats them as one card: an order for either takes either. Once a cube name has
    been learned as one of them, every later order asks for that one -- and a
    partner holding only the other printing hands that over instead.
    """
    entries = [e for e in (reported or [])
               if e.get("satisfies") and e.get("got") and e["satisfies"] != e["got"]]
    if not entries:
        return []
    async with AsyncSessionLocal() as session:
        known = set((await session.scalars(
            select(CardSubstitution.mtgo_name).where(
                CardSubstitution.mtgo_name.in_({str(e["satisfies"]) for e in entries})))).all())
    return [e for e in entries if str(e["satisfies"]) in known]


async def booked_names(items: "list[dict[str, Any]]",
                       reported: "Optional[list[dict[str, Any]]]") -> "list[dict[str, Any]]":
    """What a trade moved, under the names the library counts custody by.

    A twin printing is booked as the name that was asked for. The cube is counted
    under that name, so a copy booked under the twin's own name is on the shelf
    where no count can see it -- a deposit that never makes the cube whole, and a
    loan that never comes off what is available, so the copy is lent twice.
    Only as many copies as each substitution covers: an order can ask for both
    names, and a copy that crossed under the name it was asked by is not a twin.
    """
    totals: "dict[str, int]" = {}
    for i in items:
        totals[i["name"]] = totals.get(i["name"], 0) + int(i["qty"])
    for e in await _twin_substitutions(reported):
        got, asked = str(e["got"]), str(e["satisfies"])
        n = min(int(e.get("qty") or 0), totals.get(got, 0))
        if n:
            totals[got] -= n
            totals[asked] = totals.get(asked, 0) + n
    return [{"name": n, "qty": q} for n, q in totals.items() if q]


async def mtgo_names_for(cube_names: "list[str]") -> "dict[str, str]":
    """{cube name: MTGO name} for those of these names that differ.

    Only the ones that differ: a card whose two vocabularies agree has no row,
    and asking for every name in a 540-card cube would return mostly nothing.
    """
    wanted = [str(n) for n in cube_names if n]
    if not wanted:
        return {}
    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(
            select(CardSubstitution).where(
                CardSubstitution.cube_name.in_(wanted)))).all()
    return {str(r.cube_name): str(r.mtgo_name) for r in rows}


async def to_mtgo_names(cards: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
    """A card list in the serve's vocabulary, in the order it came.

    One query for the whole list rather than one per card: this runs on the
    deposit path over a few hundred names, and the cube's own order is kept so
    what is handed over still reads the way the cube does.
    """
    if not cards:
        return cards
    names = [str(c.get("name") or "") for c in cards]
    renames = await mtgo_names_for(names)
    if not renames:
        return cards
    return [c if name not in renames else {**c, "name": renames[name]}
            for c, name in zip(cards, names)]
