"""The buttons on the library's DMs.

The library's flows refuse outside a server, so a DM that said "use
/library borrow" or "/library return" used to send the player hunting for the
server first. These buttons sit on the DM itself. They are persistent, so a
DM sent before the last restart still works.
"""
from typing import Any, Literal, Optional

import discord

from helpers.utils import keep_only, not_none, ui_button
from library_views.callers import InteractionCaller
from services.card_lending_service import active_loan
from services.library_loan_actions import LOAN_MESSAGES, borrow, return_deck

Action = Literal["borrow", "return"]


def _loan_guild_id(loan: Any) -> Optional[int]:
    """The server the loan belongs to; None for no loan or an unparseable id."""
    if loan is None:
        return None
    try:
        return int(loan.guild_id)
    except (TypeError, ValueError):
        return None


# action -> (flow, refusal message key); resolved at press time so the
# module-level names stay patchable.
_ACTIONS = {"borrow": ("borrow", "no_loan"), "return": ("return_deck", "not_borrowed")}


async def _press(interaction: discord.Interaction, action: Action) -> None:
    """A DM carries no server, so the flow runs for the server the presser's
    loan belongs to, wherever the press happened."""
    flow_name, refusal_key = _ACTIONS[action]
    # Nothing is deferred before a refusal: it is the interaction's one
    # response, and otherwise the flow defers and answers it itself.
    loan = await active_loan(str(not_none(interaction.user).id))
    guild_id = _loan_guild_id(loan)
    if guild_id is None:
        await interaction.response.send_message(LOAN_MESSAGES[refusal_key], ephemeral=True)
        return
    flow = {"borrow": borrow, "return_deck": return_deck}[flow_name]
    await flow(InteractionCaller(interaction, guild_id=guild_id))


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


# Buttons are stateless and py-cord keeps every sent persistent view in its
# store until restart, so one shared view per face is all that is needed.
_VIEWS: dict[str, LibraryDmView] = {}


def dm_view(action: Action) -> LibraryDmView:
    """The view carrying only the button for `action`."""
    if action not in _VIEWS:
        _VIEWS[action] = keep_only(
            LibraryDmView(), [f"library_dm:{action}"])
    return _VIEWS[action]
