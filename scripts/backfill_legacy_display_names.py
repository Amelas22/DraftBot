#!/usr/bin/env python3
"""One-time data cleaning: resolve display names for players who exist only
in imported legacy history.

The dispnamefill0 migration fills names from evidence already in the
database (sign_up_history, cross-guild stats). Legacy-only players have no
such evidence anywhere -- the old bot's CSVs carried Discord IDs only --
so their names can only come from Discord itself. This script resolves
every player_stats row with an empty display_name through the Discord API:
guild nickname when the bot shares the guild and the member is still in
it, otherwise the account's global name (fetch_user works for any valid
user ID, including users who left). Deleted accounts stay empty and
display as "User <id>".

Once this has run against prod, every read-time live-lookup fallback is
dead code: stored names are complete and the live signup path keeps them
complete going forward.

Usage:
    pipenv run python scripts/backfill_legacy_display_names.py --dry-run
    pipenv run python scripts/backfill_legacy_display_names.py
"""
import argparse
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from database.db_session import db_session          # noqa: E402
from models.player import PlayerStats               # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--dry-run", action="store_true",
                    help="resolve and report, but write nothing")
args = parser.parse_args()

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)


async def resolve_name(guild_id: str, player_id: str) -> str | None:
    """Guild nickname if reachable, else global name; None if the account
    is gone (or the ID was never a real user)."""
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        return None
    guild = client.get_guild(int(guild_id)) if guild_id else None
    if guild:
        member = guild.get_member(pid)
        if member is None:
            try:
                member = await guild.fetch_member(pid)
            except discord.HTTPException:
                member = None
        if member:
            return member.display_name
    try:
        user = await client.fetch_user(pid)
    except discord.HTTPException:
        return None
    return user.global_name or user.name


@client.event
async def on_ready():
    try:
        async with db_session() as s:
            rows = (await s.execute(
                select(PlayerStats).where(
                    (PlayerStats.display_name.is_(None))
                    | (PlayerStats.display_name == "")))).scalars().all()
            print(f"{len(rows)} nameless player_stats rows")
            resolved = unresolved = 0
            for ps in rows:
                name = await resolve_name(ps.guild_id, ps.player_id)
                if name:
                    resolved += 1
                    print(f"  {ps.guild_id}/{ps.player_id} -> {name!r}")
                    if not args.dry_run:
                        ps.display_name = name
                else:
                    unresolved += 1
                    print(f"  {ps.guild_id}/{ps.player_id} -> UNRESOLVED")
            if not args.dry_run:
                await s.commit()
            print(f"resolved {resolved}, unresolved {unresolved}"
                  + (" (dry run, nothing written)" if args.dry_run else ""))
    finally:
        await client.close()


client.run(os.getenv("BOT_TOKEN"))
