"""The buttons on the library's DMs.

The library's flows refuse outside a server, so a DM that said "use
/library borrow" or "/library return" used to send the player hunting for the
server first. These buttons sit on the DM itself. They are persistent, so a
DM sent before the last restart still works.
"""
from typing import Any, Literal

import discord

from helpers.utils import not_none, ui_button
from library_views.callers import InteractionCaller
from services.card_lending_service import active_loan
from services.library_loan_actions import LOAN_MESSAGES, borrow, return_deck

Action = Literal["borrow", "return"]


async def _press(interaction: discord.Interaction, action: Action) -> None:
    """A DM carries no server, so the flow runs for the server the presser's
    loan belongs to, wherever the press happened."""
    # Nothing is deferred before a refusal: it is the interaction's one
    # response, and otherwise the flow defers and answers it itself.
    loan = await active_loan(str(not_none(interaction.user).id))
    guild_id = None
    if loan is not None:
        try:
            guild_id = int(loan.guild_id)
        except (TypeError, ValueError):
            pass
    if guild_id is None:
        refusal = LOAN_MESSAGES["not_borrowed" if action == "return" else "no_loan"]
        await interaction.response.send_message(refusal, ephemeral=True)
        return
    caller = InteractionCaller(interaction, guild_id=guild_id)
    if action == "borrow":
        await borrow(caller)
    else:
        await return_deck(caller)


class LibraryDmView(discord.ui.View):
    """Persistent: registered once at startup."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui_button(label="Borrow my deck", style=discord.ButtonStyle.primary,
               custom_id="library_dm:borrow")
    async def borrow_button(self, button: "discord.ui.Button[Any]",
                            interaction: discord.Interaction) -> None:
        await _press(interaction, "borrow")

    @ui_button(label="Return my deck", style=discord.ButtonStyle.success,
               custom_id="library_dm:return")
    async def return_button(self, button: "discord.ui.Button[Any]",
                            interaction: discord.Interaction) -> None:
        await _press(interaction, "return")


def dm_view(action: Action) -> LibraryDmView:
    """The view carrying only the button for `action`."""
    view = LibraryDmView()
    for item in list(view.children):
        if getattr(item, "custom_id", None) != f"library_dm:{action}":
            view.remove_item(item)
    return view
