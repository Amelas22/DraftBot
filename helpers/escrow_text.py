"""How an escrow reads in a list, shared by the player commands and the moderator panel."""
import discord

from helpers.display_names import plain_text
from models.escrow_hold import EscrowHold

# Every escrow reply names the parties with mentions, which render as names only while
# the ping is suppressed.
NO_MENTIONS = discord.AllowedMentions.none()


def describe_escrow(e: EscrowHold, *, age: bool = False) -> str:
    """One line for an escrow, for lists the players and the moderators both read.

    The note is typed by a player and read by others, so it is stripped of markdown and
    of anything that would mention, and folded onto one line so a long one cannot push
    the rest of a list off the page; the two parties are shown as mentions, which render
    as names because every send suppresses the ping. `age` swaps the status, which is
    always "open" on the moderator's list, for how long the escrow has waited.
    """
    note = plain_text(str(e.note))
    # created_at is a naive datetime.now(), so it is in the server's own zone, which is
    # what a naive timestamp() assumes.
    tail = f"<t:{int(e.created_at.timestamp())}:R>" if age else e.status
    return (f"#{e.id} · {e.amount} tix · <@{e.funder_id}> → <@{e.counterparty_id}> · "
            f"{tail} · {note}")
