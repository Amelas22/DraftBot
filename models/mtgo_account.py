"""
MTGO account link — maps a Discord user to their MTGO username.

Why a dedicated table (not a column on player_stats):
  * The load-bearing lookup is the REVERSE direction, mtgo_username -> discord_user_id,
    used when auto-reporting a spectated MTGO match result back to DraftBot. That needs
    a UNIQUE key on the (normalized) handle, which a plain column can't give.
  * An MTGO account belongs to a PERSON, so it's keyed by the global Discord id — unlike
    player_stats, whose PK is (player_id, guild_id).
  * player_stats rows are created lazily (only after a player has stats); a link must be
    able to exist before that.

MTGO handles are effectively case-insensitive, so we store the original casing for
display plus a normalized lower form carrying the UNIQUE constraint for lookups.
"""
from datetime import datetime

from sqlalchemy import Column, String, Boolean, DateTime, select

from database.models_base import Base
from database.db_session import db_session


def normalize_mtgo(name: str) -> str:
    """Canonical form for matching + uniqueness: trimmed and lowercased."""
    return (name or "").strip().lower()


class MtgoAccount(Base):
    __tablename__ = 'mtgo_accounts'

    discord_user_id = Column(String(64), primary_key=True)                 # global per-person key
    mtgo_username = Column(String(128), nullable=False)                    # original casing (display)
    mtgo_username_lower = Column(String(128), nullable=False,
                                 unique=True, index=True)                  # reverse-lookup key
    verified = Column(Boolean, default=False)                             # reserved for a future verify step
    linked_at = Column(DateTime, default=datetime.now)
    guild_id = Column(String(64), nullable=True)                          # where first linked (metadata only)

    def __repr__(self):
        return f"<MtgoAccount(discord={self.discord_user_id}, mtgo={self.mtgo_username})>"

    @classmethod
    async def link(cls, discord_user_id, mtgo_username, guild_id=None):
        """
        Create or update the caller's MTGO link. One MTGO handle maps to exactly one
        Discord user. Returns (status, detail):
          ("ok", mtgo_username)            link created/updated
          ("conflict", other_discord_id)   that handle is already linked to someone else
          ("empty", None)                  blank username supplied
        """
        cleaned = (mtgo_username or "").strip()
        lower = normalize_mtgo(cleaned)
        if not lower:
            return ("empty", None)
        did = str(discord_user_id)
        async with db_session() as session:
            existing = (await session.execute(
                select(cls).where(cls.mtgo_username_lower == lower))).scalar_one_or_none()
            if existing is not None and existing.discord_user_id != did:
                return ("conflict", existing.discord_user_id)
            row = await session.get(cls, did)
            if row is None:
                session.add(cls(
                    discord_user_id=did, mtgo_username=cleaned, mtgo_username_lower=lower,
                    guild_id=(str(guild_id) if guild_id else None)))
            else:
                row.mtgo_username = cleaned
                row.mtgo_username_lower = lower
                if guild_id and not row.guild_id:
                    row.guild_id = str(guild_id)
            return ("ok", cleaned)

    @classmethod
    async def discord_for_mtgo(cls, mtgo_username):
        """Reverse lookup: MTGO username -> Discord user id (or None). Used by the API/worker."""
        lower = normalize_mtgo(mtgo_username)
        if not lower:
            return None
        async with db_session() as session:
            row = (await session.execute(
                select(cls).where(cls.mtgo_username_lower == lower))).scalar_one_or_none()
            return row.discord_user_id if row else None

    @classmethod
    async def get_for_discord(cls, discord_user_id):
        """Forward lookup: Discord user id -> MtgoAccount row (or None)."""
        async with db_session() as session:
            return await session.get(cls, str(discord_user_id))

    @classmethod
    async def usernames_for_discord_ids(cls, discord_user_ids):
        """Batch forward lookup: {discord_user_id -> mtgo_username} for the linked ones.

        Unlinked ids are simply absent from the result. One query instead of N — used
        when resolving a whole pairing list for the worker.
        """
        ids = [str(d) for d in discord_user_ids if d is not None]
        if not ids:
            return {}
        async with db_session() as session:
            rows = (await session.execute(
                select(cls).where(cls.discord_user_id.in_(ids)))).scalars().all()
            return {r.discord_user_id: r.mtgo_username for r in rows}

    @classmethod
    async def unlink(cls, discord_user_id):
        """Remove a link. Returns True if a row was deleted."""
        async with db_session() as session:
            row = await session.get(cls, str(discord_user_id))
            if row is None:
                return False
            await session.delete(row)
            return True
