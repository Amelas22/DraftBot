"""The wallet panel's Discord glue: the embed and its pager.

Top-level module (like match_control_view.py) so the wallet cog can import it
without an import cycle through cogs/. The lines themselves come from
services/wallet_history.py, which stays free of Discord imports.

Panels are ephemeral, so this view is deliberately not persistent: an ephemeral
message can only be edited through the interaction token that created it, and
that token expires and does not survive a restart. The current page therefore
lives in the embed's footer -- the same trick debt_views/settle_views.py uses --
so a click can recover where it is from the message itself, and an expired panel
degrades to "run /wallet show again" rather than to a broken button.
"""
import asyncio
from typing import Any

import discord
from loguru import logger

from helpers.utils import ui_button
from services import wallet_history, wallet_service


def page_from_footer(text: str | None) -> int:
    """The 0-indexed page a footer describes; 0 when it says nothing useful."""
    if text and text.startswith("Page "):
        try:
            return int(text.split("Page ")[1].split(" of ")[0]) - 1
        except ValueError:
            logger.warning(f"wallet panel: unreadable footer {text!r}")
    return 0


async def wallet_embed(guild_id: str, player_id: str, display_name: str,
                       *, page: int = 0) -> tuple[discord.Embed, "WalletHistoryView"]:
    """The panel for one holder, and the view whose buttons match it.

    Built together on purpose: the button states are a function of which page
    this embed is, and separating them is how they drift.
    """
    # Two independent reads, each opening its own session, on a path that runs
    # again on every page turn: overlapped rather than queued behind each other.
    wallet, history = await asyncio.gather(
        wallet_service.get_wallet(guild_id, player_id),
        wallet_history.get_history_page(guild_id, player_id, page=page),
    )

    embed = discord.Embed(title=f"{display_name}'s Tix Wallet", color=discord.Color.gold())
    embed.add_field(name="Balance", value=f"**{wallet.balance}** tix", inline=True)
    if history.rows:
        lines = await wallet_history.describe_rows(history.rows)
        embed.add_field(name="Recent activity",
                        value=wallet_history.fit_field(lines), inline=False)
        entries = "entry" if history.total == 1 else "entries"
        embed.set_footer(text=f"Page {history.page + 1} of {history.pages} "
                              f"· {history.total} {entries}")
    else:
        embed.set_footer(text="No wallet activity yet.")
    return embed, WalletHistoryView(history, guild_id, player_id, display_name)


class WalletHistoryView(discord.ui.View):
    """Prev/next over one holder's ledger.

    The holder is carried on the view, not read off the click. A panel is not
    always the clicker's own wallet -- /wallet-admin show renders someone
    else's -- so rebuilding from `interaction.user` swapped the manager onto
    their own ledger on the first turn of a page, under a title that still
    named the player they thought they were reading.

    No author check: an ephemeral message is only ever visible to the person
    whose interaction created it.
    """

    def __init__(self, history: wallet_history.HistoryPage,
                 guild_id: str, player_id: str, display_name: str):
        # Finite, because this view is not persistent: py-cord's ViewStore only
        # evicts a view once it has finished, and a `timeout=None` view never
        # does, so every /wallet show and every page turn would leave its items
        # registered for as long as the process ran. 10 minutes outlasts a
        # browse and matches what every other ephemeral view here uses. Once it
        # expires the buttons stop responding and the player runs /wallet show
        # again -- an ephemeral panel cannot be edited past its token anyway.
        super().__init__(timeout=600)  # 10 minute timeout
        self.guild_id = guild_id
        self.player_id = player_id
        self.display_name = display_name
        self.prev_button.disabled = history.page <= 0
        self.next_button.disabled = history.page >= history.pages - 1

    async def _turn(self, interaction: discord.Interaction, direction: int) -> None:
        message = interaction.message
        footer = None
        if message and message.embeds:
            embed_footer = message.embeds[0].footer
            footer = embed_footer.text if embed_footer else None
        # Re-queried rather than cached: the ledger may have moved since the
        # panel was sent, and a page of stale money is worse than a slow one.
        embed, view = await wallet_embed(self.guild_id, self.player_id, self.display_name,
                                         page=page_from_footer(footer) + direction)
        await interaction.response.edit_message(embed=embed, view=view)

    @ui_button(label="◀", style=discord.ButtonStyle.blurple, row=0)
    async def prev_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction):
        await self._turn(interaction, -1)

    @ui_button(label="▶", style=discord.ButtonStyle.blurple, row=0)
    async def next_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction):
        await self._turn(interaction, +1)
