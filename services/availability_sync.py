"""Poll Spaces for usual weeks the scheduler page saved, and keep our copy."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import PlayerAvailability, SchedulerLink
from services.availability import (
    MAX_FILE_BYTES,
    InvalidWeek,
    UsualWeek,
    cells_from_bytes,
    cells_to_bytes,
    naive_utc,
    parse_usual_week,
)
from services.scheduler_links import LINK_DAYS, link_issued_since
from services.scheduler_spaces import PREFIX, SchedulerSpaces, SpacesError

_KEY = re.compile(r"scheduler/availability/([0-9]+)\.json")
_STORED_VERSION = 1


@dataclass
class SyncResult:
    stored: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    links_removed: int = 0


async def get_usual_week(user_id: str) -> UsualWeek | None:
    """The stored usual week for a player, or None if none was saved."""
    async with db_session() as session:
        row = await session.get(PlayerAvailability, user_id)
        if row is None:
            return None
        return UsualWeek(
            zone=str(row.zone),
            cells=cells_from_bytes(bytes(row.cells)),
        )


class AvailabilitySync:
    def __init__(self, spaces: SchedulerSpaces) -> None:
        self._spaces = spaces
        # key -> the LastModified that failed validation; not refetched until it changes.
        self._rejected: dict[str, datetime] = {}

    async def poll_once(self, now_utc: datetime) -> SyncResult:
        result = SyncResult()
        result.links_removed = await self._remove_expired_links(now_utc)
        # +1 day: presigned PUTs are signed just after issue and the bot may be
        # down across a link's last moments, so a save can land after expiry.
        if not await link_issued_since(now_utc - timedelta(days=LINK_DAYS + 1)):
            return result
        listing = await self._spaces.list_modified(f"{PREFIX}availability/")
        async with db_session() as session:
            stored: dict[str, datetime] = dict(
                (
                    await session.execute(
                        select(PlayerAvailability.user_id, PlayerAvailability.object_modified_utc)
                    )
                ).tuples().all()
            )
        for key, modified in listing:
            match = _KEY.fullmatch(key)
            if match is None or self._rejected.get(key) == modified:
                continue
            user_id = match.group(1)
            seen = stored.get(user_id)
            if seen is not None and naive_utc(modified) <= seen:
                continue
            try:
                await self._sync_key(user_id, key, modified, now_utc, result)
            except SpacesError as e:
                logger.warning(f"Could not fetch {key}, will retry: {e}")
            except Exception:
                logger.exception(f"Failed to sync {key}")
        return result

    async def _remove_expired_links(self, now_utc: datetime) -> int:
        removed = 0
        async with db_session() as session:
            links = (
                await session.execute(
                    select(SchedulerLink).where(
                        SchedulerLink.expires_utc < naive_utc(now_utc),
                        SchedulerLink.removed_utc.is_(None),
                    )
                )
            ).scalars().all()
            for link in links:
                try:
                    await self._spaces.delete(f"{PREFIX}link/{link.token}.json")
                except SpacesError as e:
                    logger.warning(f"Could not delete link file for {link.token}: {e}")
                    continue
                link.removed_utc = naive_utc(now_utc)
                removed += 1
        return removed

    async def _read_week(self, key: str) -> UsualWeek:
        raw = await self._spaces.fetch(key, MAX_FILE_BYTES)
        if raw is None:
            raise InvalidWeek(f"larger than {MAX_FILE_BYTES} bytes")
        return parse_usual_week(raw)

    async def _sync_key(
        self, user_id: str, key: str, modified: datetime, now_utc: datetime, result: SyncResult
    ) -> None:
        try:
            week = await self._read_week(key)
        except InvalidWeek as e:
            logger.warning(f"Rejected {key}: {e}")
            self._rejected[key] = modified
            result.rejected.append(key)
            return
        object_modified = naive_utc(modified)
        async with db_session() as session:
            row = await session.get(PlayerAvailability, user_id)
            if row is not None and object_modified <= row.object_modified_utc:
                return
            if row is None:
                row = PlayerAvailability(user_id=user_id)
                session.add(row)
            row.zone = week.zone
            row.cells = cells_to_bytes(week.cells)
            row.version = _STORED_VERSION
            row.object_modified_utc = object_modified
            row.stored_utc = naive_utc(now_utc)
        self._rejected.pop(key, None)
        result.stored.append(user_id)
        logger.info(f"Stored usual week for {user_id}")
