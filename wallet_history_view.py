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

from helpers.utils import ui_button, ui_select
from services import wallet_history, wallet_service

CATEGORY_LABELS = {
    None: "All activity",
    wallet_history.DRAFT: "Drafts",
    wallet_history.TOURNAMENT: "Tournaments",
    wallet_history.DEBT: "Debt settlements",
    wallet_history.MTGO: "MTGO deposits & withdrawals",
    wallet_history.TRANSFER: "Player-to-player & other",
}
_LABEL_CATEGORY = {label: category for category, label in CATEGORY_LABELS.items()}

# What "no filter" is called on the wire. Internally it is None -- that is what
# the ledger query wants and what CATEGORY_LABELS is keyed by -- but Discord
# requires every select option to carry a value of 1 to 100 characters, and
# rejects the whole message with 50035 for an empty one. So the wire needs a
# name for it, and this is that name; it is translated at the two places a value
# is written and read, and nothing below the select ever sees it.
#
# Not a category: it must not collide with any member of wallet_history's
# CATEGORIES, or choosing "All activity" would filter to whichever one it named.
ALL_CATEGORIES = "all"


def page_from_footer(text: str | None) -> int:
    """The 0-indexed page a footer describes; 0 when it says nothing useful."""
    if text and text.startswith("Page "):
        try:
            return int(text.split("Page ")[1].split(" of ")[0]) - 1
        except ValueError:
            logger.warning(f"wallet panel: unreadable footer {text!r}")
    return 0


def category_from_footer(text: str | None) -> str | None:
    """The category a footer names, or None for an unfiltered panel."""
    if not text:
        return None
    return _LABEL_CATEGORY.get(text.rsplit(" · ", 1)[-1])


def _option_value(category: str | None) -> str:
    """A category as the select sends it."""
    return category or ALL_CATEGORIES


def _option_category(value: str) -> str | None:
    """...and as the rest of the panel reads it back."""
    return None if value == ALL_CATEGORIES else value


def _selected_value(select: "discord.ui.Select[Any]") -> str:
    """Narrow a Select's chosen value to `str`.

    `Select.values` is typed as a union covering every select kind (channel,
    role, user...) because the type checker cannot see that THIS select is a
    string select -- that's a runtime property of the component, not the
    generic parameter. The isinstance check turns a wrong assumption into a
    clear boundary error here rather than a bad string comparison downstream.
    """
    value = select.values[0]
    if not isinstance(value, str):
        raise TypeError(f"expected a string select value, got {type(value).__name__}")
    return value


async def wallet_embed(guild_id: str, player_id: str, display_name: str,
                       *, page: int = 0,
                       category: str | None = None) -> tuple[discord.Embed, "WalletHistoryView"]:
    """The panel for one holder, and the view whose buttons match it.

    Built together on purpose: the button states are a function of which page
    this embed is, and separating them is how they drift.
    """
    # Two independent reads, each opening its own session, on a path that runs
    # again on every page turn: overlapped rather than queued behind each other.
    wallet, history = await asyncio.gather(
        wallet_service.get_wallet(guild_id, player_id),
        wallet_history.get_history_page(guild_id, player_id, page=page,
                                        category=category),
    )

    embed = discord.Embed(title=f"{display_name}'s Tix Wallet", color=discord.Color.gold())
    embed.add_field(name="Balance", value=f"**{wallet.balance}** tix", inline=True)
    if history.rows:
        lines = await wallet_history.describe_rows(history.rows)
        embed.add_field(name="Recent activity",
                        value=wallet_history.fit_field(lines), inline=False)
        entries = "entry" if history.total == 1 else "entries"
        footer = (f"Page {history.page + 1} of {history.pages} "
                  f"· {history.total} {entries}")
    else:
        footer = "Nothing yet" if category else "No wallet activity yet."
    # The footer is where the filter lives between clicks, so it is appended
    # here rather than in the paged branch: an empty page is still a filtered
    # panel, and the select that reads its own state back off the message must
    # find the category on it either way.
    if category:
        footer += f" · {CATEGORY_LABELS[category]}"
    embed.set_footer(text=footer)
    return embed, WalletHistoryView(history, guild_id, player_id, display_name)


class WalletHistoryView(discord.ui.View):
    """Prev/next over one holder's ledger, plus a category filter.

    The holder is carried on the view, not read off the click. A panel is not
    always the clicker's own wallet -- /wallet-admin show renders someone
    else's -- so rebuilding from `interaction.user` swapped the manager onto
    their own ledger on the first turn of a page or change of filter, under a
    title that still named the player they thought they were reading.

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
        # Built here, per instance, rather than once in the decorator: py-cord
        # stores the decorator's option list on the function at class-definition
        # time and Select.__init__ keeps it BY REFERENCE, so one set of
        # SelectOption objects is shared by every panel in the process and each
        # new view re-marks the default on the views already built. Nothing
        # observes that today only because no await separates constructing a
        # view from sending it at any of the three call sites -- which is not
        # something the code says, and adding a permission check or a
        # display-name lookup there would put one player's filter on another
        # player's panel.
        #
        # The filter itself stays off the view. The footer is where a click
        # reads it back from; a `self.category` alongside it would read as
        # current state while only ever holding what this view was built with.
        self.category_select.options = [
            discord.SelectOption(label=label, value=_option_value(category),
                                 default=category == history.category)
            for category, label in CATEGORY_LABELS.items()]

    async def _turn(self, interaction: discord.Interaction, direction: int) -> None:
        message = interaction.message
        footer = None
        if message and message.embeds:
            embed_footer = message.embeds[0].footer
            footer = embed_footer.text if embed_footer else None
        # Re-queried rather than cached: the ledger may have moved since the
        # panel was sent, and a page of stale money is worse than a slow one.
        embed, view = await wallet_embed(self.guild_id, self.player_id, self.display_name,
                                         page=page_from_footer(footer) + direction,
                                         category=category_from_footer(footer))
        await interaction.response.edit_message(embed=embed, view=view)

    @ui_button(label="◀", style=discord.ButtonStyle.blurple, row=0)
    async def prev_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction):
        await self._turn(interaction, -1)

    @ui_button(label="▶", style=discord.ButtonStyle.blurple, row=0)
    async def next_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction):
        await self._turn(interaction, +1)

    # The options are set in __init__ -- see the note there -- because they
    # carry which filter is active, which is a property of one panel.
    @ui_select(placeholder="Filter by category", row=1)
    async def category_select(self, select: "discord.ui.Select[Any]", interaction: discord.Interaction):
        # The holder comes from the view for the same reason the pager's does:
        # the person filtering is not necessarily the person whose wallet this is.
        # Always page 0: page 3 of everything is not page 3 of drafts.
        embed, view = await wallet_embed(self.guild_id, self.player_id, self.display_name,
                                         category=_option_category(_selected_value(select)))
        await interaction.response.edit_message(embed=embed, view=view)
