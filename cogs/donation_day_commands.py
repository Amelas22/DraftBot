"""Donation day commands.

/donation_day setup      -- a bot manager starts an event and publishes its page.
/donation_day end        -- publish a final snapshot and deactivate the event.
/donation_day status     -- totals so far and the page link.
/donation_day anonymous  -- a player opts to show as Anonymous on the page.
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
from preference_service import set_donation_anonymous
from services.donation_day import Event, build_payload, get_event, save_event, slugify
from services.donation_day_publisher import publish_event
from services.donation_service import mark_paid_cash


def new_event(name: str, goal: int, starts: str | None, now: datetime) -> Event:
    """A fresh, active event. `starts` is 'YYYY-MM-DD HH:MM' in server time, or now."""
    if goal <= 0:
        raise ValueError("The goal must be a positive number of dollars.")
    start = datetime.strptime(starts, "%Y-%m-%d %H:%M") if starts else now
    return {"name": name, "goal_usd": goal,
            "starts_at": start.isoformat(timespec="minutes"),
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
    async def setup_event(self, ctx: discord.ApplicationContext, name: str, goal: int,
                          starts: str | None = None) -> None:
        await ctx.defer(ephemeral=True)
        try:
            event = new_event(name, goal, starts, datetime.now())
        except ValueError as e:
            await ctx.followup.send(f"Couldn't set that up: {e}", ephemeral=True)
            return
        guild_id = str(not_none(ctx.guild).id)
        save_event(guild_id, event)
        url = await publish_event(guild_id, event, str(not_none(self.bot.user).id))
        await ctx.followup.send(
            f"**{name}** is live. Page: {url or 'upload failed; it retries every 3 minutes'}",
            ephemeral=True)

    @donation_day.command(name="end", description="Publish a final snapshot and end the donation day")
    @has_bot_manager_role()
    async def end_event(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        guild_id = str(not_none(ctx.guild).id)
        event = get_event(guild_id)
        if not event or not event.get("active"):
            await ctx.followup.send("No donation day is running.", ephemeral=True)
            return
        url = await publish_event(guild_id, event, str(not_none(self.bot.user).id))
        save_event(guild_id, {**event, "active": False})
        await ctx.followup.send(f"**{event['name']}** has ended. Final page: "
                                f"{url or 'the final upload failed; the last published page stays up'}",
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
        state = "running" if event.get("active") else "ended"
        await ctx.followup.send(
            f"**{event['name']}** ({state}): ${payload['raised']} raised of "
            f"${event['goal_usd']}, ${payload['pledged']} pledged.\n{url}", ephemeral=True)

    @donation_day.command(name="anonymous", description="Show as Anonymous on the donation page")
    @option("hide", bool, description="True to show as Anonymous, False to show your name")
    async def anonymous(self, ctx: discord.ApplicationContext, hide: bool) -> None:
        await ctx.defer(ephemeral=True)
        saved = await set_donation_anonymous(str(ctx.author.id), str(not_none(ctx.guild).id), hide)
        if not saved:
            await ctx.followup.send("Couldn't save that; please try again.", ephemeral=True)
            return
        await ctx.followup.send(
            "You'll show as **Anonymous**; your donations still count." if hide
            else "You'll show by name.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DonationDayCommands(bot))
