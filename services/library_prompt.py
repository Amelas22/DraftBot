"""What a player is told about the library when they sign up.

A rental library lends only to a draft that reserved its copy of the cube
(services/library_reservation), and within it only to players who said they
borrow. That answer is a saved preference per player and server, asked once,
right after somebody signs up -- the stake confirmation, a non-staked sign-up,
or joining a premade team -- and only while it is unset.

A player who borrows is not asked again. Signing up reserves the copy for the
queue when one is free, and they are told either that it is reserved, with a
button to say they don't need it this time, or which queue is using it, so
they can join that one to fire it.

Only where it can matter: a rental library behind this server, offering this
draft's cube, that this player may borrow from. A communal library needs none
of this -- it lends to everybody, as it always has.

The preference changes from the draft's Change Entry / Settings panel or
/toggle_library.
"""
from typing import Any, Optional

from typing_extensions import override

import discord
from loguru import logger

from models.library import RENTAL, Library
from preference_service import get_uses_library, set_uses_library
from services.library_access_service import may_borrow
from services.library_reservation import (ReserveAnswer, queue_link,
                                          release_player, reserve)
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
        return ("✅ Saved: when you sign up here, the library's copy of the cube "
                "is reserved for your draft if one is free.")
    return ("👍 Saved: you'll bring your own cards. Change it any time with "
            "`/toggle_library`.")


def reservation_text(answer: ReserveAnswer, cube: Any, hold: int) -> Optional[str]:
    """What a borrower is told about this draft's reservation, or None."""
    if answer.status in ("reserved", "held"):
        held = f" The hold is **{hold} tix**, refunded on return." if hold else ""
        return (f"🏛️ The library's copy of **{cube}** is reserved for this draft. "
                f"Collect your deck with `/library borrow` once it's drafted."
                f"{held}")
    if answer.status == "in_use":
        by = answer.by
        link = queue_link(by) if by is not None else None
        if by is not None and by.teams_start_time is None and link:
            return (f"⏳ The library's copy of **{cube}** is in use by "
                    f"[another queue]({link}). Join that one to fire it and "
                    f"borrow there, or bring your own cards for this draft.")
        return (f"⏳ The library's copy of **{cube}** is in use by a draft "
                f"underway. Bring your own cards for this one.")
    if answer.status == "short":
        return (f"⚠️ The library can't cover **{cube}** right now, so bring "
                f"your own cards for this draft.")
    return None


class _Release(discord.ui.Button[Any]):
    """Says this player doesn't need the library for this draft. No custom_id:
    the note is ephemeral and per player."""

    def __init__(self, player_id: str, session_id: str) -> None:
        super().__init__(label="I don't need the library this draft",
                         style=discord.ButtonStyle.secondary)
        self.player_id = player_id
        self.session_id = session_id

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if not await release_player(self.session_id, self.player_id):
            await interaction.response.send_message(
                "⚠️ Couldn't find that draft any more.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=("👍 Noted: no library deck for you this draft. If nobody "
                     "else here needs it, the copy goes to the next queue."),
            view=None)
        if self.view is not None:
            self.view.stop()


def _release_view(player_id: str, session_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_Release(player_id, session_id))
    return view


async def _reserve_and_say(player_id: str, draft: Any, library: Library
                           ) -> "tuple[Optional[str], Optional[discord.ui.View]]":
    answer = await reserve(draft.session_id)
    hold = await hold_for(library, player_id) or 0
    text = reservation_text(answer, draft.cube, hold)
    view = (_release_view(player_id, str(draft.session_id))
            if answer.status in ("reserved", "held") else None)
    return text, view


class _Answer(discord.ui.Button[Any]):
    """One of the two answers. No custom_id: the prompt is ephemeral, and an id
    fixed per draft would be shared by every player who sees it, each new
    prompt evicting the last (the same reason as the stake settings panel)."""

    def __init__(self, player_id: str, guild_id: str, draft: Any,
                 library: Library, uses_library: bool) -> None:
        super().__init__(
            label="Borrow from the library" if uses_library else "I'll bring my own cards",
            style=(discord.ButtonStyle.success if uses_library
                   else discord.ButtonStyle.secondary))
        self.player_id = player_id
        self.guild_id = guild_id
        self.draft = draft
        self.library = library
        self.uses_library = uses_library

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if not await set_uses_library(self.player_id, self.guild_id, self.uses_library):
            await interaction.response.send_message(
                "⚠️ Couldn't save that just now. Try `/toggle_library`.",
                ephemeral=True)
            return
        text, view = saved_text(self.uses_library), None
        if self.uses_library:
            said, view = await _reserve_and_say(self.player_id, self.draft, self.library)
            if said:
                text += "\n" + said
        await interaction.response.edit_message(content=text, view=view)
        if self.view is not None:
            self.view.stop()


class LibraryPreferenceView(discord.ui.View):
    """Two answers. A timeout saves nothing, so the player is asked next time,
    and says so rather than leaving buttons that no longer answer."""

    def __init__(self, player_id: str, guild_id: str, draft: Any,
                 library: Library) -> None:
        super().__init__(timeout=600)
        self.sent: "Optional[discord.WebhookMessage]" = None
        self.add_item(_Answer(player_id, guild_id, draft, library, True))
        self.add_item(_Answer(player_id, guild_id, draft, library, False))

    @override
    async def on_timeout(self) -> None:
        if self.sent is None:
            return
        try:
            await self.sent.edit(content=UNANSWERED, view=None)
        except Exception:
            logger.opt(exception=True).debug("library prompt: could not close it")


UNANSWERED = ("Not saved. Run `/toggle_library` to borrow your deck from the "
              "library, or answer next time you sign up.")


async def library_after_sign_up(interaction: discord.Interaction, draft: Any) -> None:
    """Ask, or reserve and say where the copy is -- a follow-up to a sign-up
    already answered.

    Never raises: the player is already signed up, and a failure here must not
    read as a failed sign-up. A player who could not be asked is told how to
    opt in, since an unanswered player gets no deck.
    """
    library = None
    try:
        if interaction.user is None or interaction.guild_id is None:
            return
        player_id, guild_id = str(interaction.user.id), str(interaction.guild_id)
        library = await rental_library_for(guild_id)
        if library is None:
            return
        uses = await get_uses_library(player_id, guild_id)
        if uses is False:
            return
        library = await library_to_ask_about(guild_id, draft.cube, player_id)
        if library is None:
            return
        if uses is None:
            hold = await hold_for(library, player_id) or 0
            prompt = LibraryPreferenceView(player_id, guild_id, draft, library)
            prompt.sent = await interaction.followup.send(
                prompt_text(library, hold), view=prompt, ephemeral=True, wait=True)
            return
        text, view = await _reserve_and_say(player_id, draft, library)
        if text:
            if view is None:
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.followup.send(text, view=view, ephemeral=True)
    except Exception:
        logger.exception("library: could not tell {} about the library after "
                         "sign-up", getattr(interaction.user, "id", None))
        if library is None:
            return
        try:
            await interaction.followup.send(
                "Couldn't check the library just now. Run `/toggle_library` "
                "if you'd like to borrow your deck.", ephemeral=True)
        except Exception:
            logger.opt(exception=True).warning(
                "library: could not tell {} it failed either",
                getattr(interaction.user, "id", None))


async def toggle_uses_library(player_id: Any, guild_id: Any) -> Optional[bool]:
    """Flip the saved answer and return the new one, or None if it didn't save.

    Unset flips to True: somebody reaching for the switch before being asked
    is asking to borrow.
    """
    try:
        new = not await get_uses_library(player_id, guild_id)
    except Exception:
        # Guessing here is how a tap meant to turn it off turns it on.
        logger.exception("library switch: could not read {}'s preference", player_id)
        return None
    return new if await set_uses_library(str(player_id), str(guild_id), new) else None


class LibraryToggleButton(discord.ui.Button[Any]):
    """The library switch on a draft's Change Entry / Settings panel. No
    custom_id, for the panel's reason: it is ephemeral and per player.

    Turned on from a draft still filling, it also reserves the copy for it.
    """

    def __init__(self, player_id: str, guild_id: str,
                 uses_library: Optional[bool], draft: Any = None,
                 library: "Optional[Library]" = None) -> None:
        super().__init__(
            label="Library: ON 🏛️" if uses_library else "Library: OFF",
            style=(discord.ButtonStyle.success if uses_library
                   else discord.ButtonStyle.secondary))
        self.player_id = player_id
        self.guild_id = guild_id
        self.draft = draft
        self.library = library

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This button is not for you.",
                                                    ephemeral=True)
            return
        new = await toggle_uses_library(self.player_id, self.guild_id)
        if new is None:
            await interaction.response.send_message(
                "⚠️ Couldn't save that just now. Try `/toggle_library`.",
                ephemeral=True)
            return
        text, view = saved_text(new), None
        if new and self.draft is not None and self.library is not None:
            said, view = await _reserve_and_say(self.player_id, self.draft, self.library)
            if said:
                text += "\n" + said
        if view is None:
            await interaction.response.send_message(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True)
