"""What a player is told about the library when they sign up.

A rental library lends only to a draft that reserved its copy of the cube
(services/library_reservation), and within it only to players who said they
borrow. That answer is a saved preference per player and server, asked once,
right after somebody signs up -- the stake confirmation, a non-staked sign-up,
or joining a premade team -- and only while it is unset.

A player who borrows is not asked again. Signing up holds the copy for the
queue when one is free, and they are told either that it is held, with buttons
to agree to borrow or to say they don't need it this time, or which queue is
using it, so they can join that one to fire it. A held copy nobody agreed to
goes back when the draft fires. Answering "Borrow from the library" when first
asked is agreeing, since that question already quotes the hold.

Only where it can matter: a rental library behind this server, offering this
draft's cube, that this player may borrow from. A communal library needs none
of this -- it lends to everybody, as it always has.

The preference changes from a row like the entry cap's: on the draft's Change
Entry / Settings panel, or behind the Library button other boards carry
instead (services/library_boards).
"""
import asyncio
from typing import Any, Optional

from typing_extensions import override

import discord
from loguru import logger

from models.library import Library
from preference_service import get_uses_library, set_uses_library
from services.library_access_service import may_borrow
from models.draft_session import DraftSession
from services.library_reservation import (ReserveAnswer, agree_player, agreed,
                                          joinable_link, owes_agreement,
                                          reclaim_player, release_player, reserve)
from services.library_service import library_page_link, offers, rental_library_for
from services.library_trust_service import hold_for


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
            f"We'll remember your answer. Change it any time from "
            f"{WHERE_TO_CHANGE}. "
            f"{library_page_link('What the library holds', preview=False)}")


def saved_text(uses_library: bool) -> str:
    if uses_library:
        return ("✅ Saved: when you sign up here, the library's copy of the cube "
                "is held for your draft if one is free.")
    return ("👍 Saved: you'll bring your own cards. Change it any time from "
            f"{WHERE_TO_CHANGE}.")


AGREE_LABEL = "I agree and wish to use the library"
# The draft's own panel: Change Entry / Settings on a staked board, the
# Library button on every other (services/library_boards). Alone, it is where
# to retry a borrow: it has the **Borrow from the library** button, where
# /settings has a toggle that would switch an On library off.
DRAFT_PANEL = ("the draft's **Change Entry / Settings** button "
               "(**Library 🏛️** on other drafts)")
# Where the setting lives.
WHERE_TO_CHANGE = f"`/settings`, or {DRAFT_PANEL}"
TOO_LATE = ("⚠️ Too late to agree: this draft has already started, so it "
            "won't borrow from the library.")
COULD_NOT_CHECK = ("⚠️ Couldn't check the library just now. Try again in a minute "
                   f"from {DRAFT_PANEL}.")


def reservation_text(answer: ReserveAnswer, cube: Any, hold: int,
                     has_agreed: bool = True) -> Optional[str]:
    """What a borrower is told about this draft's reservation, or None."""
    if answer.status in ("reserved", "held"):
        held = f" The hold is **{hold} tix**, refunded on return." if hold else ""
        if not has_agreed:
            return (f"🏛️ The library's copy of **{cube}** is held for this draft. "
                    f"Press **{AGREE_LABEL}** before it fires to keep it, or it "
                    f"goes back.{held}")
        return (f"🏛️ The library's copy of **{cube}** is reserved for this draft. "
                f"Collect your deck with `/library borrow` once it's drafted."
                f"{held}")
    if answer.status == "in_use":
        link = joinable_link(answer.by)
        if link:
            return (f"⏳ The library's copy of **{cube}** is in use by "
                    f"[another queue]({link}). Join that one to fire it and "
                    f"borrow there, or bring your own cards for this draft.")
        return (f"⏳ The library's copy of **{cube}** is in use by a draft "
                f"underway. Bring your own cards for this one.")
    if answer.status == "short":
        return (f"⚠️ The library can't cover **{cube}** right now, so bring "
                f"your own cards for this draft.")
    if answer.status == "unknown":
        return (f"⚠️ Couldn't check the library's shelf for **{cube}** just now. "
                f"To try again, use {DRAFT_PANEL} and press **Borrow from the library**.")
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
        # Answered first: what follows reads the database, and Discord gives
        # up on an interaction after three seconds.
        await interaction.response.defer()
        try:
            found = await release_player(self.session_id, self.player_id)
        except Exception:
            logger.exception("library: could not release {} from {}",
                             self.player_id, self.session_id)
            await interaction.followup.send(
                "⚠️ Couldn't record that just now. Press it again in a minute.",
                ephemeral=True)
            return
        if not found:
            await interaction.followup.send(
                "⚠️ Couldn't find that draft any more.", ephemeral=True)
            return
        await interaction.edit_original_response(
            content=("👍 Noted: no library deck for you this draft. If nobody "
                     "else here needs it, the copy goes to the next queue."),
            view=None)
        if self.view is not None:
            self.view.stop()


async def _agree_and_say(player_id: str, session_id: str, cube: Any, hold: int
                         ) -> "tuple[str, Optional[discord.ui.View]]":
    answer = await agree_player(session_id, player_id)
    if answer.status == "fired":
        return TOO_LATE, None
    text = reservation_text(answer, cube, hold)
    if text is None:
        # Left the queue, or switched the library off since this note.
        return ("The library isn't holding a copy for you in this draft. Sign "
                f"up with borrowing on, from {WHERE_TO_CHANGE}, to ask again."), None
    view = (_release_view(player_id, session_id, cube, hold, owes=False)
            if answer.status in ("reserved", "held") else None)
    return text, view


class _Agree(discord.ui.Button[Any]):
    """Agrees to borrow in this draft, which keeps the held copy past teams
    forming. On the note it answers, it replaces the note; on the settings
    panel (`in_place=False`) it replies separately, leaving the panel be."""

    def __init__(self, player_id: str, session_id: str, cube: Any, hold: int,
                 in_place: bool = True, row: Optional[int] = None) -> None:
        super().__init__(label=AGREE_LABEL, style=discord.ButtonStyle.success, row=row)
        self.player_id = player_id
        self.session_id = session_id
        self.cube = cube
        self.hold = hold
        self.in_place = in_place

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This button is not for you.",
                                                    ephemeral=True)
            return
        # Answered first: agreeing can reserve, which reads the cube list, and
        # Discord gives up on an interaction after three seconds.
        await interaction.response.defer()
        try:
            text, view = await _agree_and_say(self.player_id, self.session_id,
                                              self.cube, self.hold)
        except Exception:
            logger.exception("library: could not record {} agreeing in {}",
                             self.player_id, self.session_id)
            text, view = COULD_NOT_CHECK, None
            await interaction.followup.send(text, ephemeral=True)
            return
        if self.in_place:
            await interaction.edit_original_response(content=text, view=view)
            if self.view is not None:
                self.view.stop()
        elif view is None:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(text, view=view, ephemeral=True)


def library_agree_button(player_id: str, session_id: str, cube: Any,
                       hold: int) -> discord.ui.Button[Any]:
    """The agree button for the draft's Change Entry / Settings panel: the way
    back to it once the note's own buttons are gone, as they are after a
    restart."""
    return _Agree(player_id, session_id, cube, hold, in_place=False, row=LIBRARY_ROW)


def _release_view(player_id: str, session_id: str, cube: Any, hold: int,
                  owes: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if owes:
        view.add_item(_Agree(player_id, session_id, cube, hold))
    view.add_item(_Release(player_id, session_id))
    return view


async def reserve_and_say(player_id: str, draft: Any, library: Library,
                          agree: bool = False, reclaim: bool = False
                          ) -> "tuple[Optional[str], Optional[discord.ui.View]]":
    """Reserve if a copy is free and say where things stand.

    `reclaim` only for the player switching borrowing on: that is asking
    again, and takes back their own "don't need it". A sign-up path reached by
    changing entry or switching team is not, and leaves it alone.
    """
    hold = await hold_for(library, player_id) or 0
    session_id = str(draft.session_id)
    if agree:
        answer = await agree_player(session_id, player_id)
        has_agreed = True
    else:
        if reclaim:
            await reclaim_player(session_id, player_id)
        answer = await reserve(session_id)
        # Read afresh: somebody re-joining a queue they already agreed in. One
        # who said "don't need it" since is not agreed: they get the agree
        # button, which is what takes that back.
        current = await DraftSession.get_by_session_id(session_id)
        player = str(player_id)
        has_agreed = (current is not None and player in agreed(current)
                      and player not in {str(p) for p in (current.library_released or [])})
        if (not has_agreed and current is not None
                and current.teams_start_time is not None
                and answer.status in ("reserved", "held")):
            return TOO_LATE, None
    text = reservation_text(answer, draft.cube, hold, has_agreed)
    view = (_release_view(player_id, session_id, draft.cube, hold, owes=not has_agreed)
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
        # Answered first: a yes reserves, which reads the cube list, and Discord
        # gives up on an interaction after three seconds.
        await interaction.response.defer()
        if not await set_uses_library(self.player_id, self.guild_id, self.uses_library):
            await interaction.followup.send(
                f"⚠️ Couldn't save that just now. Try again from {WHERE_TO_CHANGE}.",
                ephemeral=True)
            return
        text, view = saved_text(self.uses_library), None
        if self.uses_library:
            # The question quoted the hold, so answering yes is agreeing.
            try:
                said, view = await reserve_and_say(self.player_id, self.draft,
                                                   self.library, agree=True)
            except Exception:
                logger.exception("library: saved {}'s yes but could not reserve in {}",
                                 self.player_id, self.draft.session_id)
                said, view = COULD_NOT_CHECK, None
            if said:
                text += "\n" + said
        await interaction.edit_original_response(content=text, view=view)
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


UNANSWERED = ("Not saved. To borrow your deck from the library, use "
              f"{WHERE_TO_CHANGE}; or answer next time you sign up.")


# Running library steps, held so the event loop's weak reference is not the
# only one and a task cannot be collected half way.
_RUNNING: "set[asyncio.Task[None]]" = set()


def library_after_sign_up_soon(interaction: discord.Interaction,
                               draft: Any) -> "asyncio.Task[None]":
    """Start the library step for a sign-up already confirmed, and return.

    Called straight after the confirmation, before the board is redrawn, so
    neither waits on the other: a redraw that fails cannot skip the question
    -- an unasked or unreserved player gets no deck -- and the first reserve's
    cube read does not hold up everybody's view of the sign-ups. Safe to leave
    running: library_after_sign_up never raises, and a follow-up stays valid
    for fifteen minutes.
    """
    task = asyncio.create_task(library_after_sign_up(interaction, draft))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


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
        text, view = await reserve_and_say(player_id, draft, library)
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
                "Couldn't check the library just now. To borrow your deck, use "
                f"{WHERE_TO_CHANGE}.", ephemeral=True)
        except Exception:
            logger.opt(exception=True).warning(
                "library: could not tell {} it failed either",
                getattr(interaction.user, "id", None))


LIBRARY_ROW = 2   # below the stake select (row 0) and the entry cap (row 1)


def _status_look(uses_library: Optional[bool]) -> "tuple[str, discord.ButtonStyle]":
    if uses_library:
        return "Library: ON 🏛️", discord.ButtonStyle.success
    if uses_library is False:
        return "Library: OFF 🃏", discord.ButtonStyle.danger
    return "Library: not set", discord.ButtonStyle.secondary


class _SetLibrary(discord.ui.Button[Any]):
    """One side of the library row on a draft's Change Entry / Settings panel,
    laid out like the entry cap beside it: a status, then one button per
    answer. No custom_id, for the panel's reason: it is ephemeral and per
    player.

    Pressed, it relabels the status on the panel itself, then says what it
    saved. Turned on from a draft still filling, it also holds the copy for
    it, with the note asking the player to agree.
    """

    def __init__(self, player_id: str, guild_id: str, uses_library: bool,
                 status: "discord.ui.Button[Any]", draft: Any = None,
                 library: "Optional[Library]" = None) -> None:
        super().__init__(
            label=("Borrow from the library 🏛️" if uses_library
                   else "Bring my own cards 🃏"),
            style=(discord.ButtonStyle.green if uses_library
                   else discord.ButtonStyle.red),
            row=LIBRARY_ROW)
        self.player_id = player_id
        self.guild_id = guild_id
        self.uses_library = uses_library
        self.status = status
        self.draft = draft
        self.library = library

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or str(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This button is not for you.",
                                                    ephemeral=True)
            return
        if not await set_uses_library(self.player_id, self.guild_id, self.uses_library):
            await interaction.response.send_message(
                "⚠️ Couldn't save that just now. Try again.",
                ephemeral=True)
            return
        self.status.label, self.status.style = _status_look(self.uses_library)
        await interaction.response.edit_message(view=self.view)

        text, view = saved_text(self.uses_library), None
        if self.uses_library and self.draft is not None and self.library is not None:
            try:
                said, view = await reserve_and_say(self.player_id, self.draft,
                                                   self.library, reclaim=True)
            except Exception:
                logger.exception("library: saved {}'s yes but could not reserve in {}",
                                 self.player_id, self.draft.session_id)
                said, view = COULD_NOT_CHECK, None
            if said:
                text += "\n" + said
        if view is None:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(text, view=view, ephemeral=True)


def library_setting_buttons(player_id: str, guild_id: str,
                            uses_library: Optional[bool], draft: Any = None,
                            library: "Optional[Library]" = None
                            ) -> "list[discord.ui.Button[Any]]":
    """The library row for a draft's Change Entry / Settings panel."""
    label, style = _status_look(uses_library)
    status: "discord.ui.Button[Any]" = discord.ui.Button(
        label=label, style=style, disabled=True, row=LIBRARY_ROW)
    return [status,
            _SetLibrary(player_id, guild_id, True, status, draft, library),
            _SetLibrary(player_id, guild_id, False, status, draft, library)]


async def library_settings_panel(interaction: discord.Interaction, session_id: Any) -> None:
    """The library row on its own, behind the Library button of a board with no
    Change Entry / Settings panel."""
    # Answered first: the row reads the preference, the reservation and the
    # hold, and Discord gives up on an interaction after three seconds.
    await interaction.response.defer(ephemeral=True)
    try:
        text, view = await _settings_row(interaction, session_id)
    except Exception:
        logger.exception("library: could not open the library row for {} on {}",
                         getattr(interaction.user, "id", None), session_id)
        text, view = COULD_NOT_CHECK, None
    if view is None:
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.followup.send(text, view=view, ephemeral=True)


async def _settings_row(interaction: discord.Interaction, session_id: Any
                        ) -> "tuple[str, Optional[discord.ui.View]]":
    if interaction.user is None or interaction.guild_id is None:
        return "Open this from the draft's board in its server.", None
    player_id, guild_id = str(interaction.user.id), str(interaction.guild_id)
    draft = await DraftSession.get_by_session_id(str(session_id))
    if draft is None:
        return "⚠️ Couldn't find that draft any more.", None
    library = await library_to_ask_about(guild_id, draft.cube, player_id)
    if library is None:
        return ("The library doesn't lend this draft's cube to you, so there's "
                "nothing to set here."), None
    items = library_setting_buttons(player_id, guild_id,
                                    await get_uses_library(player_id, guild_id),
                                    draft=draft, library=library)
    if player_id in {str(p) for p in (draft.sign_ups or {})} \
            and await owes_agreement(draft, player_id):
        items.append(library_agree_button(player_id, str(draft.session_id), draft.cube,
                                          await hold_for(library, player_id) or 0))
    view = discord.ui.View(timeout=600)
    for item in items:
        view.add_item(item)
    return (f"🏛️ Borrowing from the **{library.name}** library. Your answer is "
            f"remembered for drafts here."), view
