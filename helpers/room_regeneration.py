"""Rebuild one team's draft rooms from scratch.

Exists for a Discord bug (discord-api-docs#6573): permission overwrites are
accepted and stored but never take effect, so the room stays invisible on every
device -- desktop, mobile, web, even a direct jump link -- while the REST API
keeps reporting each overwrite as present and correct. Nothing readable through
the API distinguishes the broken state from a healthy one, so it cannot be
detected, only repaired.

It can take a WHOLE ROOM, not one player. The issue is reported against
overwrites on a just-created channel (~5-7% of creations), and that is the
payload admitting every original member -- so a room can be born that nobody on
its team can see. A substitute granted access later is a second way in, not the
only one. Observed on reckless-crew-92: of three rooms made in one run, the one
that took 7s to create (against 1s and 2s for its siblings, consistent with a
rate-limited retry) was the one nobody could see. Its whole team was locked out,
and the sub added 47 minutes later inherited the same dead room.

The only repair that works is to delete the channel and make it again, which
forces a fresh CHANNEL_CREATE to everyone who should see it. Re-applying
overwrites on the existing channel does NOT clear it -- tried in production,
including a full delete-then-re-PUT of a member overwrite.
"""

from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import update

from helpers.draft_rooms import (SHARED_CHAT_TEAM, SHARED_SIDE, side_by_prefix,
                                 team_channel_name)
from helpers.substitutes import channel_ids_contains
from session import AsyncSessionLocal, DraftSession, get_draft_session


def select_team_rooms(channels: Iterable[Any], team_name: str,
                      friendly_id: str) -> list[Any]:
    """The channels among `channels` that are this team's rooms, in the order given.

    Matched on the deterministic room name rather than on position in
    channel_ids, because that list records every room of the draft with nothing
    to say which team each belongs to.

    The names come from the Side row, which is the same table the sub grant and
    the log store ask. This used to build them here from the same two helpers,
    which is one copy too many for a rule whose failure is silent: nothing
    matches, and the admin is told the team has no rooms -- indistinguishable
    from the broken state /regenerate_rooms exists to repair.
    """
    side = side_by_prefix(team_name) or SHARED_SIDE
    wanted = side.room_names(friendly_id)
    return [c for c in channels if c.name.lower() in wanted]


def carried_over_member_ids(channel: Any) -> list[Any]:
    """The ids with an explicit overwrite on `channel`, whoever they belong to.

    A SUBSTITUTE is never written into team_a/team_b -- resolve_sub_grant
    refuses anyone already there, and /add_sub only calls set_permissions -- so
    the session row does not know they exist. The room's own overwrites are the
    only record that they had access, so they are read off the doomed channel
    before it goes; rebuilding from the roster alone would silently drop them.

    Roles land in here too; the caller filters them out by asking the guild
    whether each id is a member.
    """
    return [getattr(target, "id", None)
            for target in (getattr(channel, "overwrites", None) or {})]


def remaining_channel_ids(channel_ids: Iterable[Any] | None,
                          removed_ids: Iterable[Any] | None) -> list[Any]:
    """`channel_ids` without `removed_ids`, each surviving id left as it was stored.

    This is the load-bearing step of a regenerate: the draft must STOP
    recognising the deleted room before a new one is made, because
    create_team_channel reuses any channel whose id it still owns
    (views.py -> existing_channel) and a stale id is indistinguishable from a
    healthy one. Miss it and the repair hands back the very channel it set out
    to replace.
    """
    return [cid for cid in (channel_ids or [])
            if not channel_ids_contains(removed_ids, cid)]


@dataclass(frozen=True)
class TeamPlan:
    """What regenerating one team's rooms needs to know about that team."""
    member_ids: list[str]
    pools_field: str   # the DraftSession column holding this room's pools thread


def team_plan(session: Any, team_name: str) -> TeamPlan:
    """The roster whose rooms are being rebuilt, and the pools destination that
    dies with them.

    The pools thread hangs off the room being deleted, so it dies with it.
    Naming the column here lets the caller clear exactly that one, which is what
    makes post_team_logs open a fresh thread for this room while leaving the
    others -- still alive, still holding their pools -- untouched.

    The shared chat's roster is every player. team_a/team_b are empty for a
    team-less draft (swiss), whose only room is the shared chat and whose roster
    lives in sign_ups -- exactly as create_rooms_pairings builds it. Falling back
    to team_a + team_b there would rebuild the room with NO members and lock the
    whole draft out of the only channel it has.
    """
    side = side_by_prefix(team_name) or SHARED_SIDE
    return TeamPlan(side.roster(session), side.pools_column)


@dataclass(frozen=True)
class RegenerationResult:
    """What a regenerate did, so the caller can report it rather than guess."""
    deleted_ids: list[Any]
    new_channel_id: int | None
    pools_reposted: bool


async def _persist(session_id: str, **values: Any) -> None:
    """Write `values` onto this draft's row and commit."""
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            await db_session.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id)
                .values(**values)
            )


async def regenerate_team_rooms(
    bot: Any, guild: Any, session_id: str, team_name: str
) -> tuple[RegenerationResult | None, str | None]:
    """Delete one team's rooms and make them again.

    Returns `(result, error)` -- exactly one is set, following
    substitutes.resolve_sub_grant. The two failures are told apart on purpose:
    "this team has no rooms" and "a room would not delete" want opposite
    reactions from an admin, and only the second has already destroyed something.

    Everything here except the deletion is machinery that already exists:
    create_team_channel remakes the room -- overwrites in the creation payload,
    voice channel, scouting threads -- and post_team_logs reopens the pools
    thread. What this adds is making the draft forget the old room first, in
    that order.
    """
    session = await get_draft_session(session_id)
    if session is None:
        return None, f"No draft session `{session_id}`."

    friendly_id = session.friendly_id
    plan = team_plan(session, team_name)
    resolved = [c for c in (guild.get_channel(int(cid)) for cid in (session.channel_ids or []))
                if c is not None]
    stale = select_team_rooms(resolved, team_name, friendly_id)
    if not stale:
        return None, (
            f"**{team_name}** has no rooms recorded for `{friendly_id}` — they may "
            f"already be gone, or this draft has no such team.")

    # Where this draft's rooms actually live, which is not necessarily the
    # configured category: a busy guild overflows into a numbered sibling, and a
    # repair whose whole premise is "a player cannot find their room" must not
    # rehome the room away from its own siblings.
    #
    # Read off the TEXT room specifically. stale follows channel_ids order, and
    # a voice channel sits in the guild's VOICE category -- probing whichever
    # sorted first would hand the rebuilt text channel a voice category and
    # cause the very rehoming this avoids.
    chat_name = team_channel_name(team_name, friendly_id).lower()
    chat = next((c for c in stale if c.name.lower() == chat_name), stale[0])
    category = getattr(chat, "category", None)
    # Read before the delete: afterwards the overwrites are gone with the room.
    carried_ids = carried_over_member_ids(chat)

    deleted_ids: list[Any] = []
    for channel in stale:
        try:
            await channel.delete(
                reason=f"regenerating {team_name} rooms for {friendly_id}")
        except Exception as e:
            # A room that will not go is worse than one that never existed: the
            # new one lands beside it sharing its name. Stop before creating
            # anything -- but record whatever DID get deleted, so the row stops
            # pointing at rooms that are gone.
            logger.warning(
                f"[regenerate] could not delete channel {channel.id} for "
                f"{friendly_id}: {e}")
            if deleted_ids:
                await _persist(session_id, channel_ids=remaining_channel_ids(
                    session.channel_ids, deleted_ids))
            return None, (
                f"Deleted {len(deleted_ids)} of {len(stale)} room(s), then could not "
                f"delete `{channel.id}`: {e}. Nothing was recreated — the deleted "
                f"rooms are gone and this needs another run once Discord lets the "
                f"rest go.")
        deleted_ids.append(channel.id)

    # Forget them BEFORE creating -- see remaining_channel_ids.
    values: dict[str, Any] = {
        "channel_ids": remaining_channel_ids(session.channel_ids, deleted_ids)}
    # The pools thread died with the room. Clearing the stamp as well is what
    # lets post_team_logs run again; it resumes per destination, so the rooms
    # that survived are scanned, found complete, and left alone.
    values[plan.pools_field] = None
    values["team_logs_posted_at"] = None
    await _persist(session_id, **values)

    from views import PersistentView

    # The roster, plus anyone the old room had granted directly -- see
    # carried_over_member_ids. get_member answers None for the role ids that
    # come back in the same list, which is how they are filtered out.
    wanted_ids = list(dict.fromkeys(list(plan.member_ids) + carried_ids))
    members = [m for m in (guild.get_member(int(uid)) for uid in wanted_ids
                           if uid is not None)
               if m is not None]

    view = PersistentView(bot, session_id, session.session_type)
    # PersistentView.__init__ never sets this, and create_team_channel writes it
    # to the row unconditionally -- it is only assigned when the "Draft" room is
    # the one being made. Rebuilding a TEAM room on a fresh view would raise
    # AttributeError with the old rooms already deleted, so seed it with what the
    # draft already has.
    view.draft_chat_channel = session.draft_chat_channel

    try:
        new_channel_id = await view.create_team_channel(
            guild, team_name, members, session.team_a, session.team_b,
            rooms_category=category)
    except Exception as e:
        # Past the destructive step. ensure_channel re-raises anything that is
        # not a full category, and letting that reach the slash command would
        # show the admin a generic interaction failure with no hint that the
        # rooms are gone.
        logger.exception(f"[regenerate] {friendly_id} {team_name}: rebuild failed")
        return None, (
            f"Deleted {len(deleted_ids)} room(s) for **{team_name}**, then failed to "
            f"recreate them: {e}. The rooms are gone; run this again once the cause "
            f"is cleared.")
    if new_channel_id is None:
        return None, (
            f"Deleted {len(deleted_ids)} room(s) for **{team_name}**, but the "
            f"rebuild reported no channel. The rooms are gone; see the logs.")

    # create_team_channel is the SETUP path and stamps 'pairings' unconditionally
    # (views.py). This command repairs drafts that are in play or already
    # finished, and rewinding a completed draft's stage puts it back in front of
    # the live-draft re-register and the log reconciler.
    if session.session_stage and session.session_stage != "pairings":
        await _persist(session_id, session_stage=session.session_stage)

    from services.draft_log_store import post_team_logs
    pools_reposted = await post_team_logs(session_id, bot)

    if team_name == SHARED_CHAT_TEAM:
        # The pairings embeds and their result-report buttons live in the shared
        # chat, so rebuilding it takes the draft's whole reporting UI with it.
        # post_pairings is what the recover path uses to put them back.
        from utils import post_pairings
        try:
            await post_pairings(bot, guild, session_id)
        except Exception as e:
            logger.warning(
                f"[regenerate] {friendly_id}: rooms rebuilt but pairings could not "
                f"be reposted: {e}")

    logger.info(
        f"[regenerate] {friendly_id} {team_name}: replaced {deleted_ids} with "
        f"{new_channel_id} (pools reposted: {pools_reposted})")
    return RegenerationResult(deleted_ids, new_channel_id, pools_reposted), None
