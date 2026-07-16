"""Shared, color-coded team labels for draft embeds.

One source of truth so every surface (queue signup fields, the teams/seating
embed, the live-draft board) agrees on which color is which team — the same
mapping add_sub's Red/Blue option and the Red-Team/Blue-Team channels use:

    team_a -> 🔴 Red    team_b -> 🔵 Blue
"""
from typing import Optional, Tuple

TEAM_A_COLOR = "🔴"
TEAM_B_COLOR = "🔵"

# Session types whose players pick their own named teams. Everything else
# (random, staked, swiss, winston, test, and any unknown/None type) has no
# meaningful team names and gets the generic color labels below. This is a
# whitelist on purpose: team-less types like swiss still reach the live-draft
# embed, and must not be labeled with a bogus "Team A"/"Team B".
NAMED_TEAM_SESSION_TYPES = ("premade",)


def team_labels(
    session_type: Optional[str],
    team_a_name: Optional[str],
    team_b_name: Optional[str],
) -> Tuple[str, str]:
    """Return ``(team_a_label, team_b_label)`` for embed field headers.

    Named-team drafts (premade/league) show the team's actual name, color-coded
    so the color is an unambiguous key. All other drafts get the generic
    ``🔴 Team Red`` / ``🔵 Team Blue``.
    """
    if session_type in NAMED_TEAM_SESSION_TYPES:
        return (f"{TEAM_A_COLOR} {team_a_name or 'Team A'}",
                f"{TEAM_B_COLOR} {team_b_name or 'Team B'}")
    return f"{TEAM_A_COLOR} Team Red", f"{TEAM_B_COLOR} Team Blue"
