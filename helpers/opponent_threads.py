"""Per-opponent scouting threads in a team's private draft channel.

When Red-Team-Chat / Blue-Team-Chat are created, each team gets one thread per
player on the *other* team, so they have a dedicated place to pool reads and
matchup notes on that specific opponent.

Best-effort throughout: create_team_channel persists channel_ids and moves the
session to the 'pairings' stage, so nothing here may raise into that path.
"""
from __future__ import annotations


import discord
from loguru import logger

from helpers.draft_rooms import side_by_prefix
from helpers.utils import (
    DISCORD_THREAD_NAME_LIMIT, THREAD_ARCHIVE_MINUTES, mention_all, send_then_mention,
)

ARCHIVED_THREAD_LOOKUP_LIMIT = 100

# One row per team channel: who its occupants are, and how to describe the
# team they face. Keyed on the shared prefix constants -- the channel names
# have been renamed once already, and a stale literal here would silently
# produce no threads at all.
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
    side = side_by_prefix(team_name)
    if side is None or side.key is None:
        return [], [], ""
    opposing = side.opposite
    assert opposing is not None      # a keyed side always faces the other one
    return (side.roster_of(team_a, team_b),
            side.opponents_of(team_a, team_b),
            opposing.label.name)


def thread_name(discord_id: str, sign_ups: dict[str, str] | None) -> str:
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

    sharing = [str(other_id) for other_id, other in sign_ups.items()
               if str(other or "").strip() == name]
    if len(sharing) < 2:
        return name[:DISCORD_THREAD_NAME_LIMIT]

    # Shortest id suffix that separates this player from everyone else signed
    # up under the same display name. Four digits reads better than a full
    # snowflake and is enough unless two of them happen to end alike -- then it
    # grows, rather than handing both players the same thread. Growing beats a
    # fixed length because the caller posts INTO the thread this names: a
    # collision would not just cost the second player a thread, it would file
    # their pool under the other player's name.
    mine = str(discord_id)
    others = [other_id for other_id in sharing if other_id != mine]
    size = 4
    while size < len(mine) and any(o[-size:] == mine[-size:] for o in others):
        size += 1
    # Clip the name *before* appending, so a very long name can't push the
    # discriminator past the limit and take uniqueness with it.
    suffix = f" ({mine[-size:]})"
    return name[:DISCORD_THREAD_NAME_LIMIT - len(suffix)] + suffix


async def threads_by_name(channel: discord.TextChannel) -> dict[str, object]:
    """Every thread in `channel`, active and archived, keyed by name.

    One archived scan per channel. A scouting thread auto-archives after
    THREAD_ARCHIVE_MINUTES and pools are posted long after the draft finishes,
    so by then almost every thread this looks for is archived -- and asking per
    player would pay for that scan once per opponent instead of once per team.

    Active threads win a name collision: if a name somehow exists twice, the
    live one is the one people are reading.
    """
    found: dict[str, object] = {}
    try:
        async for thread in channel.archived_threads(limit=ARCHIVED_THREAD_LOOKUP_LIMIT):
            found[thread.name] = thread
    except Exception as e:
        # Keep the active names rather than lose the whole run: a failed
        # archived lookup would otherwise make every thread look absent.
        logger.warning(f"[opponent-threads] archived thread lookup failed: {e}")
    for thread in getattr(channel, "threads", []) or []:
        found[thread.name] = thread
    return found


def _starter(name: str, opponent_team_label: str, own_ids: list[str] | None = None) -> str:
    """The thread's opening message, tagging the team that owns this channel.

    The mention is not decoration: Discord adds a mentioned member to the
    thread, and a thread you belong to is the one that appears in your sidebar
    rather than staying buried behind the channel.

    It is EDITED in rather than sent, so it never notifies -- see
    send_then_mention, and _starter_plain for the form that is posted first.

    Tags the OWN team only. The player being scouted is on the other team and
    cannot see this channel; mentioning them would be both useless and rude.
    """
    mentions = mention_all(own_ids)
    lead = f"{mentions} " if mentions else ""
    return f"{lead}{_starter_plain(name, opponent_team_label)}"


def _starter_plain(name: str, opponent_team_label: str) -> str:
    """The starter as first POSTED: identical, minus the mention."""
    return (
        f"🔍 Scouting thread for **{name}** ({opponent_team_label}). "
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
        existing = set(await threads_by_name(channel))
    except Exception as e:
        logger.warning(f"[opponent-threads] could not resolve opponents for '{team_name}': {e}")
        return 0

    created = 0
    for discord_id in ids:
        name = str(discord_id)
        try:
            name = thread_name(discord_id, sign_ups)
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
            # Posted plain, then edited to carry the mention. One scouting thread
            # per OPPONENT means a 4v4 draft would otherwise notify every player
            # four times at room creation. silent=True was the previous attempt
            # at this and is not enough: it drops the push but still leaves the
            # mention badge.
            await send_then_mention(
                thread, _starter_plain(name, label), _starter(name, label, own_ids))
        except Exception as e:
            logger.warning(f"[opponent-threads] created '{name}' but its starter failed: {e}")
    return created
