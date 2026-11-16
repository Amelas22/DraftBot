"""How a player is named on a pairing embed: their Discord name, plus their linked
MTGO handle when the Discord name wouldn't let an opponent find them on MTGO.

The handle is left off when one name contains the other, ignoring case and
punctuation: "aberdasher" playing as "aber", or "gregg / keezles" as "keezles",
already say who they are. Anything else -- "aylin" as an unrelated handle --
gets it.
"""
import re
from typing import Iterable, Mapping, Optional

import discord
from loguru import logger

from helpers.display_names import get_display_name, plain_text
from models.mtgo_account import MtgoAccount

_NOT_ALNUM = re.compile(r"[^0-9a-z]")

# Below this length, "contains" stops meaning anything: "al" is in half the server.
MIN_CONTAINED_LENGTH = 3


def _squash(name: str) -> str:
    return _NOT_ALNUM.sub("", name.lower())


def _name_gives_handle(display_name: str, mtgo_name: str) -> bool:
    a, b = _squash(display_name), _squash(mtgo_name)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return shorter == longer or (len(shorter) >= MIN_CONTAINED_LENGTH and shorter in longer)


def pairing_label(shown: str, display_name: str, mtgo_name: Optional[str]) -> str:
    """`shown` is the name as rendered (escaped, with crown icons); `display_name`
    is the raw Discord name the comparison runs on."""
    if not mtgo_name or _name_gives_handle(display_name, mtgo_name):
        return shown
    return f"{shown} (MTGO: {plain_text(mtgo_name)})"


async def pairing_labels(guild: discord.Guild, user_ids: Iterable[object],
                         sign_ups: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Label for every player given, with a single MTGO lookup for all of them.

    A player Discord can no longer resolve (left the server, not cached) is named
    by `sign_ups` -- the draft's {user_id: name at sign-up} -- before "Unknown User".

    The handle is a convenience, so a failed lookup costs only the handles: the
    pairings still post, and a result report already committed is not turned
    into an error by the re-render that follows it.
    """
    ids = list(dict.fromkeys(str(uid) for uid in user_ids))
    try:
        handles: dict[str, str] = await MtgoAccount.usernames_for_discord_ids(ids)
    except Exception:
        logger.exception("MTGO handle lookup failed; pairings show Discord names only")
        handles = {}
    labels: dict[str, str] = {}
    for uid in ids:
        # Repair rows can carry no player at all (str(None) == "None").
        member = guild.get_member(int(uid)) if uid.isdigit() else None
        if member is None:
            signed_up = str((sign_ups or {}).get(uid) or "")
            shown = plain_text(signed_up) if signed_up else "Unknown User"
            labels[uid] = pairing_label(shown, signed_up, handles.get(uid))
        else:
            labels[uid] = pairing_label(get_display_name(member, guild), member.display_name,
                                        handles.get(uid))
    return labels


# The two shapes of a pairing-embed field: as posted, and once a result is in.
def unplayed_field(match_number: int, label1: str, label2: str) -> str:
    return f"⚫ **Match {match_number}**\n{label1} v.\n{label2}"


def reported_field(emoji: str, match_number: int, label1: str, wins1: int,
                   label2: str, wins2: int) -> str:
    return f"{emoji}**Match {match_number}**\n{label1}: {wins1} wins\n{label2}: {wins2} wins"
