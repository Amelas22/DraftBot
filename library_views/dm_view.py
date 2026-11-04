"""The buttons on the library's DMs.

The library's flows refuse outside a server, so a DM that said "use
/library borrow" or "/library return" used to send the player hunting for the
server first. These buttons sit on the DM itself. They are persistent, so a
DM sent before the last restart still works.
"""
from typing import Any, Literal

import discord

from helpers.utils import ui_button

Action = Literal["borrow", "return"]

NO_DECK = "📭 You have no deck to {verb} right now."


async def _press(interaction: discord.Interaction, action: Action) -> None:
    await interaction.response.send_message(
        NO_DECK.format(verb=action), ephemeral=True)


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
