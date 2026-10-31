"""The buttons on a draft chat's library panel.

They run the flows behind /library borrow and /library return -- the same
code, so a deck collected from the panel goes through every check the command
makes. What the panel adds is scope: it lives in one draft's chat, so it only
acts for a deck from that draft. Anyone else is told so privately, and a borrower whose
deck is from another draft is pointed at the commands, which follow the loan
wherever it came from.

Borrow is for every drafter, not only the ones the library set a deck aside
for: somebody who said no before the draft fired presses it to ask for their
deck after all (library_loan_actions.borrow). "I don't need my deck" hands an
uncollected one back.
"""
from typing import Any, Literal, Optional

import discord
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.utils import not_none, ui_button
from library_views.callers import InteractionCaller
from library_views.some_cards import SomeCardsModal
from models.draft_session import DraftSession
from services.card_lending_service import active_loan
from services.library_loan_actions import borrow, decline, return_deck
from services.library_panel_state import panel_borrowers
from services.library_reminders import session_of

NO_DECK = "📭 You have no deck from this draft."
OTHER_DRAFT = (" Your library deck is from another draft — use `/library borrow` "
               "or `/library return`.")


Action = Literal["borrow", "return", "decline"]


async def may_use_panel(session_id: str, user_id: str,
                        action: Action) -> Optional[str]:
    """Why this user may not use this draft's panel, or None to run the command.

    The command runs for a loan from this draft. For borrow it also runs for
    anybody in the draft with no loan yet -- the reserved, and a drafter who
    changed their mind -- so the borrow flow decides whether they can have a
    deck and says why not in its own words.
    """
    loan = await active_loan(user_id)
    if loan is not None:
        if session_of(loan) == session_id:
            return None
        return NO_DECK + OTHER_DRAFT
    if action == "borrow" and (await _drafted_in(session_id, user_id) or any(
            b.user_id == str(user_id) for b in await panel_borrowers(session_id))):
        return None
    return NO_DECK


async def _drafted_in(session_id: str, user_id: str) -> bool:
    draft = await DraftSession.get_by_session_id(session_id)
    return draft is not None and str(user_id) in {str(p) for p in (draft.sign_ups or {})}


async def _session_for_channel(channel_id: Any) -> Optional[str]:
    async with AsyncSessionLocal() as s:
        return await s.scalar(
            select(DraftSession.session_id)
            .where(DraftSession.draft_chat_channel == str(channel_id)))


async def _press(interaction: discord.Interaction, action: Action,
                 some: bool = False) -> None:
    # Nothing is deferred here: a refusal is the interaction's one response,
    # and otherwise the flow defers and answers it itself.
    user_id = str(not_none(interaction.user).id)
    session_id = await _session_for_channel(interaction.channel_id)
    if session_id is None:
        await interaction.response.send_message(NO_DECK, ephemeral=True)
        return
    refused = await may_use_panel(session_id, user_id, action)
    if refused is not None:
        await interaction.response.send_message(refused, ephemeral=True)
        return
    if some:
        # The form is the press's one response, so nothing was deferred.
        await interaction.response.send_modal(SomeCardsModal(session_id))
        return
    caller = InteractionCaller(interaction)
    if action == "borrow":
        await borrow(caller, session_id)
    elif action == "decline":
        await decline(caller, session_id)
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

    @ui_button(label="Borrow only some cards", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:some")
    async def some_button(self, button: "discord.ui.Button[Any]",
                          interaction: discord.Interaction) -> None:
        await _press(interaction, "borrow", some=True)

    @ui_button(label="I don't need my deck", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:decline")
    async def decline_button(self, button: "discord.ui.Button[Any]",
                             interaction: discord.Interaction) -> None:
        await _press(interaction, "decline")


def panel_view(mode: str) -> Optional[LibraryPanelView]:
    """The buttons the panel shows in `mode`: Borrow, Borrow only some cards
    and "I don't need my deck" while decks are going out, Return once they are coming back, none
    when closed."""
    keep = {"borrow": ("library_panel:borrow", "library_panel:some",
                       "library_panel:decline"),
            "return": ("library_panel:return",)}.get(mode)
    if keep is None:
        return None
    view = LibraryPanelView()
    for item in list(view.children):
        if getattr(item, "custom_id", None) not in keep:
            view.remove_item(item)
    return view
