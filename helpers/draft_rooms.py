"""The rooms one draft gets: their names, their permissions, and their creation.

Extracted from views.PersistentView.create_team_channel, which had grown to do
config lookup, category resolution, permission construction, channel creation,
database persistence and scouting-thread spawning in one body. The consequence
was that every change to how a room is made had to edit that body, so successive
changes kept re-opening the same twenty lines and could not be reviewed apart.

The seam is "make one room" versus "make this draft's rooms". Everything here is
the former: it takes a guild and plain values, returns a channel, and touches no
session state and no database. create_team_channel keeps the latter -- the
accumulation of channel_ids, the commit, the threads.

Nothing in this module changes behaviour relative to the code it came from. It
is a move, so that the changes that follow it are small.
"""
import asyncio
from collections import defaultdict
from typing import Any, Iterable

import discord
from loguru import logger

# Discord caps a category at 50 channels. The refusal comes back as the generic
# "Invalid Form Body" (50035); only the nested parent_id error identifies the cap,
# which is why this keys on the code AND the field rather than the message text.
CATEGORY_FULL_CODE = 50035
CATEGORY_CHANNEL_LIMIT = 50

# The shared chat plus one channel per team.
DRAFT_ROOM_COUNT = 3
TEAM_COUNT = 2


def rooms_needed(voice: bool, teams: int = TEAM_COUNT) -> int:
    """How many channels one draft will put in its category.

    The shared chat plus one per team, and a voice channel per team when the
    guild has voice on. Counted rather than assumed, because reserving fewer
    than the draft creates splits it across two categories -- which is the exact
    thing reserving capacity exists to prevent.

    Voice only counts here when it lands in the SAME category. A guild with its
    own voice category reserves nothing extra in the draft one.
    """
    return 1 + teams + (teams if voice else 0)

# Serialises overflow-category creation, PER GUILD. ~20 drafts can hit a full
# category in the same second; without this each would create its own
# "Draft Channels 2" instead of the first creating it and the rest filling it.
# Keyed by guild because it is held across an awaited category create: one process
# serves several guilds, and a rate-limited create in one must not block another.
_OVERFLOW_LOCKS: "defaultdict[int, asyncio.Lock]" = defaultdict(asyncio.Lock)

# The shared draft chat is addressed by this team name. It is not a team: it has
# no roster of its own, it is the one channel every player can see, and several
# rules key off it (admin access, no scouting threads, no team voice).
SHARED_CHAT_TEAM = "Draft"


def team_channel_name(team_name: str, friendly_id: str) -> str:
    """The text channel's name. Derived purely from the team and the draft, which
    is what lets a later run recognise a room it already made."""
    return f"{team_name}-Chat-{friendly_id}"


def team_voice_name(team_name: str, friendly_id: str) -> str:
    """The voice channel's name, paired with the text channel's above."""
    return f"{team_name}-Voice-{friendly_id}"


def resolve_category(guild: Any, config: dict[str, Any], key: str) -> Any:
    """The configured category under `key`, or None when the guild has not named
    one (or names one that no longer exists). None means "no category", which
    Discord accepts -- the channel is simply not grouped."""
    name = (config.get("categories") or {}).get(key)
    return discord.utils.get(guild.categories, name=name) if name else None


def team_overwrites(
    guild: Any,
    config: dict[str, Any],
    team_name: str,
    team_members: Iterable[Any],
    bot_role_names: Iterable[str],
) -> "dict[Any, discord.PermissionOverwrite]":
    """Who can see and speak in one draft room.

    Starts closed -- @everyone denied -- and opens it to the bot, the draft-access
    bots, this team's members, and (for the shared chat only) the admin role.

    Only bot-MANAGED integration roles are honoured for `bot_role_names`: the role
    Discord creates when a bot is invited cannot be assigned to a human, so a
    same-named vanity role cannot be used to read private team channels.
    """
    # Keyed by Role or Member, which have no common protocol to name -- both are
    # hashable and both are valid overwrite targets, which is all this needs.
    overwrites: "dict[Any, discord.PermissionOverwrite]" = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, manage_messages=True),
    }

    wanted = set(bot_role_names)
    bot_roles = [r for r in guild.roles if r.name in wanted and r.tags and r.tags.bot_id]
    for role in bot_roles:
        overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    logger.info(f"Draft-access bot roles for '{team_name}': {[r.name for r in bot_roles] or 'none'}")

    # The admin role sees the shared chat and NOT the private team channels --
    # a team's own room is private from staff by design.
    if team_name == SHARED_CHAT_TEAM:
        admin_role_name = (config.get("roles") or {}).get("admin")
        admin_role = discord.utils.get(guild.roles, name=admin_role_name) if admin_role_name else None
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True, manage_messages=True)
        else:
            logger.warning(f"Admin role '{admin_role_name}' not found in guild {guild.name}")

    for member in team_members:
        overwrites[member] = discord.PermissionOverwrite(
            read_messages=True, manage_messages=True)
    return overwrites


def _category_is_full(error: "discord.HTTPException") -> bool:
    return error.code == CATEGORY_FULL_CODE and "parent_id" in (error.text or "")


async def draft_category(guild: Any, config: "dict[str, Any]", needed: int) -> Any:
    """The category ALL of this draft's rooms should go in, from config.

    One home for the three steps -- read the config, find the category, check it
    has room for the whole set -- so a caller cannot do two of the three.
    """
    return await category_with_room(guild, resolve_category(guild, config, "draft"), needed)


async def category_with_room(guild: Any, base: Any, needed: int) -> Any:
    """The category ALL of this draft's rooms should go in.

    Resolved once per draft and passed to each room, rather than re-decided per
    room: the answer has to be the same for every room of one draft, and a
    per-room check gives different answers as the category fills.

    None in, None out -- a guild that groups nothing does not get a category
    invented for it.
    """
    if base is None:
        return None
    if len(base.channels) + needed <= CATEGORY_CHANNEL_LIMIT:
        return base
    return await overflow_category(guild, base, needed)


def configured_base_name(name: str) -> str:
    """The configured category's name, given either it or one of its siblings.

    Overflow siblings are named "<base> 2", "<base> 3". Since a draft can now be
    placed directly into a sibling, the thing handed to ensure_channel is not
    always the configured category -- and numbering off it would produce
    "Draft Channels 2 2" instead of "Draft Channels 3".
    """
    root, _, suffix = name.rpartition(" ")
    return root if root and suffix.isdigit() else name


async def overflow_category(guild: Any, base: Any, needed: int = 1) -> Any:
    """A sibling of `base` with room for `needed` channels -- reused if one
    exists, else created.

    Named "<base> 2", "<base> 3", ... so the set is self-describing in the channel
    list and recognisable on the next run. Empty ones are deliberately NOT deleted:
    they are reused, so the number of them settles at whatever peak concurrency
    needs rather than growing, and deleting one that a draft is about to fill would
    just churn.

    Permissions are copied from `base`, so a draft category with restricted
    visibility cannot spawn an open one.
    """
    async with _OVERFLOW_LOCKS[guild.id]:
        numbered: "dict[int, Any]" = {}
        root = configured_base_name(base.name)
        prefix = f"{root} "
        for category in guild.categories:
            suffix = category.name[len(prefix):] if category.name.startswith(prefix) else ""
            if suffix.isdigit():
                numbered[int(suffix)] = category
        for n in sorted(numbered):
            if len(numbered[n].channels) + needed <= CATEGORY_CHANNEL_LIMIT:
                return numbered[n]
        return await guild.create_category(
            f"{root} {max(numbered, default=1) + 1}",
            overwrites=dict(base.overwrites),
            position=base.position + 1,
        )


def existing_channel(channels: Iterable[Any], name: str, owned_ids: "set[str]") -> Any:
    """A channel THIS draft already has, matched on its deterministic name.

    Room creation must be safe to re-enter: a run that died partway leaves real
    channels behind, and a retry that made second copies would be as bad as one
    that made none.

    The name alone is not enough to claim one. friendly_id is drawn at random and
    is explicitly NOT unique -- DraftSession.get_by_friendly_id documents that
    duplicates within a guild happen -- so two live drafts can want the same
    channel name, and matching on name alone would hand one team the other team's
    private channel. `owned_ids` is the session's own recorded channel_ids, which
    can only contain channels a previous run of THIS draft created and committed.

    Compared case-insensitively because Discord lowercases TEXT channel names but
    leaves voice channel names as sent -- so an exact match would find the voice
    channel and miss the text one.
    """
    target = name.lower()
    return next((c for c in channels
                 if c.name.lower() == target and str(c.id) in owned_ids), None)


async def ensure_channel(guild: Any, kind: str, name: str,
                         overwrites: "dict[Any, discord.PermissionOverwrite]",
                         category: Any, owned_ids: "set[str] | None" = None) -> Any:
    """The draft's `kind` ("text" or "voice") channel called `name`.

    One room, made once, moving to a fresh category when the one it was aimed at
    is full. A guild allows 500 channels but a category only 50, so a guild
    running many drafts at once fills the draft category with room to spare
    elsewhere. The order is: the category asked for, then a numbered sibling of
    it, then no category. Uncategorised is untidy; a draft with no channel cannot
    be played.

    Only the full-category refusal moves on. Anything else -- no permission, a
    5xx -- is raised, because a different category would not fix it.
    """
    # Only when this draft has recorded channels at all: with nothing owned there
    # is by definition nothing to reuse, and guild.text_channels rebuilds and
    # sorts a list over every channel in the guild. A first run pays nothing.
    pool = (guild.text_channels if kind == "text" else guild.voice_channels) if owned_ids else []
    already = existing_channel(pool, name, owned_ids or set())
    if already is not None:
        logger.info(f"Reusing existing {kind} channel '{already.name}' (ID: {already.id}) "
                    f"-- a previous run created it")
        return already

    create = guild.create_text_channel if kind == "text" else guild.create_voice_channel
    attempted = []
    while True:
        try:
            channel = await create(name=name, overwrites=overwrites, category=category)
            break
        except discord.HTTPException as e:
            if category is None or not _category_is_full(e):
                raise
            attempted.append(category)
            try:
                # Numbering hangs off the configured category, which attempted[0]
                # is not necessarily -- a draft can be placed straight into a
                # sibling. configured_base_name strips the suffix back off.
                nxt = await overflow_category(guild, attempted[0])
            except discord.HTTPException as create_error:
                logger.warning(
                    f"Could not create an overflow category for "
                    f"'{attempted[-1].name}': {create_error}")
                nxt = None
            # A category the cache still reported as roomy can be full in reality;
            # not retrying one already refused is what guarantees this terminates.
            category = nxt if nxt is not None and nxt not in attempted else None
            logger.warning(
                f"Category '{attempted[-1].name}' is full; creating {kind} channel "
                + (f"'{name}' in '{category.name}' instead." if category else
                   f"'{name}' outside any category. Permissions are unchanged, but "
                   f"it will not be grouped.")
            )
    # Read off the CHANNEL, not the category asked for: with overflow the two
    # differ exactly when it matters most, and a log that reports the intent
    # rather than the result is how a placement bug hides.
    landed = getattr(getattr(channel, "category", None), "name", None) or "None"
    logger.info(f"✅ Created {kind} channel '{name}' (ID: {channel.id}) in category '{landed}'")
    return channel
