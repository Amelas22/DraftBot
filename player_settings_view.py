"""The /settings message: an embed of a player's settings and a button for each.

Each click flips the stored value (player_settings.flip), reloads, and redraws
this same message. Row 0 holds the Drafting buttons: the stake cap, offered
only on a money server, and the card library, offered only where the player
may borrow. A new cap, or the library switched on, is carried into the
player's live sign-ups.
"""
import asyncio
import weakref
from typing import Any

import discord
from loguru import logger
from typing_extensions import override

from helpers.money_gate import spawn_followup
from helpers.utils import not_none, ui_button
from player_settings import (Setting, Settings, apply_cap_to_live_drafts,
                             apply_library_to_live_drafts, flip, live_result_text,
                             load_settings)
from utils import redraw_leaderboard

CAP_LABEL = "Cap my entry to my share of my team"
LIBRARY_LABEL = "Offer me a library deck"
LIBRARY_NOT_SET = "○ Not set (you'll be asked at sign-up)"
DMS_LABEL = "DM me when a ready check starts"
HIDE_LABEL = "Hide me from the Skill Rating Ladder"
ANONYMOUS_LABEL = "Show me as Anonymous on donation pages"

NOT_YOURS = "These buttons are not for you."
SAVE_FAILED = "⚠️ Couldn't save that just now. Try again."
EXPIRED = "These buttons have expired. Run `/settings` again."
NOTHING_AFFECTED = "No draft you're signed up for right now was affected."
LIVE_FAILED = ("⚠️ Saved, but I couldn't update the drafts you're signed up for. "
               "Use each draft's **Change Entry / Settings** button.")

# One lock per (player, server): two quick clicks run one after the other, so
# a flip and its live-draft writes can't interleave with the next click's and
# leave the saved preference disagreeing with a live entry. Weak values: a
# lock goes once no click holds or waits on it.
_locks: "weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock]" = (
    weakref.WeakValueDictionary())


def _lock_for(player_id: str, guild_id: str) -> asyncio.Lock:
    lock = _locks.get((player_id, guild_id))
    if lock is None:
        lock = _locks[(player_id, guild_id)] = asyncio.Lock()
    return lock


def _line(on: bool, label: str) -> str:
    return f"● On  {label}" if on else f"○ Off  {label}"


def _groups(settings: Settings) -> list[tuple[str, list[str]]]:
    """Each embed field: its group name and its lines. Empty groups are left out."""
    drafting: list[str] = []
    if settings.stake_cap is not None:
        drafting.append(_line(settings.stake_cap, CAP_LABEL))
    if settings.library_offered:
        drafting.append(f"{LIBRARY_NOT_SET}  {LIBRARY_LABEL}" if settings.library is None
                        else _line(settings.library, LIBRARY_LABEL))
    return [
        (name, lines) for name, lines in (
            ("Drafting", drafting),
            ("Notifications", [_line(settings.dms, DMS_LABEL)]),
            ("Privacy", [_line(settings.hide_from_ladder, HIDE_LABEL),
                         _line(settings.anonymous, ANONYMOUS_LABEL)]),
        ) if lines
    ]


def settings_embed(guild_name: str, settings: Settings) -> discord.Embed:
    embed = discord.Embed(title=f"⚙️ Your settings: {guild_name}",
                          color=discord.Color.blurple())
    for name, lines in _groups(settings):
        embed.add_field(name=name, value="\n".join(lines), inline=False)
    embed.set_footer(text="Settings are per server.")
    return embed


def _style(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.success if on else discord.ButtonStyle.secondary


class SettingsView(discord.ui.View):
    def __init__(self, player_id: str, guild_id: str, guild_name: str,
                 settings: Settings) -> None:
        super().__init__(timeout=600)
        self.player_id = player_id
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.settings = settings
        # The latest interaction on this message, so a timeout can edit it:
        # set by the command, then replaced by each click, because every
        # click restarts the timeout and outlives the opener's 15-minute token.
        self.opener: discord.Interaction | None = None
        if settings.stake_cap is None:
            self.remove_item(self.cap_button)
        if not settings.library_offered:
            self.remove_item(self.library_button)
        self._restyle()

    def _restyle(self) -> None:
        self.cap_button.style = _style(bool(self.settings.stake_cap))
        self.library_button.style = _style(bool(self.settings.library))
        self.dms_button.style = _style(self.settings.dms)
        self.hide_button.style = _style(self.settings.hide_from_ladder)
        self.anonymous_button.style = _style(self.settings.anonymous)

    async def _toggle(self, interaction: discord.Interaction, setting: Setting) -> None:
        if str(not_none(interaction.user).id) != self.player_id:
            await interaction.response.send_message(NOT_YOURS, ephemeral=True)
            return
        await interaction.response.defer()
        self.opener = interaction
        async with _lock_for(self.player_id, self.guild_id):
            new = await flip(self.player_id, self.guild_id, setting)
            if new is None:
                await interaction.followup.send(SAVE_FAILED, ephemeral=True)
                return
            # Side effects first, so a failed edit of this message can't skip them.
            if setting is Setting.HIDE_FROM_LADDER:
                self._redraw_ladder(interaction)
            live = await self._carry_into_live_drafts(interaction, setting, new)
        self.settings = await load_settings(self.player_id, self.guild_id)
        self._restyle()
        await interaction.edit_original_response(
            embed=settings_embed(self.guild_name, self.settings), view=self)
        for text, view in live:
            if view is None:
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.followup.send(text, view=view, ephemeral=True)

    def _redraw_ladder(self, interaction: discord.Interaction) -> None:
        """Redraw the Skill Rating Ladder board in the background."""
        spawn_followup("settings ladder redraw",
                       redraw_leaderboard(interaction.client, self.guild_id, "sr_ladder"))

    async def _carry_into_live_drafts(self, interaction: discord.Interaction,
                                      setting: Setting, new: bool
                                      ) -> list[tuple[str, discord.ui.View | None]]:
        """Carry a saved cap, or the library switched on, into the player's
        live sign-ups; the follow-ups to send, each with its view if any.

        The preference is saved by now, so a failure here must not escape.
        """
        try:
            if setting is Setting.STAKE_CAP:
                text = live_result_text(await apply_cap_to_live_drafts(
                    interaction.client, self.player_id, self.guild_id, new))
                return [(text or NOTHING_AFFECTED, None)]
            if setting is Setting.LIBRARY and new:
                return (await apply_library_to_live_drafts(self.player_id, self.guild_id)
                        or [(NOTHING_AFFECTED, None)])
        except Exception:
            logger.exception("settings: saved {} for {} but couldn't update their live drafts",
                             setting.value, self.player_id)
            return [(LIVE_FAILED, None)]
        return []

    @ui_button(label="Entry cap", row=0)
    async def cap_button(self, _button: discord.ui.Button[Any],
                         interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.STAKE_CAP)

    @ui_button(label="Card library", row=0)
    async def library_button(self, _button: discord.ui.Button[Any],
                             interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.LIBRARY)

    @ui_button(label="Ready-check DMs", row=1)
    async def dms_button(self, _button: discord.ui.Button[Any],
                         interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.DMS)

    @ui_button(label="Hide from ladder", row=2)
    async def hide_button(self, _button: discord.ui.Button[Any],
                          interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.HIDE_FROM_LADDER)

    @ui_button(label="Anonymous donor", row=2)
    async def anonymous_button(self, _button: discord.ui.Button[Any],
                               interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.ANONYMOUS)

    @override
    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.opener is None:
            return
        try:
            await self.opener.edit_original_response(content=EXPIRED, view=self)
        except discord.HTTPException:
            pass
