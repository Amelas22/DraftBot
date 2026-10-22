"""Issuing the short links that open the scheduler page."""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import SchedulerLink
from services.scheduler_spaces import PAGE_URL, PREFIX, SchedulerSpaces

LINK_DAYS = 7


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


async def issue_usual_link(spaces: SchedulerSpaces, user_id: str, now_utc: datetime) -> str:
    """Hand out a link to the player's usual-week page.

    File 8 is written before the row commits, so a returned link always
    resolves; if Spaces fails, nothing is committed and SpacesError propagates.
    """
    token = secrets.token_urlsafe(16)
    expires = now_utc + timedelta(days=LINK_DAYS)
    key = f"{PREFIX}availability/{user_id}.json"
    await spaces.put_public_json(
        f"{PREFIX}link/{token}.json",
        {
            "format": "draftbot.scheduler.link",
            "version": 1,
            "view": "usual",
            "user": user_id,
            "exp": expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "urls": {
                "usual_get": await spaces.presign("get_object", key),
                "usual_put": await spaces.presign("put_object", key),
            },
        },
    )
    async with db_session() as session:
        session.add(
            SchedulerLink(
                token=token,
                user_id=user_id,
                view="usual",
                issued_utc=_naive_utc(now_utc),
                expires_utc=_naive_utc(expires),
            )
        )
    return f"{PAGE_URL}#{token}"


async def link_issued_since(cutoff_utc: datetime) -> bool:
    async with db_session() as session:
        found = await session.execute(
            select(SchedulerLink.id)
            .where(SchedulerLink.issued_utc >= _naive_utc(cutoff_utc))
            .limit(1)
        )
        return found.first() is not None
