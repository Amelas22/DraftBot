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


def context_guild_id(ctx: discord.ApplicationContext) -> Optional[int]:
    """The server a command was run in, as the library's gate reads it.

    A guild the bot can see: an id with no guild behind it is treated as no
    server at all.
    """
    return ctx.guild_id if ctx.guild is not None else None


class ContextCaller:
    """A `LibraryCaller` for a slash command."""

    def __init__(self, ctx: discord.ApplicationContext) -> None:
        self._ctx = ctx
        self.user_id: int = ctx.author.id
        self.guild_id: Optional[int] = context_guild_id(ctx)

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

    Acknowledged with a visible, ephemeral "thinking…" and answered on the
    interaction's followup webhook, as a slash command is. The visible part
    has to be asked for: pycord defers a component invisibly by default, as a
    silent update to the message the button sits on, and ignores `ephemeral`
    when it does -- so the press would show nothing while the flow works.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        # Every component interaction carries the member or user who pressed it.
        self.user_id: int = not_none(interaction.user).id
        self.guild_id: Optional[int] = (interaction.guild_id
                                        if interaction.guild is not None else None)

    async def defer(self) -> None:
        await self._interaction.response.defer(ephemeral=True, invisible=False)

    async def send(self, content: str, *,
                   view: Optional[discord.ui.View] = None) -> None:
        if view is None:
            await self._interaction.followup.send(content, ephemeral=True)
        else:
            await self._interaction.followup.send(content, view=view, ephemeral=True)
