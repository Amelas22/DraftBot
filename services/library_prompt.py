"""Ask a player, once, whether they borrow their deck from the library.

A rental library offers a drafted deck only to the players who said they want
one (services/draft_deck_assignment). That answer is a saved preference per
player and server, asked right after somebody signs up -- the stake
confirmation, or joining a premade team -- and only while it is unset, so a
regular is asked once rather than every draft.

Asked only where the answer can matter: a rental library behind this server,
offering this draft's cube, that this player may borrow from. A communal
library needs no answer -- it lends to everybody, as it always has.

Changed later from the draft's Change Entry / Settings panel or /toggle_library.
"""
from typing import Any, Optional

from typing_extensions import override

import discord
from loguru import logger

from models.library import RENTAL, Library
from preference_service import get_uses_library, set_uses_library
from services.library_access_service import may_borrow
from services.library_service import library_for, offers
from services.library_trust_service import hold_for


async def rental_library_for(guild_id: Any) -> "Optional[Library]":
    """The rental library behind this server, or None if it has none."""
    library = await library_for(guild_id)
    if library is None or library.kind != RENTAL:
        return None
    return library


async def library_to_ask_about(guild_id: Any, cube_id: Any,
                               player_id: Any) -> "Optional[Library]":
    """The rental library this player could borrow this cube from, if any."""
    library = await rental_library_for(guild_id)
    if library is None or not await offers(library.id, cube_id):
        return None
    if not await may_borrow(library.id, player_id):
        return None
    return library


def prompt_text(library: Library, hold: int) -> str:
    held = (f"It holds **{hold} tix** from your wallet while the deck is out "
            f"and gives it all back when you return it."
            if hold else "It's free.")
    return (f"🏛️ **Borrow your deck from the {library.name} library?**\n"
            f"When you draft here, the library can lend you the cards you "
            f"drafted, so you don't need your own. {held}\n"
            f"We'll remember your answer. Change it any time with "
            f"`/toggle_library` or **Change Entry / Settings**.")


def saved_text(uses_library: bool) -> str:
    if uses_library:
        return ("✅ Saved: after each draft here you'll be offered your drafted "
                "deck from the library. Collect it with `/library borrow`.")
    return ("👍 Saved: you'll bring your own cards. Change it any time with "
            "`/toggle_library`.")


class _Answer(discord.ui.Button[Any]):
    """One of the two answers. No custom_id: the prompt is ephemeral, and an id
    fixed per draft would be shared by every player who sees it, each new
    prompt evicting the last (the same reason as the stake settings panel)."""

    def __init__(self, player_id: str, guild_id: str, uses_library: bool) -> None:
        super().__init__(
            label="Borrow from the library" if uses_library else "I'll bring my own cards",
            style=(discord.ButtonStyle.success if uses_library
                   else discord.ButtonStyle.secondary))
        self.player_id = player_id
        self.guild_id = guild_id
        self.uses_library = uses_library

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if not await set_uses_library(self.player_id, self.guild_id, self.uses_library):
            await interaction.response.send_message(
                "⚠️ Couldn't save that just now. Try `/toggle_library`.",
                ephemeral=True)
            return
        await interaction.response.edit_message(
            content=saved_text(self.uses_library), view=None)
        if self.view is not None:
            self.view.stop()


class LibraryPreferenceView(discord.ui.View):
    """Two answers. A timeout saves nothing, so the player is asked next time."""

    def __init__(self, player_id: str, guild_id: str) -> None:
        super().__init__(timeout=600)
        self.add_item(_Answer(player_id, guild_id, True))
        self.add_item(_Answer(player_id, guild_id, False))


async def maybe_ask_library_preference(interaction: discord.Interaction,
                                       cube_id: Any) -> None:
    """Ask, as a follow-up to a sign-up already answered, if the answer is unset.

    Never raises: the player is already signed up, and a failure here must not
    read as a failed sign-up. It leaves the preference unset, so they are asked
    again at their next sign-up.
    """
    try:
        if interaction.user is None or interaction.guild_id is None:
            return
        player_id, guild_id = str(interaction.user.id), str(interaction.guild_id)
        if await get_uses_library(player_id, guild_id) is not None:
            return
        library = await library_to_ask_about(guild_id, cube_id, player_id)
        if library is None:
            return
        hold = await hold_for(library, player_id) or 0
        await interaction.followup.send(
            prompt_text(library, hold),
            view=LibraryPreferenceView(player_id, guild_id), ephemeral=True)
    except Exception:
        logger.exception("library prompt: could not ask {} about the library",
                         getattr(interaction.user, "id", None))


async def toggle_uses_library(player_id: Any, guild_id: Any) -> Optional[bool]:
    """Flip the saved answer and return the new one, or None if it didn't save.

    Unset flips to True: somebody reaching for the switch before being asked
    is asking to borrow.
    """
    new = not await get_uses_library(player_id, guild_id)
    return new if await set_uses_library(str(player_id), str(guild_id), new) else None


class LibraryToggleButton(discord.ui.Button[Any]):
    """The library switch on a draft's Change Entry / Settings panel. No
    custom_id, for the panel's reason: it is ephemeral and per player."""

    def __init__(self, player_id: str, guild_id: str,
                 uses_library: Optional[bool]) -> None:
        super().__init__(
            label="Library: ON 🏛️" if uses_library else "Library: OFF",
            style=(discord.ButtonStyle.success if uses_library
                   else discord.ButtonStyle.secondary))
        self.player_id = player_id
        self.guild_id = guild_id

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This button is not for you.",
                                                    ephemeral=True)
            return
        new = await toggle_uses_library(self.player_id, self.guild_id)
        await interaction.response.send_message(
            "⚠️ Couldn't save that just now. Try `/toggle_library`."
            if new is None else saved_text(new), ephemeral=True)
