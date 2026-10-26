"""Team-draft matchups: validate the two sides, create the row, let exactly one draft claim it.

Every function takes the caller's session; none commits. The caller commits.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.team_draft import TeamDraft
from services.availability import naive_utc

MAX_PICKS = 5
MAX_SIDE_PEOPLE = 12


@dataclass(frozen=True)
class Picks:
    """One side's choices: role ids and individual user ids."""

    role_ids: tuple[str, ...]
    user_ids: tuple[str, ...]

    def count(self) -> int:
        return len(self.role_ids) + len(self.user_ids)


def _people(
    picks: Picks,
    members: Callable[[str], list[str] | None],
    is_bot: Callable[[str], bool],
) -> set[str]:
    """Everyone a side names: role members plus named users, minus bots.

    A role Discord no longer has contributes nobody.
    """
    found: set[str] = set(picks.user_ids)
    for role_id in picks.role_ids:
        found.update(members(role_id) or [])
    return {user_id for user_id in found if not is_bot(user_id)}


def check_sides(
    a: Picks,
    b: Picks,
    members: Callable[[str], list[str] | None],
    guild_id: str,
    is_bot: Callable[[str], bool],
) -> str | None:
    """The reason these two sides can't be a matchup, or None when they can."""
    if a.count() == 0 or b.count() == 0:
        return "Each side needs at least one role or person."
    if guild_id in a.role_ids or guild_id in b.role_ids:
        return "@everyone can't be a side. Pick a smaller role or name people."
    if a.count() > MAX_PICKS or b.count() > MAX_PICKS:
        return f"A side can have at most {MAX_PICKS} roles and people."
    if set(a.role_ids) & set(b.role_ids) or set(a.user_ids) & set(b.user_ids):
        return "The same role or person is on both sides."
    people_a = _people(a, members, is_bot)
    people_b = _people(b, members, is_bot)
    if not people_a or not people_b:
        return "Each side needs at least one person; a deleted role or a bot doesn't count."
    both = people_a & people_b
    if both:
        noun = "person is" if len(both) == 1 else "people are"
        return f"{len(both)} {noun} on both sides. Each person can only play for one side."
    if len(people_a) > MAX_SIDE_PEOPLE or len(people_b) > MAX_SIDE_PEOPLE:
        return f"A side can have at most {MAX_SIDE_PEOPLE} people."
    return None


async def create_team_draft(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
    name: str,
    a: Picks,
    b: Picks,
    created_by: str,
    now: datetime,
) -> TeamDraft:
    """Add the matchup and flush so it has an id. The caller commits."""
    team_draft = TeamDraft(
        guild_id=guild_id,
        channel_id=channel_id,
        name=name,
        side_a_roles=list(a.role_ids),
        side_a_users=list(a.user_ids),
        side_b_roles=list(b.role_ids),
        side_b_users=list(b.user_ids),
        created_by=created_by,
        created_utc=naive_utc(now),
    )
    session.add(team_draft)
    await session.flush()
    return team_draft


async def delete_team_draft(session: AsyncSession, team_draft_id: int) -> None:
    """Remove a matchup whose thread never opened. The caller commits."""
    await session.execute(delete(TeamDraft).where(TeamDraft.id == team_draft_id))


async def set_room(
    session: AsyncSession, team_draft_id: int, thread_id: str, control_message_id: str
) -> None:
    """Record the matchup's thread and its control message. The caller commits."""
    await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id)
        .values(thread_id=thread_id, control_message_id=control_message_id)
    )


async def by_thread(session: AsyncSession, thread_id: str) -> TeamDraft | None:
    result = await session.execute(select(TeamDraft).where(TeamDraft.thread_id == thread_id))
    return result.scalars().first()


async def claim_draft(session: AsyncSession, team_draft_id: int, draft_session_id: str) -> bool:
    """Attach a draft to the matchup if none is attached yet; True when this call won.

    One UPDATE decides it, so concurrent claimers cannot both succeed. The
    caller commits (and rolls back or releases if the draft then fails).
    """
    result = await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id, TeamDraft.draft_session_id.is_(None))
        .values(draft_session_id=draft_session_id)
    )
    return isinstance(result, CursorResult) and result.rowcount == 1


async def release_draft(session: AsyncSession, team_draft_id: int, draft_session_id: str) -> None:
    """Free the matchup, but only if it still holds this draft. The caller commits."""
    await session.execute(
        update(TeamDraft)
        .where(TeamDraft.id == team_draft_id, TeamDraft.draft_session_id == draft_session_id)
        .values(draft_session_id=None)
    )
