"""Who is asking the card library for a deck, from whichever surface they used.

The borrow and return flows in `services.library_loan_actions` take a
`LibraryCaller`. These adapt the two shapes pycord hands a handler -- a slash
command's context, and a component's interaction -- and are the one place
pycord's optional fields are read. The invoking user is always present on
both, so it is asserted; the server is not (both are reachable from a DM), so
it is carried as None for the flows' gate to refuse.

Replies are always private, and are sent exactly as the commands always sent
them: an ephemeral defer, then ephemeral followups.
"""
from typing import Optional

import discord

from helpers.utils import not_none


class ContextCaller:
    """A `LibraryCaller` for a slash command."""

    def __init__(self, ctx: discord.ApplicationContext) -> None:
        self._ctx = ctx
        self.user_id: int = ctx.author.id
        # A guild the bot can see, as the gate has always asked for: an id
        # with no guild behind it is treated as no server at all.
        self.guild_id: Optional[int] = ctx.guild_id if ctx.guild is not None else None

    async def defer(self) -> None:
        await self._ctx.defer(ephemeral=True)

    async def send(self, content: str, *,
                   view: Optional[discord.ui.View] = None) -> None:
        if view is None:
            await self._ctx.followup.send(content, ephemeral=True)
        else:
            await self._ctx.followup.send(content, view=view, ephemeral=True)


class InteractionCaller:
    """A `LibraryCaller` for a button or other component interaction.

    Answers the way a component handler does: `interaction.response.defer`
    to acknowledge, then the interaction's followup webhook.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        # Every component interaction carries the member or user who pressed it.
        self.user_id: int = not_none(interaction.user).id
        self.guild_id: Optional[int] = (interaction.guild_id
                                        if interaction.guild is not None else None)

    async def defer(self) -> None:
        await self._interaction.response.defer(ephemeral=True)

    async def send(self, content: str, *,
                   view: Optional[discord.ui.View] = None) -> None:
        if view is None:
            await self._interaction.followup.send(content, ephemeral=True)
        else:
            await self._interaction.followup.send(content, view=view, ephemeral=True)
