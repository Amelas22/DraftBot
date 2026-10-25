"""The choice offered when the library cannot cover a whole deck."""
from typing import Any, Awaitable, Callable

import discord

from helpers.money_gate import spawn_followup
from helpers.utils import ui_button
from services.card_lending_service import card_lines, trim_to_available


class TakeWhatIsThereView(discord.ui.View):
    """Offer the partial deck when the library cannot cover all of it.

    Deliberately asked BEFORE queueing for the serve: the library trades with
    one person at a time, and holding that place while a human decides would
    block everyone else behind a dialog nobody else can see.

    `collect` starts the borrow for the agreed subset. Handed in rather than
    imported, because the borrow flow is what shows this view.
    """

    def __init__(self, guild_id: int, loan_id: Any,
                 collect: "Callable[[list[dict[str, Any]]], Awaitable[None]]",
                 timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.guild_id, self.loan_id, self.collect = guild_id, loan_id, collect

    @ui_button(label="Take what's available", style=discord.ButtonStyle.primary)
    async def take(self, button: "discord.ui.Button[Any]",
                   interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        trimmed = await trim_to_available(self.guild_id, self.loan_id)
        if not trimmed:
            await interaction.followup.send(
                "📭 There's nothing left of that deck to lend right now.", ephemeral=True)
            self.stop()
            return
        await interaction.followup.send(f"👍 Borrowing what's there:\n{card_lines(trimmed)}",
                                        ephemeral=True)
        self.stop()
        # The agreed subset rides along as an argument. A second click cannot
        # change what the first one is moving, because the offer is only written
        # down as part of the trade that carries it.
        spawn_followup("card-library borrow", self.collect(trimmed))

    @ui_button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: "discord.ui.Button[Any]",
                     interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "👌 Left your deck as it is — nothing borrowed.", ephemeral=True)
        self.stop()
