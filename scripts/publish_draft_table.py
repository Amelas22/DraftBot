"""Publish a draft's table page for a draft that never got one.

The table page ships with the log embed, so a draft whose log unlocked before
the feature existed -- or one whose publish failed -- has a stored log and no
page. This renders and uploads that page after the fact.

It calls services.draft_table_publisher.publish, the same function the live
path calls, rather than re-deriving the render: a backfill that draws the page
its own way is a second implementation to keep in step.

    pipenv run python scripts/publish_draft_table.py navigation-orb-45
    pipenv run python scripts/publish_draft_table.py navigation-orb-45 --apply

Dry run by default. The embed in #draft-logs was already posted without the
link and is NOT edited -- this hands back a URL to share, nothing more.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def find(identifier: str):
    """The session row for a friendly_id, or a session_id, or None.

    friendly_id is not unique (see resolve_filename), so an ambiguous name is
    reported rather than silently resolved to whichever row sorted first.
    """
    from sqlalchemy import or_, select
    from sqlalchemy.orm import undefer

    from database.db_session import db_session
    from models.draft_session import DraftSession

    async with db_session() as session:
        rows = (await session.execute(
            # undefer: draft_data is deferred at the mapper; this script
            # publishes the log, so it is one of the few real readers.
            select(DraftSession)
            .options(undefer(DraftSession.draft_data))
            .where(or_(
                DraftSession.friendly_id == identifier,
                DraftSession.session_id == identifier,
            ))
        )).scalars().all()

        if len(rows) > 1:
            print(f"{identifier} matches {len(rows)} drafts -- pass a session_id:")
            for row in rows:
                print(f"   {row.session_id}   {row.teams_start_time}   {row.session_type}")
            return None
        return rows[0] if rows else None


async def main(identifier: str, apply: bool, force: bool) -> int:
    from database.db_session import db_session
    from services.draft_table_publisher import publish

    row = await find(identifier)
    if row is None:
        print(f"no draft found for {identifier!r}")
        return 1

    print(f"draft:   {row.friendly_id}  ({row.session_id})")
    print(f"type:    {row.session_type}   started {row.teams_start_time}")
    print(f"cube:    {row.cube}")

    if row.drafttable_url and not force:
        print(f"\nalready published: {row.drafttable_url}")
        print("pass --force to render and upload it again")
        return 0

    if not row.draft_data:
        print("\nno stored draft_data -- the log never arrived, so there is "
              "nothing to render. Nothing to do here.")
        return 1

    # Only premade drafts get a page on the live path. Not enforced: the gate
    # is a product decision about what the bot posts unasked, and this is an
    # operator asking for one particular draft on purpose.
    if row.session_type != "premade":
        print(f"\nnote: {row.session_type} drafts do not get a page automatically")

    if not apply:
        print("\nDRY RUN -- would render and upload the page. Re-run with --apply.")
        return 0

    url = await publish(row.draft_data, row)
    if not url:
        print("\npublish failed -- see the traceback above for the reason "
              "(the publisher logs and swallows every error by design)")
        return 1

    print(f"\npublished: {url}")

    # Stamp the row so a later draft sharing this friendly_id gets a suffixed
    # filename instead of overwriting this page. resolve_filename checks the
    # recorded URLs, so a page nobody recorded is a page that can be clobbered.
    #
    # Matched on session_id, not session.get: the primary key is a surrogate
    # `id`, so a get() on session_id silently finds nothing.
    from sqlalchemy import update

    from models.draft_session import DraftSession

    async with db_session() as session:
        stamped = (await session.execute(
            update(DraftSession)
            .where(DraftSession.session_id == row.session_id)
            .values(drafttable_url=url)
        )).rowcount
        await session.commit()

    if stamped:
        print("recorded drafttable_url on the session row")
        return 0

    # Loud, because the page is up and nothing points at it: the next draft to
    # take this friendly_id would overwrite it without warning.
    print(f"WARNING: page is published but {row.session_id} was not stamped")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("identifier", help="friendly_id (navigation-orb-45) or session_id")
    ap.add_argument("--apply", action="store_true",
                    help="actually render and upload (default is a dry run)")
    ap.add_argument("--force", action="store_true",
                    help="republish even if the row already records a page")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.identifier, args.apply, args.force)))
