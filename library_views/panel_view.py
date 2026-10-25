"""The buttons on a draft chat's library panel.

They run the flows behind /library borrow and /library return -- the same
code, so a deck collected from the panel goes through every check the command
makes. What the panel adds is scope: it lives in one draft's chat, so it only
acts for a deck from that draft. Anyone else is told so privately, and a borrower whose
deck is from another draft is pointed at the commands, which follow the loan
wherever it came from.
"""
from typing import Any, Literal, Optional

import discord
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.utils import not_none, ui_button
from library_views.callers import InteractionCaller
from models.draft_session import DraftSession
from services.card_lending_service import active_loan
from services.library_loan_actions import borrow, return_deck
from services.library_panel_state import panel_borrowers
from services.library_reminders import session_of

NO_DECK = "📭 You have no deck from this draft."
OTHER_DRAFT = (" Your library deck is from another draft — use `/library borrow` "
               "or `/library return`.")


async def may_use_panel(session_id: str, user_id: str,
                        action: Literal["borrow", "return"]) -> Optional[str]:
    """Why this user may not use this draft's panel, or None to run the command.

    The command runs for a loan from this draft. For borrow it also runs for a
    reserved player with no loan yet, so they hear the command's own "no deck
    waiting" rather than a refusal that reads as if they were never in line.
    """
    loan = await active_loan(user_id)
    if loan is not None:
        if session_of(loan) == session_id:
            return None
        return NO_DECK + OTHER_DRAFT
    if action == "borrow" and any(
            b.user_id == str(user_id) for b in await panel_borrowers(session_id)):
        return None
    return NO_DECK


async def _session_for_channel(channel_id: Any) -> Optional[str]:
    async with AsyncSessionLocal() as s:
        return await s.scalar(
            select(DraftSession.session_id)
            .where(DraftSession.draft_chat_channel == str(channel_id)))


async def _press(interaction: discord.Interaction,
                 action: Literal["borrow", "return"]) -> None:
    # Nothing is deferred here: a refusal is the interaction's one response,
    # and otherwise the flow defers and answers it itself.
    user_id = str(not_none(interaction.user).id)
    session_id = await _session_for_channel(interaction.channel_id)
    refused = (NO_DECK if session_id is None
               else await may_use_panel(session_id, user_id, action))
    if refused is not None:
        await interaction.response.send_message(refused, ephemeral=True)
        return
    caller = InteractionCaller(interaction)
    if action == "borrow":
        await borrow(caller)
    else:
        await return_deck(caller)


class LibraryPanelView(discord.ui.View):
    """Persistent: registered once at startup, so the panel's buttons work on a
    message posted before the last restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui_button(label="Borrow my deck", style=discord.ButtonStyle.primary,
               custom_id="library_panel:borrow")
    async def borrow_button(self, button: "discord.ui.Button[Any]",
                            interaction: discord.Interaction) -> None:
        await _press(interaction, "borrow")

    @ui_button(label="Return my deck", style=discord.ButtonStyle.success,
               custom_id="library_panel:return")
    async def return_button(self, button: "discord.ui.Button[Any]",
                            interaction: discord.Interaction) -> None:
        await _press(interaction, "return")


def panel_view(mode: str) -> Optional[LibraryPanelView]:
    """The buttons the panel shows in `mode`: Borrow while decks are going out,
    Return once they are coming back, none when closed."""
    keep = {"borrow": "library_panel:borrow",
            "return": "library_panel:return"}.get(mode)
    if keep is None:
        return None
    view = LibraryPanelView()
    for item in list(view.children):
        if getattr(item, "custom_id", None) != keep:
            view.remove_item(item)
    return view
