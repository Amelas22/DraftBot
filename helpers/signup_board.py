"""The staked draft's signup board: one field, one row per player.

Each row is the entry amount inside an inline code span -- left-justified but
padded to a common width, which is what holds the column -- then crowns and the
name outside it. Three properties fall out of that shape, and
each one was learned the hard way:

* The span aligns the amounts. Equal character counts render equal widths in
  monospace, so every name starts at the same x -- while the name itself stays
  outside the span where markdown and CUSTOM emoji still render. A fenced
  block would align too, but it would print the double- and triple-crown
  emoji as raw `<:doublecrown:...>` shortcodes.
* Being ONE field is what survives mobile. Two inline fields render side by
  side on desktop and STACK on a phone, which turns a table into a list of
  names followed by an unpaired list of amounts.
* The numbers lead. Anything whose rendered width cannot be known here -- an
  emoji, a name long enough to wrap -- has to come last, so its drift never
  reaches the column. A wrapped name then costs only its own row.

Same reasoning as services/tournament_formatter._standings_rows, which solved
this first for tournament standings.
"""
from typing import Any, Callable, Mapping

from loguru import logger

# What views.py passes for display_name_for: (user_id, stored_name) -> the name
# as it should read, crowns and gem already prefixed, markdown escaped. The
# escaping matters here beyond the usual reason: an unescaped backtick in a
# name would close the row's code span.
DisplayNameFor = Callable[[str, str], str]
# The per-player stake dict views.py builds from a StakeInfo row.
StakeInfoMap = Mapping[str, Mapping[str, Any]]

# At or above this, an entry is shown as "100+" rather than its own figure. It
# is the point past which an exact number stops informing anybody's choice and
# starts inviting comparison: everything below is a step somebody might match,
# everything above is simply more than the table is likely to cover.
STAKE_SHOWN_CEILING = 100

# What an unset entry reads as. A dash and not a ❌: the cell lives inside the
# span, where an emoji's width is unknowable and one wide cell would shift its
# whole row. The dashes line up in the column, which makes the players who
# still need to choose easier to pick out than the old inline ❌ did, not
# harder.
NO_ENTRY = "-"

# Discord rejects a field value over 1024 characters, and views.py splits a
# longer board across "Sign-Ups: (cont. N)" fields -- except its update_field
# refuses to CREATE a field, so the continuation chunks are dropped rather than
# written (measured: 986 of 1259 characters survived). That refusal is wanted
# elsewhere, which is why this is a warning here and not a fix here. A board
# needs roughly 25 players to get near it, so nothing reaches it today; the
# warning exists so that if something ever does, it says so instead of quietly
# shortening the queue.
FIELD_SPLIT_THRESHOLD = 1000


def shown_stake(amount: Any) -> str:
    """What an entry looks like on the signup board, before teams exist.

    Bucketed at the top so the largest entries cannot be ranked against one
    another. What the bucket withholds is real, and became MORE real when the
    entry cap moved to reading a player's own side
    (draft_pool_service.cap_targets): anybody in this queue may be drawn as your
    teammate, and their declared figures are what your own ceiling is computed
    from, so a neighbour shown as "100+" could be 100 or 400 and you cannot tell
    which. That is the price of not ranking, and it is charged here knowingly.
    """
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return "?"
    return f"{STAKE_SHOWN_CEILING}+" if n >= STAKE_SHOWN_CEILING else str(n)


def entry_cap_phrase(is_capped: Any) -> str:
    """How a player's cap setting reads, in the one place the wording lives.

    Three confirmation surfaces state this -- the signup confirmation, the
    over-100 modal's result and the cap toggle -- and they stated it three ways,
    one of which dropped the clause explaining what "uncapped" is relative to.
    Same reasoning money_gate.wallet_howto gives for itself: written once
    because the copies drift, and a reviewer had to find the drift last time.

    It deliberately does NOT quote the percentage. CAP_SHARE is a tunable
    justified from history in draft_pool_service, so a confirmation line naming
    55% would need rewriting every time it moved; the figure belongs in the
    explainer panel, which has the room to say what it is measured against.
    """
    return ("capped to your share of your team" if is_capped else
            "uncapped, whatever your team looks like")


def _weighted(name: str) -> str:
    """The name, bold, unless bolding it would break the row.

    escape_markdown defaults to ignore_links=True, so a URL-shaped display name
    keeps its markdown: "https://x.y/**oops" arrives still carrying ** and
    would close this row's bold early, and a name ending in a backslash would
    escape the closing delimiter outright. Neither failure stops at its own row
    -- an unclosed ** bleeds into every row beneath it -- so a name that cannot
    be wrapped safely is left plain. Fixing the escaping itself belongs in
    helpers/display_names, where every other surface would benefit too.
    """
    if "**" in name or name.endswith("\\"):
        return name
    return f"**{name}**"


def signup_header(count: int, pool: int = 0) -> str:
    """The line above the rows: how many are in, and what they play for.

    An empty queue reports itself in words rather than counting to zero --
    "**0 players**" above nothing reads like a failed render.
    """
    if not count:
        return "No players yet."
    players = "player" if count == 1 else "players"
    header = f"**{count} {players}**"
    return f"{header} · prize pool: up to {pool} tix" if pool else header


def signup_rows(sign_ups: Mapping[str, str], stake_info_by_player: StakeInfoMap,
                display_name_for: DisplayNameFor) -> list[str]:
    """One row per player, in join order, amounts aligned to a common width.

    Entries are shown, but only up to STAKE_SHOWN_CEILING; at or above it every
    entry reads "100+". Two of the three things that made this a leaderboard
    when it printed exact figures sorted by size are gone: join order means the
    largest no longer sits at the top of every draft, and a shared bucket means
    the big entries cannot be ranked against each other at all -- somebody in
    for 300 and somebody in for 50 are indistinguishable here.

    The third, later arrivals reading the room before choosing, is now the
    POINT rather than the cost. Unmatched money is returned, so a table that
    converges on one figure is a table where everybody plays for what they
    meant to; seeing roughly where the pod is, is how somebody lands on a
    number that will actually be matched.

    Exact figures return once teams form, where they are final and describe
    money already committed rather than an invitation to anyone still choosing.
    Nothing here is coloured or ordered by size -- a size band would hand back
    the ranking the bucket exists to withhold.

    The width is measured from the entries actually shown, not fixed, so a
    small-stakes pod does not carry four characters of padding and a "100+"
    widens the column instead of knocking every row below it out of true.
    Amounts sit left-justified in the span but are still PADDED to that width:
    the justification is taste, the padding is what holds the column.

    The name is bold -- the same weight _standings_rows gives a team name, and
    the row's only decoration. It wraps the whole value display_name_for
    returns, crowns included, because bold does nothing to an emoji and
    splitting the icons back off the name would need an accessor that exists
    only to serve a formatting detail.
    """
    cells = {user_id: _entry_cell(stake_info_by_player.get(user_id))
             for user_id in sign_ups}
    width = max((len(cell) for cell in cells.values()), default=0)
    return [
        f"`{cells[user_id]:<{width}}` "
        + _weighted(display_name_for(user_id, stored_name))
        for user_id, stored_name in sign_ups.items()
    ]


def build_board(sign_ups: Mapping[str, str], stake_info_by_player: StakeInfoMap,
                display_name_for: DisplayNameFor, pool: int = 0) -> str:
    """The whole Sign-Ups field value: header, then a row per player.

    Returns text rather than touching the embed, so the field it goes into is
    still written by views.py's update_field -- which refuses to CREATE a field
    that is missing. That refusal is load-bearing: after teams form, the queue
    embed is replaced by the "Draft is Ready!" one, and a player still holding
    the 5-minute ephemeral stake view can submit an entry after that. Writing
    the board unconditionally would re-list the whole queue onto the ready
    message.
    """
    rows = signup_rows(sign_ups, stake_info_by_player, display_name_for)
    header = signup_header(len(sign_ups), pool)
    board = "\n".join([header, *rows])
    if len(board) > FIELD_SPLIT_THRESHOLD:
        logger.warning(
            f"[signup-board] {len(sign_ups)} players render to {len(board)} "
            f"chars, over the {FIELD_SPLIT_THRESHOLD}-char field split "
            "threshold; the continuation chunks will be dropped and the queue "
            "will show short")
    return board


def _entry_cell(stake: Mapping[str, Any] | None) -> str:
    """The span's contents for one player, before padding."""
    return NO_ENTRY if stake is None else shown_stake(stake.get("amount"))
