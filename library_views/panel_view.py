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

Return is on both faces, so a deck can go back between matches and come out
again from the same place; Borrow stays on the return face for dead rubbers.
"""
from typing import Any, Literal, Optional

import discord
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.permissions import is_bot_manager_interaction
from helpers.utils import keep_only, not_none, ui_button
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


Action = Literal["borrow", "return", "decline", "open", "sub"]


async def may_use_panel(session_id: str, user_id: str,
                        action: Action) -> Optional[str]:
    """Why this user may not use this draft's panel, or None to run the command.

    The command runs for a loan from this draft. For borrow and open it also runs for
    anybody in the draft with no loan yet -- the reserved, and a drafter who
    changed their mind -- so the borrow flow decides whether they can have a
    deck and says why not in its own words.

    Sub is for anybody seated on a team in the draft, whatever they hold:
    Confirm's own checks say what stands in the way.
    """
    if action == "sub":
        return None if await _seated_in(session_id, user_id) else NO_DECK
    loan = await active_loan(user_id)
    if loan is not None:
        if session_of(loan) == session_id:
            return None
        return NO_DECK + OTHER_DRAFT
    if action in ("borrow", "open") and (
            await _drafted_in(session_id, user_id)
            or any(b.user_id == str(user_id)
                   for b in await panel_borrowers(session_id))):
        return None
    return NO_DECK


async def _drafted_in(session_id: str, user_id: str) -> bool:
    draft = await DraftSession.get_by_session_id(session_id)
    return draft is not None and str(user_id) in {str(p) for p in (draft.sign_ups or {})}


async def _seated_in(session_id: str, user_id: str) -> bool:
    draft = await DraftSession.get_by_session_id(session_id)
    return draft is not None and str(user_id) in {
        str(p) for p in [*(draft.team_a or []), *(draft.team_b or [])]}


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
    is_manager = False
    if action == "sub" and refused is not None \
            and await is_bot_manager_interaction(interaction):
        refused, is_manager = None, True
    if refused is not None:
        await interaction.response.send_message(refused, ephemeral=True)
        return
    if some:
        # The form is the press's one response, so nothing was deferred.
        await interaction.response.send_modal(SomeCardsModal(session_id))
        return
    if action == "open":
        # Imported here: the panel module imports this one.
        from services.library_panel import open_panel
        if not await open_panel(session_id, interaction):
            await interaction.response.send_message(
                "📭 This draft's library decks are closed.", ephemeral=True)
        return
    if action == "sub":
        # Imported here: the picker imports the panel module, which imports this one.
        from library_views.sub_picker import SubPickerView
        await interaction.response.send_message(
            "Who can't play, and who is subbing?",
            view=await SubPickerView.build(session_id, user_id, is_manager),
            ephemeral=True)
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

    @ui_button(label="Borrow only some cards", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:some")
    async def some_button(self, button: "discord.ui.Button[Any]",
                          interaction: discord.Interaction) -> None:
        await _press(interaction, "borrow", some=True)

    @ui_button(label="Return my deck", style=discord.ButtonStyle.success,
               custom_id="library_panel:return")
    async def return_button(self, button: "discord.ui.Button[Any]",
                            interaction: discord.Interaction) -> None:
        await _press(interaction, "return")

    @ui_button(label="I don't need my deck", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:decline")
    async def decline_button(self, button: "discord.ui.Button[Any]",
                             interaction: discord.Interaction) -> None:
        await _press(interaction, "decline")

    @ui_button(label="Use the library", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:open")
    async def open_button(self, button: "discord.ui.Button[Any]",
                          interaction: discord.Interaction) -> None:
        await _press(interaction, "open")

    @ui_button(label="Sub for a player", style=discord.ButtonStyle.secondary,
               custom_id="library_panel:sub")
    async def sub_button(self, button: "discord.ui.Button[Any]",
                         interaction: discord.Interaction) -> None:
        await _press(interaction, "sub")


def panel_view(mode: str) -> Optional[LibraryPanelView]:
    """The buttons the panel shows in `mode`: Borrow, Borrow only some cards,
    Return and "I don't need my deck" while decks are going out; the same
    without the decline once they are coming back, so a dead rubber can take
    a few cards out again; Sub for a player last on both; one "Use the
    library" button on the ask face; none when closed."""
    keep = {"borrow": ("library_panel:borrow", "library_panel:some",
                       "library_panel:return", "library_panel:decline",
                       "library_panel:sub"),
            "return": ("library_panel:borrow", "library_panel:some",
                       "library_panel:return", "library_panel:sub"),
            "ask": ("library_panel:open",)}.get(mode)
    if keep is None:
        return None
    return keep_only(LibraryPanelView(), keep)
