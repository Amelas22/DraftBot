#!/usr/bin/env python
"""Create libraries, bind servers to them, and say who may borrow.

Everything here is deliberately OUT of Discord. A server admin can rewrite
`configs/<guild>.json` through the bot's own commands, so anything they could
reach is a deposit they could lower, a cube they could make free, or a library
they could repoint their server at.

    # a communal library for Cube Night, free, and the server that draws on it
    pipenv run python scripts/library_tool.py create cubenight "Cube Night" \
        --kind communal --deposit 0
    pipenv run python scripts/library_tool.py bind 1234567890 cubenight

    # a rental library, 100 tix deposit and 2 tix a week
    pipenv run python scripts/library_tool.py create lotuslounge "Lotus Lounge" \
        --kind rental --deposit 100

    pipenv run python scripts/library_tool.py list
    pipenv run python scripts/library_tool.py offer lotuslounge PowerLSV
    pipenv run python scripts/library_tool.py members lotuslounge
    pipenv run python scripts/library_tool.py invite lotuslounge 1300917843226787
    pipenv run python scripts/library_tool.py uninvite lotuslounge 1300917843226787
    pipenv run python scripts/library_tool.py open lotuslounge
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                                    # noqa: E402

from database.db_session import AsyncSessionLocal                # noqa: E402
from models.library import KINDS, Library                        # noqa: E402
from models.library_cube import LibraryCube                      # noqa: E402
from models.library_server import LibraryServer                  # noqa: E402
import services.library_access_service as access                 # noqa: E402
import services.library_service as libs                          # noqa: E402


async def _create(library_id, name, kind, deposit, by, replace):
    async with AsyncSessionLocal() as s:
        row = await s.get(Library, library_id)
        if row is not None and not replace:
            print(f"{library_id} already exists: {row.kind}, deposit={row.collateral_tix} "
                  )
            print("   Pass --replace to change its terms. Everything already "
                  "deposited stays\n       where it is; only what borrowing costs moves.")
            return
        if row is None:
            s.add(Library(id=library_id, name=name, kind=kind,
                          collateral_tix=deposit, created_by=by))
            print(f"{library_id}: created — {kind}, deposit={deposit}")
        else:
            was = f"{row.kind}, deposit={row.collateral_tix}"
            row.kind, row.collateral_tix = kind, deposit
            row.name = name
            print(f"{library_id}: now {kind}, deposit={deposit} (was {was})")
        await s.commit()
    if kind == "communal":
        print("   ⚠️  Communal means any member's deposit LISTS a cube here.")
        print("       Right where the cards are the members' own; wrong where "
              "they are a sponsor's.")


async def _list():
    async with AsyncSessionLocal() as s:
        libraries = list((await s.scalars(select(Library))).all())
        bindings = list((await s.scalars(select(LibraryServer))).all())
        cubes = list((await s.scalars(select(LibraryCube))).all())
    if not libraries:
        print("No libraries. Nothing can be borrowed anywhere.")
        return
    for lib in sorted(libraries, key=lambda r: str(r.id)):
        servers = [b.guild_id for b in bindings if b.library_id == lib.id]
        offered = sorted(c.cube_id for c in cubes if c.library_id == lib.id)
        members = await access.members(lib.id)
        print(f"{lib.id}  ({lib.kind})  deposit={lib.collateral_tix}")
        print(f"   servers: {', '.join(servers) or 'NONE — nobody can borrow'}")
        print(f"   cubes  : {', '.join(offered) or 'none'}")
        listed = (f"invite-only, {len(members)} member(s)" if members
                  else "open to those servers")
        print(f"   access : {listed}")


async def _bind(guild, library_id, by):
    async with AsyncSessionLocal() as s:
        if await s.get(Library, library_id) is None:
            print(f"No library called {library_id}. `list` shows what there is.")
            return
        row = await s.get(LibraryServer, str(guild))
        if row is None:
            s.add(LibraryServer(guild_id=str(guild), library_id=library_id,
                                bound_by=by))
            print(f"{guild}: now draws on {library_id}.")
        else:
            was = row.library_id
            if was == library_id:
                print(f"{guild} already draws on {library_id}.")
                return
            row.library_id = library_id
            print(f"{guild}: {was} → {library_id}.")
            print("   ⚠️  Decks already ASSIGNED keep the old library's price: "
                  "the loan\n       records which shelf its cards came off, and "
                  "rebinding must not\n       reprice a deck somebody was already "
                  "promised.")
        await s.commit()


async def _unbind(guild):
    async with AsyncSessionLocal() as s:
        row = await s.get(LibraryServer, str(guild))
        if row is None:
            print(f"{guild} draws on no library already.")
            return
        await s.delete(row)
        await s.commit()
    print(f"{guild}: unbound. Nobody there can borrow, deposit or withdraw.")


async def _offer(library_id, cube, by):
    if await libs.offer_cube(library_id, cube, by):
        print(f"{library_id}: now offers {cube}.")
    else:
        print(f"{library_id} already offers {cube}.")


async def _members(library_id):
    people = await access.members(library_id)
    if not people:
        print(f"{library_id}: nobody listed — it lends to everyone in its servers.")
        return
    print(f"{library_id}: invite-only, {len(people)} member(s):")
    for player in sorted(people):
        print(f"   {player}")


async def _invite(library_id, player, by):
    if await access.invite(library_id, player, by):
        print(f"{library_id}: {player} added.")
        print("   ⚠️  This was the FIRST member, so the library is now "
              "invite-only.\n       Everyone who could borrow yesterday cannot today.")
    else:
        print(f"{library_id}: {player} may borrow (already listed, or added "
              f"alongside others).")


async def _uninvite(library_id, player):
    outcome = await access.uninvite(library_id, player)
    if outcome == "not_listed":
        print(f"{library_id}: {player} was not listed — nothing to do.")
        print("   Check the id: the members who ARE listed still borrow.")
    elif outcome == "would_open":
        print(f"{library_id}: refused — {player} is the LAST member.")
        print(f"   Removing them would leave nobody listed, which opens the "
              f"library to\n       everyone in its servers, {player} included.")
        print("   Run `open` if that is what you want; otherwise invite their "
              "replacement first.")
    else:
        print(f"{library_id}: {player} removed.")


async def _open(library_id):
    cleared = await access.open_library(library_id)
    if not cleared:
        print(f"{library_id}: nobody was listed — it already lends to everyone.")
        return
    print(f"{library_id}: open. {cleared} member(s) cleared.")
    print("   ⚠️  Everyone in its servers may borrow now.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    who = os.environ.get("USER", "shell")

    p = sub.add_parser("create", help="create a library, or change its terms")
    p.add_argument("library")
    p.add_argument("name")
    p.add_argument("--kind", choices=list(KINDS), default="rental")
    p.add_argument("--deposit", type=int, default=0, help="refundable, in tix")
    p.add_argument("--replace", action="store_true", help="change existing terms")
    p.add_argument("--by", default=who)

    sub.add_parser("list", help="every library, its servers, cubes and access")

    p = sub.add_parser("bind", help="point a server at a library")
    p.add_argument("guild")
    p.add_argument("library")
    p.add_argument("--by", default=who)

    p = sub.add_parser("unbind", help="stop a server borrowing at all")
    p.add_argument("guild")

    p = sub.add_parser("offer", help="list a cube for a library")
    p.add_argument("library")
    p.add_argument("cube")
    p.add_argument("--by", default=who)

    p = sub.add_parser("members", help="who may borrow from a library")
    p.add_argument("library")

    p = sub.add_parser("invite", help="trust somebody to borrow")
    p.add_argument("library")
    p.add_argument("player", help="their Discord user id")
    p.add_argument("--by", default=who)

    p = sub.add_parser("uninvite", help="stop somebody borrowing")
    p.add_argument("library")
    p.add_argument("player")

    p = sub.add_parser("open", help="let everyone in its servers borrow again")
    p.add_argument("library")

    a = ap.parse_args()
    match a.cmd:
        case "create":
            asyncio.run(_create(a.library, a.name, a.kind, a.deposit,
                                a.by, a.replace))
        case "list":
            asyncio.run(_list())
        case "bind":
            asyncio.run(_bind(a.guild, a.library, a.by))
        case "unbind":
            asyncio.run(_unbind(a.guild))
        case "offer":
            asyncio.run(_offer(a.library, a.cube, a.by))
        case "members":
            asyncio.run(_members(a.library))
        case "invite":
            asyncio.run(_invite(a.library, a.player, a.by))
        case "uninvite":
            asyncio.run(_uninvite(a.library, a.player))
        case "open":
            asyncio.run(_open(a.library))


if __name__ == "__main__":
    main()
