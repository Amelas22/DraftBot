"""The rules for granting a substitute access to a draft's channels, and the
grant that applies them.

resolve_sub_grant decides which rooms a sub may see and which side they join;
grant_sub_access applies that decision as permission overwrites and announces
it in the draft chat. Both /add_sub and the library panel's Sub for a player
go through them.
"""
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import discord
from loguru import logger

from helpers.draft_rooms import SHARED_SIDE, side_by_key, side_by_prefix
from helpers.team_names import BLUE, RED

@dataclass
class GrantDecision:
    """Which channels a sub may see, and what to call the side they joined.

    `team_key` is the internal A/B selector -- the same vocabulary as the
    team_a/team_b columns and the /add_sub choice VALUES, none of which a
    player ever reads. Nothing in production reads it back; it is kept because
    it is the field that says which branch was taken, and the tests assert on
    it. `team_display_name` is the only one of the three that reaches a person,
    and it comes from team_labels so it agrees with every other surface.
    """
    team_key: Optional[str]            # "A"/"B", or None for a team-less draft
    channel_prefix: Optional[str]      # "Red-Team"/"Blue-Team", or None (draft chat only)
    team_display_name: str             # a premade team's own name, else the colour


def resolve_sub_grant(
    session,
    invoker_id: str,
    target_id: str,
    is_admin: bool,
    team_choice: Optional[str] = None,
) -> Tuple[Optional[GrantDecision], Optional[str]]:
    """Decide whether invoker may grant target sub access, and to which team.

    Players on a team always grant their own team (team_choice is ignored);
    admins outside the draft must pass team_choice "A" or "B".
    Returns (decision, error) — exactly one is None.
    """
    team_a = session.team_a or []
    team_b = session.team_b or []
    sign_ups = session.sign_ups or {}

    if target_id in team_a or target_id in team_b or target_id in sign_ups:
        return None, "That user is already a participant in this draft."

    if not team_a and not team_b:
        # Team-less draft (e.g. swiss): only the shared draft chat exists.
        if invoker_id in sign_ups or is_admin:
            return GrantDecision(None, None, SHARED_SIDE.label.name), None
        return None, "Only players in this draft (or bot managers) can add a sub."

    if invoker_id in team_a:
        team_key = "A"
    elif invoker_id in team_b:
        team_key = "B"
    elif is_admin:
        if team_choice not in ("A", "B"):
            return None, ("You're not on a team in this draft — pass the "
                          f"`team` option ({RED.name} or {BLUE.name}) to "
                          "choose the sub's team.")
        team_key = team_choice
    else:
        return None, "Only players in this draft (or bot managers) can add a sub."

    side = side_by_key(team_key)
    assert side is not None      # team_key is "A" or "B" on every path above
    return GrantDecision(side.key, side.prefix, side.named(session).name), None


def is_sub_target_channel(channel_name: str, friendly_id: str, channel_prefix: Optional[str]) -> bool:
    """True if channel_name is one of the channels a sub should be granted.

    A sub always gets the shared draft chat, plus their own side's rooms when
    they have a side. channel_prefix is None for team-less drafts (swiss),
    where the shared chat is the only room there is.

    Which rooms a side owns is draft_rooms' answer now, so the name pattern is
    written once rather than here and in three other modules.
    """
    targets = SHARED_SIDE.room_names(friendly_id)
    side = side_by_prefix(channel_prefix)
    if side is not None:
        targets |= side.room_names(friendly_id)
    return channel_name.lower() in targets


def channel_ids_contains(channel_ids, channel_id) -> bool:
    """Membership test tolerant of int-vs-str storage in the channel_ids JSON."""
    if not channel_ids:
        return False
    wanted = str(channel_id)
    return any(str(cid) == wanted for cid in channel_ids)


@dataclass
class SubGrant:
    granted: list[str]     # channel mentions granted
    failed: list[str]      # channel mentions Discord refused
    announced: bool        # the draft-chat announcement was sent
    draft_chat_failed: bool = False   # Discord refused the draft chat itself


async def grant_sub_access(guild: Any, draft: Any, user: Any, decision: GrantDecision,
                           *, added_by: str, note: str = "") -> SubGrant:
    """Give `user` the draft's shared and own-team rooms, then announce it in the draft chat."""
    draft_chat_id = str(draft.draft_chat_channel)
    granted: list[str] = []
    failed: list[str] = []
    draft_chat_failed = False
    draft_chat: Any = None
    for channel_id in draft.channel_ids:
        channel = guild.get_channel(int(channel_id))
        if not channel or not is_sub_target_channel(
                channel.name, draft.friendly_id, decision.channel_prefix):
            continue
        try:
            # Same overwrites teammates get at channel creation
            await channel.set_permissions(
                user, read_messages=True, manage_messages=True)
        except discord.HTTPException:
            logger.exception(f"add_sub: failed to set permissions on "
                             f"channel {channel.id}")
            failed.append(channel.mention)
            draft_chat_failed = draft_chat_failed or str(channel.id) == draft_chat_id
            continue
        granted.append(channel.mention)
        if str(channel.id) == draft_chat_id:
            draft_chat = channel

    announced = False
    if draft_chat is not None:
        text = (f"{user.mention} was added as a substitute for "
                f"**{decision.team_display_name}** by {added_by}.")
        if note:
            text += f" {note}"
        try:
            await draft_chat.send(text)
            announced = True
        except discord.HTTPException:
            logger.warning(
                f"add_sub: grant succeeded but the public announcement "
                f"failed in channel {draft_chat.id}", exc_info=True)
    return SubGrant(granted, failed, announced, draft_chat_failed)
