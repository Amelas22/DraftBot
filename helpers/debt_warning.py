"""Debt warning marker for staked-draft sign-ups, and the (extracted, pure)
staked Sign-Ups field formatter that applies it.

The formatter is the render-time-only decoration point: stored sign_ups names
are never modified (see PR #349 — decorating stored names broke seating)."""
from loguru import logger


# Days a debt must remain outstanding before it counts toward the warning
# threshold. Code constant on purpose — the threshold is per-guild config,
# the age window is not (YAGNI until someone asks).
DEBT_WARNING_AGE_DAYS = 7


def debt_warning_suffix(total_owed, old_owed, threshold) -> str:
    """The trailing marker (e.g. " ⚠️ owes 150 tix") when the player's
    week-old outstanding debt strictly exceeds threshold. Displays the total
    outstanding amount; triggers on the aged portion only. A falsy threshold
    disables warnings entirely."""
    if not threshold or not old_owed or old_owed <= threshold:
        return ""
    return f" ⚠️ owes {total_owed} tix"


def format_staked_sign_ups(sign_ups, stake_info_by_player, owed_map, old_owed_map,
                           threshold, display_name_for, session_id: str = "",
                           pool: int = 0) -> str:
    """The staked draft message's Sign-Ups field text: the players in JOIN
    order, the pool they are collectively playing for, and (players whose
    week-old debt exceeds threshold only) the debt warning suffix.

    It used to print every player's own bet, sorted by size. That made signing
    up a leaderboard: the largest bet sat at the top of every draft, and each
    arrival could read what everyone else was in for before choosing their own.
    The number that actually matters to the table is the pool, and a winner
    takes double their matched stake -- so the most it can ever be worth is
    what everyone has put in. The per-player figures are still shown once teams
    form, where they are final and describe money already committed.

    The cap emoji went with them: bet capping was read only by the tiered
    matcher, which no longer runs.
    """
    lines = []
    for user_id, stored_name in sign_ups.items():
        display_name = display_name_for(user_id, stored_name)
        suffix = debt_warning_suffix(owed_map.get(user_id), old_owed_map.get(user_id), threshold)
        if user_id in stake_info_by_player:
            lines.append(f"{display_name}{suffix}")
        else:
            lines.append(f"❌ {display_name} has not set a bet{suffix}")

    header = (f"**Players ({len(sign_ups)})** — prize pool: up to {pool} tix"
              if pool else f"**Players ({len(sign_ups)}):**")
    result = header + "\n" + ("\n".join(lines) if lines else "No players yet.")
    if len(result) > 1000:
        logger.warning(
            f"[debt-warning] Sign-Ups field for {session_id or 'unknown session'} "
            f"exceeds single-field limit ({len(result)} chars); continuation-chunk "
            "splitting may drop content"
        )
    return result
