"""Debt warning marker for staked-draft sign-ups, and the (extracted, pure)
staked Sign-Ups field formatter that applies it.

The formatter is the render-time-only decoration point: stored sign_ups names
are never modified (see PR #349 — decorating stored names broke seating)."""
from loguru import logger


def debt_warning_suffix(total_owed, threshold) -> str:
    """" ⚠️ owes 75 tix" when total_owed >= threshold; "" otherwise.
    A falsy threshold disables warnings entirely."""
    if not threshold or not total_owed or total_owed < threshold:
        return ""
    return f" ⚠️ owes {total_owed} tix"


def format_staked_sign_ups(sign_ups, stake_info_by_player, owed_map, threshold,
                           display_name_for, session_id: str = "") -> str:
    """The staked draft message's Sign-Ups field text: one line per player with
    stake, cap emoji, display name, and (over-threshold players only) the debt
    warning suffix. Identical to the historical format when owed_map is empty."""
    sign_ups_list = []
    for user_id, stored_name in sign_ups.items():
        display_name = display_name_for(user_id, stored_name)
        if user_id in stake_info_by_player:
            stake_amount = stake_info_by_player[user_id]["amount"]
            is_capped = stake_info_by_player[user_id]["is_capped"]
            capped_emoji = "🧢" if is_capped else "🏎️"
            sign_ups_list.append((user_id, display_name, stake_amount, is_capped, capped_emoji))
        else:
            sign_ups_list.append((user_id, display_name, "Not set", True, "❓"))

    def sort_key(item):
        stake = item[2]
        return -1 if stake == "Not set" else stake

    sign_ups_list.sort(key=sort_key, reverse=True)

    formatted = []
    for user_id, display_name, stake_amount, _is_capped, emoji in sign_ups_list:
        suffix = debt_warning_suffix(owed_map.get(user_id), threshold)
        if stake_amount == "Not set":
            formatted.append(f"❌ Not set: {display_name}{suffix}")
        else:
            formatted.append(f"{emoji} {stake_amount} tix: {display_name}{suffix}")

    result = (f"**Players ({len(sign_ups)}):**\n"
              + ("\n".join(formatted) if formatted else "No players yet."))
    if len(result) > 1000:
        logger.warning(
            f"[debt-warning] Sign-Ups field for {session_id or 'unknown session'} "
            f"exceeds single-field limit ({len(result)} chars); continuation-chunk "
            "splitting may drop content"
        )
    return result
