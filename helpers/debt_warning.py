"""Debt warning marker for staked-draft sign-ups, and the (extracted, pure)
staked Sign-Ups field formatter that applies it.

The formatter is the render-time-only decoration point: stored sign_ups names
are never modified (see PR #349 — decorating stored names broke seating)."""
from loguru import logger


def debt_warning_suffix(total_owed, threshold) -> str:
    """The trailing marker (e.g. " ⚠️ owes 75 tix") when total_owed reaches
    threshold, else "". A falsy threshold disables warnings entirely."""
    if not threshold or not total_owed or total_owed < threshold:
        return ""
    return f" ⚠️ owes {total_owed} tix"


def format_staked_sign_ups(sign_ups, stake_info_by_player, owed_map, threshold,
                           display_name_for, session_id: str = "") -> str:
    """The staked draft message's Sign-Ups field text: one line per player with
    stake, cap emoji, display name, and (over-threshold players only) the debt
    warning suffix. Identical to the historical format when owed_map is empty."""
    entries = []
    for user_id, stored_name in sign_ups.items():
        display_name = display_name_for(user_id, stored_name)
        if user_id in stake_info_by_player:
            stake = stake_info_by_player[user_id]
            emoji = "🧢" if stake["is_capped"] else "🏎️"
            entries.append((user_id, display_name, stake["amount"], emoji))
        else:
            entries.append((user_id, display_name, "Not set", "❓"))

    def sort_key(entry):
        stake_amount = entry[2]
        return -1 if stake_amount == "Not set" else stake_amount

    entries.sort(key=sort_key, reverse=True)

    lines = []
    for user_id, display_name, stake_amount, emoji in entries:
        suffix = debt_warning_suffix(owed_map.get(user_id), threshold)
        if stake_amount == "Not set":
            lines.append(f"❌ Not set: {display_name}{suffix}")
        else:
            lines.append(f"{emoji} {stake_amount} tix: {display_name}{suffix}")

    result = (f"**Players ({len(sign_ups)}):**\n"
              + ("\n".join(lines) if lines else "No players yet."))
    if len(result) > 1000:
        logger.warning(
            f"[debt-warning] Sign-Ups field for {session_id or 'unknown session'} "
            f"exceeds single-field limit ({len(result)} chars); continuation-chunk "
            "splitting may drop content"
        )
    return result
