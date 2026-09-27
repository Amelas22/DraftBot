"""Telling a player that part of their entry came back.

The prize-pool copy promises, in four places, that unmatched money "comes
straight back", "is returned before the draft starts", "is returned
immediately". Until this, nothing said it had: draft_pool_service logged each
refund and moved on, so a player who declared 300 and was levelled to 100
watched 200 reappear with no message and nothing anywhere attributing it. A
promise the person it was made to cannot verify is worse than no promise.

Composition lives here rather than in draft_pool_service because that module
moves money and must not grow a Discord dependency, and in its own module rather
than in notification_service because the message needs BOTH refund reasons and
the resulting holding to say anything useful -- which is a fact about a draft
pool, not about a wallet.
"""
from typing import Any, Mapping, Optional

from loguru import logger


def refund_message(returned: int, held: int, capped: int,
                   friendly_id: Optional[str] = None) -> str:
    """What came back, why, and what they are actually playing for.

    The holding is the part that matters and the part the player cannot work
    out for themselves: "200 tix came back" invites the question this answers.

    A cap is named when one applied, because that is the player's OWN setting
    and the one thing here they chose -- levelling is not, and blaming it on a
    cap they did not set would send them looking for a preference to change.

    Both ceilings can bite ONE entry -- capping runs first, then levelling -- and
    when they do, each share is named. Reporting the combined total under the
    cap's name alone is not merely imprecise, it is advice that does not work:
    side B {400 capped, 200} against {100, 100} has the cap return 160 and
    levelling return a further 140, but with the cap OFF that 400 still levels
    to exactly 100 -- so the preference the message points at would have
    returned none of it. A single reason stays a single sentence; the split only
    earns its words when both actually applied.
    """
    which = f" in **{friendly_id}**" if friendly_id else ""
    levelled = max(returned - capped, 0)
    if capped and levelled:
        why = (f"**{capped}** to your entry cap and **{levelled}** to levelling "
               "your side down to match the other side")
    elif capped:
        why = "your entry cap trimmed it to your share of your team"
    else:
        why = "your side was levelled down to match the other side"
    return (f"↩️ **{returned} tix** came back from your entry{which} — {why}.\n"
            f"You are playing for **{held} tix**.")


async def announce_refunds(session_id: Any, *,
                           refunded: Mapping[str, int],
                           capped: Mapping[str, int],
                           held: Mapping[str, int],
                           friendly_id: Optional[str] = None) -> int:
    """DM everyone who got something back. Returns how many were told.

    One message per player, not per reason: capping runs before levelling, so a
    single entry can be trimmed twice, and two DMs about one draft reads as a
    bug rather than as detail.

    Best-effort by contract -- this runs immediately after the money moved, and a
    borrower with closed DMs must not turn a settled pool into an error.
    """
    import notification_service
    from bot_registry import get_bot

    bot = get_bot()
    if bot is None:      # no bot (tests, migrations, CLI): nobody to tell
        return 0

    told = 0
    for player_id in dict.fromkeys([*refunded, *capped]):
        by_cap = int(capped.get(player_id, 0) or 0)
        total = int(refunded.get(player_id, 0) or 0) + by_cap
        if total <= 0:
            continue
        # No try/except, for the reason library_reminders._dm documents: send_dm
        # catches Forbidden, HTTPException and anything else itself and returns
        # False. A guard here would catch nothing and would state a contract
        # opposite to the one that module states in writing.
        sent = await notification_service.send_dm(
            bot, player_id,
            refund_message(total, int(held.get(player_id, 0) or 0), by_cap,
                           friendly_id),
            label=f"entry refund for {player_id}")
        told += bool(sent)
    if told:
        logger.info("entry notices: told {} player(s) what came back on {}",
                    told, session_id)
    return told
