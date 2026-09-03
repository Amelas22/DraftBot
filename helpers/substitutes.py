"""Pure decision logic for granting a substitute access to draft channels.

No Discord objects in here — the DraftCommands cog resolves members/channels
and applies the permission overwrites this module decides on.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

from helpers.team_names import BLUE, RED, team_labels

# Team channels are created with these hardcoded prefixes (views.py):
# team_a -> "Red-Team-Chat-{friendly_id}", team_b -> "Blue-Team-Chat-{friendly_id}".
TEAM_A_CHANNEL_PREFIX = "Red-Team"
TEAM_B_CHANNEL_PREFIX = "Blue-Team"


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
            return GrantDecision(None, None, "this draft"), None
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

    red, blue = team_labels(session.team_a_name, session.team_b_name)
    if team_key == "A":
        return GrantDecision("A", TEAM_A_CHANNEL_PREFIX, red.name), None
    return GrantDecision("B", TEAM_B_CHANNEL_PREFIX, blue.name), None


def is_sub_target_channel(channel_name: str, friendly_id: str, channel_prefix: Optional[str]) -> bool:
    """True if channel_name is one of the channels a sub should be granted.

    Case-insensitive: Discord lowercases text channel names on creation,
    while voice channels keep their original casing.

    channel_prefix is None for team-less drafts, where only the shared
    draft chat exists.
    """
    targets = {f"draft-chat-{friendly_id}".lower()}
    if channel_prefix:
        targets.add(f"{channel_prefix}-chat-{friendly_id}".lower())
        targets.add(f"{channel_prefix}-voice-{friendly_id}".lower())
    return channel_name.lower() in targets


def channel_ids_contains(channel_ids, channel_id) -> bool:
    """Membership test tolerant of int-vs-str storage in the channel_ids JSON."""
    if not channel_ids:
        return False
    wanted = str(channel_id)
    return any(str(cid) == wanted for cid in channel_ids)
