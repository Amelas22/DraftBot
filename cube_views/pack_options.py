"""Dependency-light cube/pack selection building blocks.

These live in ``cube_views`` (importing only ``discord``) so that both the
draft-start flow (``modals.py``) and the in-draft Update Cube flow
(``views.py`` via ``CubeSelectionView``) can share the exact same controls
without the circular import that ``modals`` -> ``sessions`` -> ``views`` would
otherwise create.
"""
import asyncio
from typing import Any, Optional

import discord
from loguru import logger
from config import get_cube_options
from services.card_library_inventory import (
    cube_as_the_library_sees_it, cube_support, library_available, library_holdings,
)

# Default pack structure (standard MTG draft / Draftmancer defaults).
DEFAULT_PACKS_PER_PLAYER = 3
DEFAULT_CARDS_PER_PACK = 15
PACKS_PER_PLAYER_MIN, PACKS_PER_PLAYER_MAX = 1, 15
CARDS_PER_PACK_MIN, CARDS_PER_PACK_MAX = 1, 24


# Embed field used to surface non-default pack structure on the draft post.
PACK_FORMAT_FIELD_NAME = "Pack Format:"


def pack_format_display(packs_per_player, cards_per_pack):
    """Human-readable pack structure, or None when it matches the default.

    Used to decide whether to show a pack-format field on the draft embed —
    standard 3 x 15 drafts return None so the embed stays uncluttered.
    """
    if packs_per_player == DEFAULT_PACKS_PER_PLAYER and cards_per_pack == DEFAULT_CARDS_PER_PACK:
        return None
    return f"{packs_per_player} packs × {cards_per_pack} cards"


def selected_value(interaction: discord.Interaction):
    """Pull the chosen string-select value from the raw interaction payload."""
    values = (interaction.data or {}).get("values") or []
    return values[0] if values else None


def parse_pack_settings(packs_raw, cards_raw):
    """Parse and validate the advanced-options inputs.

    Returns (packs_per_player, cards_per_pack, errors). A field that fails to
    parse/validate comes back as None with a human-readable error appended.
    """
    errors = []

    def _parse(raw, name, lo, hi):
        try:
            value = int(str(raw).strip())
        except (ValueError, TypeError):
            errors.append(f"{name} must be a whole number.")
            return None
        if not (lo <= value <= hi):
            errors.append(f"{name} must be between {lo} and {hi}.")
            return None
        return value

    packs = _parse(packs_raw, "Packs per player", PACKS_PER_PLAYER_MIN, PACKS_PER_PLAYER_MAX)
    cards = _parse(cards_raw, "Cards per pack", CARDS_PER_PACK_MIN, CARDS_PER_PACK_MAX)
    return packs, cards, errors


class AdvancedOptionsModal(discord.ui.Modal):
    """Optional per-pod overrides for pack structure. Stores results on a view."""

    def __init__(self, view, *args, **kwargs):
        super().__init__(title="Advanced Draft Options", *args, **kwargs)
        self.view_ref = view
        self.add_item(discord.ui.InputText(
            label=f"Packs per player ({PACKS_PER_PLAYER_MIN}-{PACKS_PER_PLAYER_MAX})",
            value=str(view.packs_per_player),
            custom_id="packs_per_player_input",
        ))
        self.add_item(discord.ui.InputText(
            label=f"Cards per pack ({CARDS_PER_PACK_MIN}-{CARDS_PER_PACK_MAX})",
            value=str(view.cards_per_pack),
            custom_id="cards_per_pack_input",
        ))

    async def callback(self, interaction: discord.Interaction) -> None:
        packs, cards, errors = parse_pack_settings(
            self.children[0].value, self.children[1].value
        )
        if errors:
            await interaction.response.send_message(
                "❌ Could not save advanced options:\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        self.view_ref.packs_per_player = packs
        self.view_ref.cards_per_pack = cards
        await interaction.response.send_message(
            f"✅ Advanced options set: **{packs}** packs per player, **{cards}** cards per pack.\n"
            "Select a cube (if you haven't) and click the green ✅ button when ready.",
            ephemeral=True,
        )


class CustomCubeNameModal(discord.ui.Modal):
    """Collects a custom cube id, then runs an async submit handler.

    Used when the user picks "Custom Cube..." and then submits — mirrors how the
    draft-start flow collects a custom cube name.
    """

    def __init__(self, view, on_submit, *args, **kwargs):
        super().__init__(title="Custom Cube", *args, **kwargs)
        self.view_ref = view
        self.on_submit = on_submit
        self.add_item(discord.ui.InputText(
            label="Custom Cube Name",
            placeholder="Enter your cube name",
            custom_id="cube_name_input",
        ))

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view_ref.cube_choice = self.children[0].value
        await self.on_submit(interaction, self.view_ref)


class BaseCubeSelectionView(discord.ui.View):
    """Shared cube-selection UI used by both the draft-start and Update Cube flows.

    Provides the cube dropdown (with a Custom Cube option), an Advanced Options
    button for pack settings, and a submit button. Subclasses implement
    ``submit_callback`` and may override ``submit_label`` / ``submit_emoji``.
    """

    submit_label = "Start Draft"
    submit_emoji = "✅"

    def __init__(self, session_type, guild_id, current_cube=None, own_session_id=None):
        super().__init__()
        self.session_type = session_type
        # The draft this picker changes, for Update Cube: a copy it reserved
        # itself is not "in use by another queue".
        self.own_session_id = own_session_id
        self.cube_choice = current_cube
        self.packs_per_player = DEFAULT_PACKS_PER_PLAYER
        self.cards_per_pack = DEFAULT_CARDS_PER_PACK

        self.guild_id = guild_id
        # Kept so the list can be re-rendered once the library's prices have
        # been read; a view is built synchronously and those live in the DB.
        self._cube_options = list(get_cube_options(guild_id, session_type))
        options = [discord.SelectOption(**opt) for opt in self._cube_options]
        options.append(discord.SelectOption(label="Custom Cube...", value="custom"))
        self.cube_select = discord.ui.Select(placeholder="Select a Cube", options=options)
        self.cube_select.callback = self.cube_select_callback
        self.add_item(self.cube_select)

        self.advanced_button = discord.ui.Button(
            label="Advanced Options", emoji="⚙️", style=discord.ButtonStyle.secondary
        )
        self.advanced_button.callback = self.advanced_options_callback
        self.add_item(self.advanced_button)

        self.submit_button = discord.ui.Button(
            label=self.submit_label, emoji=self.submit_emoji, style=discord.ButtonStyle.success
        )
        self.submit_button.callback = self.submit_callback
        self.add_item(self.submit_button)

    async def show_library_prices(self) -> bool:
        """Mark the cubes this server can borrow from, with what it costs.
        True if any cube was marked.

        Separate from __init__ because the price lives in the database and a
        view is constructed synchronously; see mark_once_sent for when it runs.

        A failure here leaves the list unmarked rather than failing the draft:
        not knowing what borrowing costs is a worse dropdown, but no dropdown
        at all stops the draft that was being created.
        """
        try:
            marked = await mark_library_cubes(self._cube_options, self.guild_id,
                                              own_session_id=self.own_session_id)
        except Exception:
            logger.opt(exception=True).warning(
                "cube list: could not read library prices; cubes left unmarked")
            return False
        if marked == self._cube_options:
            return False
        options = [discord.SelectOption(**opt) for opt in marked]
        options.append(discord.SelectOption(label="Custom Cube...", value="custom"))
        for opt in options:
            opt.default = (opt.value == self.cube_choice)
        self.cube_select.options = options
        return True

    def mark_once_sent(self, interaction: discord.Interaction) -> "asyncio.Task[None]":
        """Mark the library's cubes on a picker already sent, and edit it.

        After sending, not before: marking reads each library cube from
        CubeCobra, and a picker has three seconds to reach Discord. The picker
        appears at once, unmarked, and gains its badges a moment later; a
        server with no library, or a read that fails, leaves it as it was.
        """
        async def mark() -> None:
            try:
                if await self.show_library_prices():
                    await interaction.edit_original_response(view=self)
            except Exception:
                logger.opt(exception=True).warning(
                    "cube list: could not mark the library's cubes")
        task = asyncio.create_task(mark())
        _MARKING.add(task)
        task.add_done_callback(_MARKING.discard)
        return task

    async def advanced_options_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AdvancedOptionsModal(self))

    async def cube_select_callback(self, interaction: discord.Interaction):
        self.cube_choice = selected_value(interaction)
        # Keep the chosen option highlighted after the message is re-rendered.
        for opt in self.cube_select.options:
            opt.default = (opt.value == self.cube_choice)
        label = next(
            (o.label for o in self.cube_select.options if o.value == self.cube_choice),
            self.cube_choice,
        )
        await interaction.response.edit_message(
            content=(
                f"✅ Cube selected: **{label}**.\n"
                f"Optionally adjust ⚙️ **Advanced Options**, then click {self.submit_emoji} "
                f"**{self.submit_label}** when ready."
            ),
            view=self,
        )

    async def submit_callback(self, interaction: discord.Interaction):
        raise NotImplementedError


# Running mark_once_sent tasks, held so a task is not collected half way.
_MARKING: "set[asyncio.Task[None]]" = set()

# Discord rejects a SelectOption description longer than this, taking the whole
# dropdown with it rather than truncating.
_JOIN = " · "
_DESCRIPTION_LIMIT = 100


def _library_note(least: int, most: int) -> str:
    """How a cube's borrowing price reads in the list.

    The NUMBER, not merely that there is one: "costs tix" leaves a player
    guessing whether they can afford it, and being able to answer that before
    joining is the whole reason this is shown here. Both figures for a tiered
    library, as on the signup board: the list is read by the whole room and
    cannot know which applies to the reader.
    """
    if most == 0:
        return "🆓 Free cube — borrow a deck free"
    if least == most:
        return f"🏛️ Library cube — {most} tix hold"
    return f"🏛️ Library cube — {least}–{most} tix hold"


def _shortfall_note(short: int, stocked: bool) -> "tuple[str, str]":
    """(emoji, caveat) for a cube the library cannot cover right now.

    Two different situations that must not read the same way. A cube whose
    cards are OUT is borrowable again shortly, and telling somebody so is
    useful. A cube the library never stocked is not coming back later, and
    "try again soon" would be a lie.
    """
    if not stocked:
        return ("🚫", "library doesn't stock it yet")
    return ("⚠️", f"{short} cards out right now")


async def mark_library_cubes(options: list, guild_id, own_session_id=None) -> list:
    """Copy of `options` with the borrowable cubes marked.

    Shown where cubes are CHOSEN because a drafter has to know whether they can
    afford to borrow before they join, not after they have drafted a deck they
    cannot pay for.

    The badge answers "does the library lend for this cube", which is stable.
    Whether it can cover a draft THIS MINUTE is far more volatile -- a draft in
    progress has its cards in players' hands -- so that goes in a caveat beside
    the badge rather than into the badge itself. A badge that flickered on and
    off as drafts came and went would read as broken rather than informative.

    A cube the library cannot lend for here is returned untouched. Most cubes
    have nothing to do with the library, and decorating every one of them would
    make the marked ones invisible -- which is also why only the priced ones
    cost a CubeCobra read.
    """
    from models.library import RENTAL
    from services.card_library_inventory import fetch_cube, read_once
    from services.library_reservation import reservations
    from services.library_service import library_for, offered_among, price_range_of

    # The common case, a server that borrows nothing, pays this one lookup
    # and stops.
    library = await library_for(guild_id)
    if library is None:
        return list(options)
    library_id = library.id
    offered = await offered_among(library_id, [o.get("value") for o in options])
    if not offered:
        return list(options)
    least, most = price_range_of(library) or (0, 0)
    from services.library_access_service import is_invite_only
    if await is_invite_only(library_id):
        # Same rule as library_signup_note, and the gate sits here so a
        # whitelisted library also skips the CubeCobra reads below.
        return list(options)
    # One read of each cube for the whole list: the shelf check and the
    # per-cube coverage below would otherwise each fetch it from CubeCobra.
    read = read_once(fetch_cube)
    held = await library_holdings(library_id)
    available = await library_available(library_id, fetch=read)
    # Which cubes a queue has reserved, read once for the whole list: another
    # queue's is "in use"; this draft's own (Update Cube) is held for it.
    by_others, by_us = set(), set()
    if library.kind == RENTAL:
        for draft in await reservations(library_id):
            mine = own_session_id is not None and str(draft.session_id) == str(own_session_id)
            (by_us if mine else by_others).add(draft.cube)

    marked = []
    for opt in options:
        cube = opt.get("value")
        if cube not in offered:
            marked.append(opt)
            continue

        emoji = "🆓" if most == 0 else "🏛️"
        parts = [_library_note(least, most)]
        try:
            seen = await cube_as_the_library_sees_it(cube, fetch=read)
            cards = seen.cards if seen else None
        except Exception:
            logger.opt(exception=True).warning(
                "cube list: could not read {} to check availability", cube)
            cards = None
        if cards and cube not in by_us:
            short = cube_support(cards, available).cards_short
            if short:
                stocked = cube_support(cards, held).ok
                emoji, caveat = _shortfall_note(short, stocked=stocked)
                if stocked and cube in by_others:
                    # The whole cube is out because a queue reserved it, not
                    # because cards are scattered on loan: say where it is.
                    emoji, caveat = "⏳", "in use by another queue"
                parts.append(caveat)

        note = " · ".join(parts)
        existing = opt.get("description")
        if existing:
            # The guild's own text gives way, not the note. Both together run
            # past Discord's cap, and a plain truncation cut from the right --
            # which is where the note is, so a server with a wordy cube
            # description lost the PRICE and kept the prose. "🏛️ Library cube
            # — 10" for a 100-tix cube is worse than no marking at all.
            room = _DESCRIPTION_LIMIT - len(note) - len(_JOIN)
            existing = existing[:room] if room > 0 else ""
        description = f"{existing}{_JOIN}{note}" if existing else note
        marked.append({**opt, "description": description[:_DESCRIPTION_LIMIT],
                       "emoji": emoji})
    return marked


LIBRARY_FIELD_NAME = "Cards:"


class LibraryLineUnknown(Exception):
    """The library line could not be checked; keep whatever the board says."""


async def library_signup_check(cube_id, guild_id,
                               session_id: Any = None) -> "Optional[str]":
    """The board line with its link; None if the library isn't involved.
    Raises LibraryLineUnknown when it can't be checked.

    `session_id` is the board's own draft, once it exists: whatever that draft
    has reserved is its own copy, not one held by another queue. At posting
    there is no draft yet, so nothing is excluded.

    Whatever it says ends with a link to the library page, which shows what
    the shelf holds and what is out -- the detail a board line has no room for.
    """
    from services.library_service import library_page_link

    note = await _library_signup_line(cube_id, guild_id, session_id)
    return f"{note} {library_page_link()}" if note else None


async def library_signup_note(cube_id, guild_id) -> "Optional[str]":
    """One line for the signup board, or None if the library is not involved or
    could not be checked -- at posting there is no earlier line to keep."""
    try:
        return await library_signup_check(cube_id, guild_id)
    except LibraryLineUnknown:
        return None


def apply_library_field(embed: Any, line: "Optional[str]") -> None:
    """Write the library line into a board embed: replaced if present, added just
    after the Cube field if not, removed when there is none."""
    index = next((i for i, f in enumerate(embed.fields) if f.name == LIBRARY_FIELD_NAME), None)
    if line:
        if index is not None:
            embed.set_field_at(index, name=LIBRARY_FIELD_NAME, value=line, inline=False)
            return
        cube = next((i for i, f in enumerate(embed.fields) if f.name == "Cube:"), None)
        at = cube + 1 if cube is not None else len(embed.fields)
        embed.insert_field_at(at, name=LIBRARY_FIELD_NAME, value=line, inline=False)
    elif index is not None:
        embed.remove_field(index)


async def _reserved_since(session_id: Any) -> "Optional[int]":
    """When this draft took the library's copy, as a Unix time, if it still
    holds it. Raises LibraryLineUnknown when that can't be read: a board must
    not claim a hold it did not check, nor drop one it has."""
    from models.draft_session import DraftSession
    from services.library_reservation import still_reserved

    try:
        draft = await DraftSession.get_by_session_id(str(session_id))
        if draft is None or not await still_reserved(draft):
            return None
    except Exception:
        logger.opt(exception=True).warning(
            "signup board: could not read the reservation of {}", session_id)
        raise LibraryLineUnknown(str(session_id)) from None
    return int(draft.library_reserved_at.timestamp())


async def _library_signup_line(cube_id, guild_id,
                               session_id: Any = None) -> "Optional[str]":
    """The board's library line, without the link.

    Answers the question a player deciding whether to join actually has: can I
    play this without owning the cards? Every other library signal lives where
    the draft is CREATED, which only the organiser sees -- so somebody signing
    up had no way to know, and finding out at fire time wastes the whole pod's
    evening.

    None for a cube the library does not lend for, which is most of them. A
    field reading "you need your own cards" on every ordinary draft would be
    noise on the majority in order to inform a minority.

    Never claims coverage it has not checked. If the cube cannot be read, or
    the ledger cannot be reached, it raises LibraryLineUnknown rather than
    promising a deck that may not be there -- which is the exact failure it
    exists to prevent; the caller keeps whatever line the board already has.
    And a library whose custodian reports it cannot work is said to be offline:
    the shelf may cover the cube, but nobody can borrow from it. The custodian's
    state is the health monitor's last poll; before the first poll it is
    unknown, and an unknown custodian is not called offline.
    """
    from models.library import RENTAL
    from services.card_lending_service import lending_custodian_up
    from services.library_access_service import is_invite_only
    from services.library_service import library_for, offers, price_range_of

    custodian = None
    try:
        library = await library_for(guild_id)
        if library is None or not await offers(library.id, cube_id):
            return None
        if await is_invite_only(library.id):
            # Before the reads below, not after: this runs inline in draft
            # creation under a one-second budget, and cube_as_the_library_sees_it
            # is an uncached CubeCobra fetch. Computing coverage and then
            # discarding it spent that whole budget on nothing.
            #
            # The board is ONE message for the whole room, so it cannot address
            # only the people who may borrow. It used to state the terms and add
            # "invite-only", which told a room of people who cannot borrow what
            # they would have paid if they could. While a library lends to named
            # people only the room is told nothing, and the named are DMed
            # instead (services/library_reminders).
            return None
        # Alongside the coverage reads rather than after them, so the two share
        # the budget instead of queueing for it.
        custodian = asyncio.create_task(lending_custodian_up())
        # One read of each cube for the whole board line.
        from services.card_library_inventory import fetch_cube, read_once
        read = read_once(fetch_cube)
        seen = await cube_as_the_library_sees_it(cube_id, fetch=read)
        if not (seen and seen.cards):
            raise LibraryLineUnknown(str(cube_id))
        cards = seen.cards
        available = await library_available(
            library.id, fetch=read,
            exclude_session_id=None if session_id is None else str(session_id))
        covered = cube_support(cards, available).ok
        up = await custodian
    except LibraryLineUnknown:
        raise
    except Exception:
        logger.opt(exception=True).warning(
            "signup board: could not check the library for {}", cube_id)
        raise LibraryLineUnknown(str(cube_id)) from None
    finally:
        if custodian is not None and not custodian.done():
            custodian.cancel()

    if not covered:
        # Where the copy is, when another queue reserved it: those players can
        # join that one and fire it (services/library_reservation).
        # A failed lookup is not "bring your own cards": the copy may be in a
        # queue they could join, so the line is unknown, not uncovered.
        try:
            from services.library_reservation import joinable_link, queue_holding
            link = joinable_link(await queue_holding(guild_id, cube_id, session_id))
        except Exception:
            logger.opt(exception=True).warning(
                "signup board: could not see who holds {}", cube_id)
            raise LibraryLineUnknown(str(cube_id)) from None
        if link:
            return (f"⏳ **Library in use** — its copy of this cube is reserved "
                    f"by [another queue]({link}). Join that one to fire it, or "
                    f"bring your own cards.")
        return ("⚠️ **Bring your own cards** — the library can't cover this "
                "cube right now.")
    if up is False:
        return ("⚠️ **Bring your own cards** — the library is offline right "
                "now.")

    least, most = price_range_of(library) or (0, 0)
    # Who may borrow, where that is not simply everybody here: a library that
    # lends to members and to experienced drafters says where the line is.
    who = ""
    if library.min_drafts_to_borrow is not None:
        who = f" Open to players with {library.min_drafts_to_borrow}+ drafts here."
    if library.kind == RENTAL:
        who += " Signing up holds it for this draft; agree before it fires to keep it."
    # A rental library is said to be one, beside its terms. A communal library
    # keeps the line it has always had.
    lead = ("Library available, no cards needed" if library.kind == RENTAL
            else "No cards needed")
    # "Available" read the same whether the copy was free or already held for
    # this queue, so nobody could tell a hold had been taken. The time shows
    # it is a live hold, not a stale board.
    since = (await _reserved_since(session_id)
             if library.kind == RENTAL and session_id is not None else None)
    if since is not None:
        lead = f"Library reserved for this queue <t:{since}:R>, no cards needed"
    mark = "🔒" if since is not None else ("🆓" if most == 0 else "🏛️")

    if most == 0:
        return (f"{mark} **{lead}** — borrow your deck from the library "
                f"free.{who}")

    # Says the hold comes back, because that is the part that decides
    # whether somebody can afford to play: 100 tix they get back is a very
    # different proposition from 100 tix spent.
    if least == most:
        return (f"{mark} **{lead}** — borrow your deck for a "
                f"**{most} tix** hold, refunded when you return it.{who}")
    # Both figures, because the board is one message for the whole room and
    # cannot know which applies to the reader. Naming only the lower would
    # promise a price to the very people about to be asked for the higher one;
    # naming only the higher would put off members who will never pay it.
    return (f"{mark} **{lead}** — borrow your deck for a "
            f"**{least}–{most} tix** hold, refunded when you return it. "
            f"Members pay {least}.{who}")
