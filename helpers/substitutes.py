"""Pure decision logic for granting a substitute access to draft channels.

No Discord objects in here — the DraftCommands cog resolves members/channels
and applies the permission overwrites this module decides on.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

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
