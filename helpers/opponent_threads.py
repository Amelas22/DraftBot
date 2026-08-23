"""Per-opponent scouting threads in a team's private draft channel.

When Red-Team-Chat / Blue-Team-Chat are created, each team gets one thread per
player on the *other* team, so they have a dedicated place to pool reads and
matchup notes on that specific opponent.

Best-effort throughout: create_team_channel persists channel_ids and moves the
session to the 'pairings' stage, so nothing here may raise into that path.
"""
from __future__ import annotations

from collections import namedtuple

import discord
from loguru import logger

from helpers.substitutes import TEAM_A_CHANNEL_PREFIX, TEAM_B_CHANNEL_PREFIX
from helpers.utils import DISCORD_THREAD_NAME_LIMIT, THREAD_ARCHIVE_MINUTES, mention_all

ARCHIVED_THREAD_LOOKUP_LIMIT = 100

# One row per team channel: who its occupants are, and how to describe the
# team they face. Keyed on the shared prefix constants -- the channel names
# have been renamed once already, and a stale literal here would silently
# produce no threads at all.
#
# One table, not three. The rosters and the label were previously three
# separate dispatches on the same two keys, so "an unknown channel name
# yields nothing" was an invariant spread across them -- and the label lookup
# was an unguarded dict access that only happened to be safe because the
# roster lookup had already returned empty and short-circuited the caller.
_TeamChannel = namedtuple("_TeamChannel", "own_is_team_a opponent_label")
_TEAM_CHANNELS = {
    TEAM_A_CHANNEL_PREFIX: _TeamChannel(own_is_team_a=True, opponent_label="Blue Team"),
    TEAM_B_CHANNEL_PREFIX: _TeamChannel(own_is_team_a=False, opponent_label="Red Team"),
}


def team_channel_rosters(
    team_name: str,
    team_a: list[str] | None,
    team_b: list[str] | None,
) -> tuple[list[str], list[str], str]:
    """`(own roster, opponents, opponent label)` for a team channel.

    `([], [], "")` for the shared "Draft" channel -- which holds both teams,
    so nobody in it is an opponent -- and for any other channel name, which is
    what keeps swiss (whose only channel is "Draft") out of this feature.

    "Own" is who gets tagged into each scouting thread; "opponents" is who
    gets a thread. Returning both from one lookup is what makes it impossible
    to tag one team while scouting the wrong one.
    """
    entry = _TEAM_CHANNELS.get(team_name)
    if entry is None:
        return [], [], ""
    a, b = list(team_a or []), list(team_b or [])
    own, opponents = (a, b) if entry.own_is_team_a else (b, a)
    return own, opponents, entry.opponent_label


def _thread_name(discord_id: str, sign_ups: dict[str, str] | None) -> str:
    """Thread label for one opponent, derived from their sign-up display name.

    Must be a pure function of (discord_id, sign_ups): spawn_opponent_threads
    skips names it already finds in the channel, so a name that varies with
    roster order or with existing threads would make a re-run duplicate them.

    Display names are not unique (sign_ups stores member.display_name), so an id
    fragment is appended when another sign-up shares the name -- otherwise the
    second player silently gets no thread. Counted over the whole sign_ups dict,
    never over iteration state, to keep the function pure.
    """
    sign_ups = sign_ups or {}
    name = str(sign_ups.get(discord_id) or "").strip()
    if not name:
        return str(discord_id)[:DISCORD_THREAD_NAME_LIMIT]

    shared = sum(1 for other in sign_ups.values() if str(other or "").strip() == name)
    if shared < 2:
        return name[:DISCORD_THREAD_NAME_LIMIT]

    # Clip the name *before* appending, so a very long name can't push the
    # discriminator past the limit and take uniqueness with it.
    suffix = f" ({str(discord_id)[-4:]})"
    return name[:DISCORD_THREAD_NAME_LIMIT - len(suffix)] + suffix


async def _existing_thread_names(channel: discord.TextChannel) -> set[str]:
    """Names of the threads already in `channel`, active and archived.

    `channel.threads` is pycord's cache of ACTIVE threads only, and scouting
    threads auto-archive after THREAD_ARCHIVE_MINUTES -- so a re-run days later
    would not see its own earlier work and would duplicate every thread. The
    archived lookup is a real API call; if it fails we keep the active names
    rather than lose the whole run.
    """
    names = {t.name for t in channel.threads}
    try:
        async for thread in channel.archived_threads(limit=ARCHIVED_THREAD_LOOKUP_LIMIT):
            names.add(thread.name)
    except Exception as e:
        logger.warning(f"[opponent-threads] archived thread lookup failed: {e}")
    return names


def _starter(name: str, opponent_team_label: str, own_ids: list[str] | None = None) -> str:
    """The thread's opening message, which also tags the team that owns this
    channel.

    The mention is not decoration: Discord adds a mentioned member to the
    thread, and a thread you belong to is the one that appears in your
    sidebar rather than staying buried behind the channel. Carried inside the
    starter that was already being posted, so tagging costs no extra message.

    Tags the OWN team only. The player being scouted is on the other team and
    cannot see this channel; mentioning them would be both useless and rude.
    """
    mentions = mention_all(own_ids)
    lead = f"{mentions} " if mentions else ""
    return (
        f"{lead}🔍 Scouting thread for **{name}** ({opponent_team_label}). "
        "Share reads, matchup notes, and what you saw them pick."
    )


async def spawn_opponent_threads(
    channel: discord.TextChannel,
    team_name: str,
    team_a: list[str] | None,
    team_b: list[str] | None,
    sign_ups: dict[str, str] | None,
) -> int:
    """Create one scouting thread per opponent in `channel`. Returns how many
    were created.

    Never raises -- this runs inside channel creation, which is the draft's
    critical path, so the guard lives here rather than at each call site.

    Skips opponents that already have a same-named thread, so re-running against
    an existing channel (recover_draft_channels) doesn't double up.
    """
    try:
        own_ids, ids, label = team_channel_rosters(team_name, team_a, team_b)
        if not ids:
            return 0
        existing = await _existing_thread_names(channel)
    except Exception as e:
        logger.warning(f"[opponent-threads] could not resolve opponents for '{team_name}': {e}")
        return 0

    created = 0
    for discord_id in ids:
        name = str(discord_id)
        try:
            name = _thread_name(discord_id, sign_ups)
            if name in existing:
                continue
            thread = await channel.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=THREAD_ARCHIVE_MINUTES,
            )
        except Exception as e:
            logger.warning(f"[opponent-threads] could not create thread '{name}': {e}")
            continue

        # The thread exists from here on. Record it before posting the starter:
        # that is a separate call which can fail on its own, and treating that
        # as a failed creation would lose track of a thread that is really
        # there -- and re-create it on the next run.
        existing.add(name)
        created += 1
        try:
            # silent: one scouting thread per OPPONENT means a 4v4 draft would
            # otherwise ping every player four times at room creation. A silent
            # mention still adds them to the thread -- verified against Discord,
            # with a loud send as the control -- so the sidebar entry survives
            # while the notification storm does not. The pools thread stays
            # loud: that is one message per draft and worth a ping.
            await thread.send(_starter(name, label, own_ids), silent=True)
        except Exception as e:
            logger.warning(f"[opponent-threads] created '{name}' but its starter failed: {e}")
    return created
