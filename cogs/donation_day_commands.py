"""Donation day commands.

/donation_day setup      -- a bot manager starts an event and publishes its page.
/donation_day end        -- publish a final snapshot and deactivate the event.
/donation_day status     -- totals so far and the page link.
/donation_day paid_cash  -- a loser marks their donation debt to DraftBot as
paid in cash (GitHub Sponsors or otherwise). On trust; every mark is logged.
"""
from datetime import datetime

import discord
from discord.commands import SlashCommandGroup, option
from discord.ext import commands

from helpers.digital_ocean_helper import DigitalOceanHelper
from helpers.permissions import has_bot_manager_role
from helpers.utils import not_none
from services.donation_day import (Event, build_payload, donation_day_running, get_event,
                                   save_event, slugify)
from services.donation_day_discord import get_state, save_state, state_for_setup
from services.donation_day_publisher import publish_event
from services.donation_service import mark_paid_cash


MAX_NAME_LENGTH = 100


def new_event(name: str, goal: int, starts: str | None, now: datetime,
              existing: Event | None = None) -> Event:
    """An active event. `starts` is 'YYYY-MM-DD HH:MM' in server time, or now.

    Re-running setup on a running event (`existing`) without `starts` keeps its start,
    so the totals window doesn't silently move.
    """
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"The name must be at most {MAX_NAME_LENGTH} characters.")
    if goal <= 0:
        raise ValueError("The goal must be a positive number of dollars.")
    if starts:
        start_at = datetime.strptime(starts, "%Y-%m-%d %H:%M").isoformat(timespec="minutes")
    elif existing is not None and existing.get("active"):
        start_at = existing["starts_at"]
    else:
        start_at = now.isoformat(timespec="minutes")
    return {"name": name, "goal_usd": goal, "starts_at": start_at,
            "page_key": slugify(name), "active": True}


class DonationDayCommands(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    donation_day = SlashCommandGroup("donation_day", "Donation day")

    @donation_day.command(name="paid_cash",
                          description="Mark your donation-draft debt to DraftBot as paid in cash")
    async def paid_cash(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        amount = await mark_paid_cash(str(not_none(ctx.guild).id), str(ctx.author.id),
                                      str(not_none(self.bot.user).id))
        if amount:
            await ctx.followup.send(
                f"Thank you! {amount} tix of donation debt to DraftBot is marked "
                f"paid in cash.", ephemeral=True)
        else:
            await ctx.followup.send(
                "You have no donation debt to DraftBot outstanding.", ephemeral=True)

    @donation_day.command(name="setup", description="Start a donation day and publish its page")
    @has_bot_manager_role()
    @option("name", str, description="Event name, shown on the page")
    @option("goal", int, description="Goal in dollars (1 tix = $1)")
    @option("starts", str, description="Start as YYYY-MM-DD HH:MM (server time); default now",
            required=False, default=None)
    @option("receipts_channel", discord.TextChannel,
            description="Where each donation is announced", required=False, default=None)
    @option("leaderboard_channel", discord.TextChannel,
            description="Where the live leaderboard is kept", required=False, default=None)
    async def setup_event(self, ctx: discord.ApplicationContext, name: str, goal: int,
                          starts: str | None = None,
                          receipts_channel: discord.TextChannel | None = None,
                          leaderboard_channel: discord.TextChannel | None = None) -> None:
        await ctx.defer(ephemeral=True)
        guild_id = str(not_none(ctx.guild).id)
        previous = get_event(guild_id)
        try:
            event = new_event(name, goal, starts, datetime.now(), existing=previous)
        except ValueError as e:
            await ctx.followup.send(f"Couldn't set that up: {e}", ephemeral=True)
            return
        save_event(guild_id, event)
        save_state(guild_id, state_for_setup(
            get_state(guild_id), bool(previous and previous.get("active")),
            str(receipts_channel.id) if receipts_channel else None,
            str(leaderboard_channel.id) if leaderboard_channel else None))
        url = await publish_event(guild_id, event, str(not_none(self.bot.user).id), bot=self.bot)
        when = ("is live" if donation_day_running(event, datetime.now())
                else f"is scheduled for {event['starts_at'].replace('T', ' ')}")
        lines = [f"**{name}** {when}. Page: {url or 'upload failed; it retries every 3 minutes'}"]
        if receipts_channel:
            lines.append(f"Receipts: {receipts_channel.mention}")
        if leaderboard_channel:
            lines.append(f"Leaderboard: {leaderboard_channel.mention}")
        await ctx.followup.send("\n".join(lines), ephemeral=True)

    @donation_day.command(name="end", description="Publish a final snapshot and end the donation day")
    @has_bot_manager_role()
    @option("force", bool, description="End even if the final upload fails",
            required=False, default=False)
    async def end_event(self, ctx: discord.ApplicationContext, force: bool = False) -> None:
        await ctx.defer(ephemeral=True)
        guild_id = str(not_none(ctx.guild).id)
        event = get_event(guild_id)
        if not event or not event.get("active"):
            await ctx.followup.send("No donation day is running.", ephemeral=True)
            return
        url = await publish_event(guild_id, event, str(not_none(self.bot.user).id), bot=self.bot)
        if url is None and not force:
            # Stay active: the 3-minute loop keeps retrying, and ending now would
            # freeze the last published page short of the final totals.
            await ctx.followup.send(
                f"The final upload failed, so **{event['name']}** is still running and "
                f"keeps retrying every 3 minutes. Once it recovers, run `/donation_day end` again, "
                f"or run it with force:True to end anyway.",
                ephemeral=True)
            return
        save_event(guild_id, {**event, "active": False})
        final = f"Final page: {url}" if url else "The final upload FAILED (ended by force); the last published page stays up."
        await ctx.followup.send(f"**{event['name']}** has ended. {final}",
                                ephemeral=True)

    @donation_day.command(name="status", description="Donation day totals and the page link")
    async def status(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        guild_id = str(not_none(ctx.guild).id)
        event = get_event(guild_id)
        if not event:
            await ctx.followup.send("No donation day has been set up here.", ephemeral=True)
            return
        payload = await build_payload(guild_id, event, str(not_none(self.bot.user).id))
        url = DigitalOceanHelper().get_public_url(f"donations/{event['page_key']}/index.html")
        state = ("running" if donation_day_running(event, datetime.now())
                 else f"scheduled for {event['starts_at'].replace('T', ' ')}" if event.get("active")
                 else "ended")
        await ctx.followup.send(
            f"**{event['name']}** ({state}): ${payload['raised']} raised of "
            f"${event['goal_usd']}, ${payload['pledged']} pledged.\n{url}", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DonationDayCommands(bot))
