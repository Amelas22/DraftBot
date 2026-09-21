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


# At or above this, a bet is shown as "100+" rather than its own figure. It is
# the point past which an exact number stops informing anybody's choice and
# starts inviting comparison: everything below is a step somebody might match,
# everything above is simply more than the table is likely to cover.
STAKE_SHOWN_CEILING = 100


def shown_stake(amount) -> str:
    """What a bet looks like on the signup board, before teams exist.

    Bucketed at the top so the largest bets cannot be ranked against one
    another. It is not obfuscation -- a bet over the ceiling has no effect the
    exact figure would explain, because the matcher caps both sides at what
    the smaller one can cover and hands the rest straight back.
    """
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return "?"
    return f"{STAKE_SHOWN_CEILING}+" if n >= STAKE_SHOWN_CEILING else str(n)


def format_staked_sign_ups(sign_ups, stake_info_by_player, owed_map, old_owed_map,
                           threshold, display_name_for, session_id: str = "",
                           pool: int = 0) -> str:
    """The staked draft message's Sign-Ups field text: the players in JOIN
    order with what each has bet, the pool they are collectively playing for,
    and (players whose week-old debt exceeds threshold only) the debt warning.

    Bets are shown, but only up to STAKE_SHOWN_CEILING; at or above it every
    bet reads "100+". Two of the three things that made this a leaderboard when
    it printed exact figures sorted by size are gone: join order means the
    largest no longer sits at the top of every draft, and a shared bucket means
    the big bets cannot be ranked against each other at all -- somebody in for
    300 and somebody in for 50 are indistinguishable here.

    The third, later arrivals reading the room before choosing, is now the
    POINT rather than the cost. Unmatched money is returned, so a table that
    converges on one figure is a table where everybody plays for what they
    meant to; seeing roughly where the pod is, is how somebody lands on a
    number that will actually be matched.

    Exact figures return once teams form, where they are final and describe
    money already committed rather than an invitation to anyone still choosing.

    The cap emoji is not shown: bet capping was read only by the tiered
    matcher, which no longer runs.
    """
    lines = []
    for user_id, stored_name in sign_ups.items():
        display_name = display_name_for(user_id, stored_name)
        suffix = debt_warning_suffix(owed_map.get(user_id), old_owed_map.get(user_id), threshold)
        stake = stake_info_by_player.get(user_id)
        if stake is not None:
            lines.append(f"{display_name} {shown_stake(stake.get('amount'))}{suffix}")
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
