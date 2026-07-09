"""Pure decision logic for granting a substitute access to draft channels.

No Discord objects in here — the DraftCommands cog resolves members/channels
and applies the permission overwrites this module decides on.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

# Team channels are created with these hardcoded prefixes (views.py):
# team_a -> "Red-Team-Chat-{draft_id}", team_b -> "Blue-Team-Chat-{draft_id}".
TEAM_A_CHANNEL_PREFIX = "Red-Team"
TEAM_B_CHANNEL_PREFIX = "Blue-Team"


@dataclass
class GrantDecision:
    team_key: str           # "A" or "B"
    channel_prefix: str     # "Red-Team" or "Blue-Team"
    team_display_name: str  # premade team name, or "Red Team"/"Blue Team"


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

    if invoker_id in team_a:
        team_key = "A"
    elif invoker_id in team_b:
        team_key = "B"
    elif is_admin:
        if team_choice not in ("A", "B"):
            return None, ("You're not on a team in this draft — pass the "
                          "`team` option (A or B) to choose the sub's team.")
        team_key = team_choice
    else:
        return None, "Only players in this draft (or bot managers) can add a sub."

    if team_key == "A":
        display = session.team_a_name or "Red Team"
        return GrantDecision("A", TEAM_A_CHANNEL_PREFIX, display), None
    display = session.team_b_name or "Blue Team"
    return GrantDecision("B", TEAM_B_CHANNEL_PREFIX, display), None


def is_sub_target_channel(channel_name: str, draft_id: str, channel_prefix: str) -> bool:
    """True if channel_name is one of the channels a sub should be granted.

    Case-insensitive: Discord lowercases text channel names on creation,
    while voice channels keep their original casing.
    """
    targets = {
        f"draft-chat-{draft_id}".lower(),
        f"{channel_prefix}-chat-{draft_id}".lower(),
        f"{channel_prefix}-voice-{draft_id}".lower(),
    }
    return channel_name.lower() in targets


def channel_ids_contains(channel_ids, channel_id) -> bool:
    """Membership test tolerant of int-vs-str storage in the channel_ids JSON."""
    if not channel_ids:
        return False
    wanted = str(channel_id)
    return any(str(cid) == wanted for cid in channel_ids)
