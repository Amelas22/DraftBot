"""Dependency-light cube/pack selection building blocks.

These live in ``cube_views`` (importing only ``discord``) so that both the
draft-start flow (``modals.py``) and the in-draft Update Cube flow
(``views.py`` via ``CubeSelectionView``) can share the exact same controls
without the circular import that ``modals`` -> ``sessions`` -> ``views`` would
otherwise create.
"""
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

    def __init__(self, session_type, guild_id, current_cube=None):
        super().__init__()
        self.session_type = session_type
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

    async def show_library_prices(self) -> None:
        """Mark the cubes this server can borrow from, with what it costs.

        Separate from __init__ because the price lives in the database and a
        view is constructed synchronously. Callers await it before sending.

        A failure here leaves the list unmarked rather than failing the draft:
        not knowing what borrowing costs is a worse dropdown, but no dropdown
        at all stops the draft that was being created.
        """
        try:
            marked = await mark_library_cubes(self._cube_options, self.guild_id)
        except Exception:
            logger.opt(exception=True).warning(
                "cube list: could not read library prices; cubes left unmarked")
            return
        options = [discord.SelectOption(**opt) for opt in marked]
        options.append(discord.SelectOption(label="Custom Cube...", value="custom"))
        for opt in options:
            opt.default = (opt.value == self.cube_choice)
        self.cube_select.options = options

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


# Discord rejects a SelectOption description longer than this, taking the whole
# dropdown with it rather than truncating.
_JOIN = " · "
_DESCRIPTION_LIMIT = 100


def _library_note(collateral: int) -> str:
    """How a cube's borrowing price reads in the list.

    The NUMBER, not merely that there is one: "costs tix" leaves a player
    guessing whether they can afford it, and being able to answer that before
    joining is the whole reason this is shown here.
    """
    if collateral == 0:
        return "🆓 Free cube — borrow a deck free"
    return f"🏛️ Library cube — {collateral} tix deposit"


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


async def mark_library_cubes(options: list, guild_id) -> list:
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
    from services.library_service import library_id_for, prices_for

    prices = await prices_for([o.get("value") for o in options], guild_id)
    if not prices:
        return list(options)

    # Resolved once, after prices_for has already established there IS a
    # library here -- so the common case, a server that borrows nothing, pays
    # a single lookup and stops.
    library_id = await library_id_for(guild_id)
    held = await library_holdings(library_id)
    available = await library_available(library_id)

    marked = []
    for opt in options:
        cube = opt.get("value")
        priced = prices.get(cube)
        if priced is None:
            marked.append(opt)
            continue

        collateral = priced
        emoji = "🆓" if collateral == 0 else "🏛️"
        parts = [_library_note(collateral)]
        try:
            seen = await cube_as_the_library_sees_it(cube)
            cards = seen.cards if seen else None
        except Exception:
            logger.opt(exception=True).warning(
                "cube list: could not read {} to check availability", cube)
            cards = None
        if cards:
            short = cube_support(cards, available).cards_short
            if short:
                emoji, caveat = _shortfall_note(
                    short, stocked=cube_support(cards, held).ok)
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


async def library_signup_note(cube_id, guild_id) -> "Optional[str]":
    """One line for the signup board, or None if the library is not involved.

    Answers the question a player deciding whether to join actually has: can I
    play this without owning the cards? Every other library signal lives where
    the draft is CREATED, which only the organiser sees -- so somebody signing
    up had no way to know, and finding out at fire time wastes the whole pod's
    evening.

    None for a cube the library does not lend for, which is most of them. A
    field reading "you need your own cards" on every ordinary draft would be
    noise on the majority in order to inform a minority.

    Never claims coverage it has not checked. If the cube cannot be read, or
    the ledger cannot be reached, it says nothing rather than promising a deck
    that may not be there -- which is the exact failure it exists to prevent.
    """
    from services.library_access_service import is_invite_only
    from services.library_service import library_for, offers, price_of

    try:
        library = await library_for(guild_id)
        if library is None or not await offers(library.id, cube_id):
            return None
        seen = await cube_as_the_library_sees_it(cube_id)
        if not (seen and seen.cards):
            return None
        cards = seen.cards
        available = await library_available(library.id)
        covered = cube_support(cards, available).ok
        restricted = await is_invite_only(library.id)
    except Exception:
        logger.opt(exception=True).warning(
            "signup board: could not check the library for {}", cube_id)
        return None

    if not covered:
        return ("⚠️ **Bring your own cards** — the library can't cover this "
                "cube right now.")

    collateral = price_of(library) or 0
    # Said before the terms, not after: on an invite-only library the terms do
    # not apply to most people reading this. The board is shared, so it cannot
    # know who is looking -- but promising a free deck to a room where most of
    # them will be turned away at /borrow is the version that wastes an evening.
    who = " for members" if restricted else ""
    if collateral == 0:
        free = f"🆓 **No cards needed{who}** — borrow your deck from the library free."
        return free if not restricted else (
            free + " Borrowing here is invite-only.")

    # Says the deposit comes back, because that is the part that decides
    # whether somebody can afford to play: 100 tix they get back is a very
    # different proposition from 100 tix spent.
    tail = " Borrowing here is invite-only." if restricted else ""
    return (f"🏛️ **No cards needed{who}** — borrow your deck for a "
            f"**{collateral} tix** deposit, refunded when you return it."
            + tail)
