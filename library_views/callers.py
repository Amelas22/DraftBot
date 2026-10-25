"""Who is asking the card library for a deck, from whichever surface they used.

The borrow and return flows in `services.library_loan_actions` take a
`LibraryCaller`. This adapts a slash command's context, and is the one place
its optional fields are read. The server is optional for real -- the commands
are reachable from a DM -- so it is carried as None for the flows' gate to
refuse.

Replies are always private, and are sent exactly as the commands always sent
them: an ephemeral defer, then ephemeral followups.
"""
from typing import Optional

import discord


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

