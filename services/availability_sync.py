"""Poll Spaces for usual weeks the scheduler page saved, and keep our copy."""
import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import PlayerAvailability, SchedulerLink
from services.availability import (
    MAX_FILE_BYTES,
    InvalidWeek,
    UsualWeek,
    decode_cells,
    encode_cells,
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


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


async def get_usual_week(user_id: str) -> UsualWeek | None:
    """The stored usual week for a player, or None if none was saved."""
    async with db_session() as session:
        row = await session.get(PlayerAvailability, user_id)
        if row is None:
            return None
        return UsualWeek(
            zone=str(row.zone),
            cells=decode_cells(base64.b64encode(bytes(row.cells)).decode("ascii")),
        )


class AvailabilitySync:
    def __init__(self, spaces: SchedulerSpaces) -> None:
        self._spaces = spaces
        # key -> the LastModified that failed validation; not refetched until it changes.
        self._rejected: dict[str, datetime] = {}

    async def poll_once(self, now_utc: datetime) -> SyncResult:
        result = SyncResult()
        result.links_removed = await self._remove_expired_links(now_utc)
        if not await link_issued_since(now_utc - timedelta(days=LINK_DAYS)):
            return result
        listing = await self._spaces.list_modified(f"{PREFIX}availability/")
        for key, modified in listing:
            match = _KEY.fullmatch(key)
            if match is None:
                continue
            try:
                await self._sync_key(match.group(1), key, modified, now_utc, result)
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
                        SchedulerLink.expires_utc < _naive_utc(now_utc),
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
                link.removed_utc = _naive_utc(now_utc)
                removed += 1
        return removed

    async def _sync_key(
        self, user_id: str, key: str, modified: datetime, now_utc: datetime, result: SyncResult
    ) -> None:
        if self._rejected.get(key) == modified:
            return
        object_modified = _naive_utc(modified)
        async with db_session() as session:
            row = await session.get(PlayerAvailability, user_id)
            if row is not None and object_modified <= row.object_modified_utc:
                return
            raw = await self._spaces.fetch(key, MAX_FILE_BYTES)
            reason: str | None = None
            week: UsualWeek | None = None
            if raw is None:
                reason = f"larger than {MAX_FILE_BYTES} bytes"
            else:
                try:
                    week = parse_usual_week(raw)
                except InvalidWeek as e:
                    reason = str(e)
            if week is None:
                logger.warning(f"Rejected {key}: {reason}")
                self._rejected[key] = modified
                result.rejected.append(key)
                return
            cells = base64.b64decode(encode_cells(week.cells))
            if row is None:
                row = PlayerAvailability(user_id=user_id)
                session.add(row)
            row.zone = week.zone
            row.cells = cells
            row.version = _STORED_VERSION
            row.object_modified_utc = object_modified
            row.stored_utc = _naive_utc(now_utc)
        self._rejected.pop(key, None)
        result.stored.append(user_id)
        logger.info(f"Stored usual week for {user_id}")

