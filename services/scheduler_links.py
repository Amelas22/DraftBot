"""Issuing and resolving the short links that open the scheduler page."""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config import scheduler_base_url
from database.db_session import db_session
from models.scheduler import SchedulerLink
from services.availability import naive_utc

LINK_DAYS = 7


@dataclass(frozen=True)
class LinkIdentity:
    user_id: str
    guild_id: str
    view: str
    expires_utc: datetime


async def issue_link(user_id: str, guild_id: str, view: str, now_utc: datetime) -> str:
    """Hand out a link whose fragment is a 7-day bearer token."""
    token = secrets.token_urlsafe(16)
    expires = now_utc + timedelta(days=LINK_DAYS)
    async with db_session() as session:
        session.add(
            SchedulerLink(
                token=token,
                user_id=user_id,
                guild_id=guild_id,
                view=view,
                issued_utc=naive_utc(now_utc),
                expires_utc=naive_utc(expires),
            )
        )
    return f"{scheduler_base_url()}/#{token}"


async def resolve_token(token: str, now_utc: datetime) -> LinkIdentity | None:
    """Who a token belongs to, or None if it is unknown, removed or expired."""
    async with db_session() as session:
        row = (
            await session.execute(select(SchedulerLink).where(SchedulerLink.token == token))
        ).scalar_one_or_none()
        if row is None or row.removed_utc is not None:
            return None
        if naive_utc(now_utc) > row.expires_utc:
            return None
        return LinkIdentity(
            user_id=str(row.user_id),
            guild_id=str(row.guild_id),
            view=str(row.view),
            expires_utc=row.expires_utc.replace(tzinfo=timezone.utc),
        )
