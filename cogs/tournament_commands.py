import discord
from discord.ext import commands
from loguru import logger

from config import get_config
from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from services.tournament_service import (
    create_tournament,
    get_active_tournament,
    list_participants,
    register_team,
)


def tournament_enabled(guild_id):
    return get_config(guild_id).get("features", {}).get("tournament", False)


class TournamentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Tournament cog initialized")

    tournament = discord.SlashCommandGroup("tournament", "Team-based Swiss tournament commands")

    async def _check_enabled(self, ctx):
        if tournament_enabled(ctx.guild.id):
            return True
        await ctx.respond("Tournaments are not enabled on this server.", ephemeral=True)
        return False

    @tournament.command(name="create", description="Create a tournament and open registration")
    @has_bot_manager_role()
    async def create(
        self,
        ctx,
        name: discord.Option(str, "Tournament name"),
        rounds: discord.Option(int, "Number of Swiss rounds", min_value=1, max_value=20),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            async with db_session() as session:
                tournament = await create_tournament(session, ctx.guild.id, name, rounds)
            logger.info(f"Tournament '{name}' created in guild {ctx.guild.id} by {ctx.author.id}")
            await ctx.followup.send(
                f"✅ Tournament **{tournament.name}** created with {tournament.total_rounds} rounds. "
                f"Registration is open — captains can join with `/tournament register`.",
                ephemeral=True,
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="register", description="Register your team for the open tournament")
    async def register(
        self,
        ctx,
        team: discord.Option(str, "Your team name"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no tournament accepting registrations right now.", ephemeral=True)
                    return
                participant, created = await register_team(
                    session, tournament.id, team, ctx.author.id
                )
            if created:
                logger.info(
                    f"Team '{participant.team_name}' registered for tournament {tournament.id} "
                    f"by {ctx.author.id} in guild {ctx.guild.id}"
                )
                await ctx.followup.send(
                    f"✅ **{participant.team_name}** is registered for **{tournament.name}** "
                    f"with {ctx.author.mention} as captain."
                )
            else:
                await ctx.followup.send(
                    f"**{participant.team_name}** is already registered for **{tournament.name}** "
                    f"(captain: <@{participant.captain_user_id}>).",
                    ephemeral=True,
                )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="status", description="Show the current tournament and its teams")
    async def status(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament in this server.", ephemeral=True)
                return
            participants = await list_participants(session, tournament.id)

        embed = discord.Embed(
            title=f"🏆 {tournament.name}",
            description=(
                f"**Status:** {tournament.status.title()}\n"
                f"**Rounds:** {tournament.current_round}/{tournament.total_rounds}"
            ),
            color=discord.Color.gold(),
        )
        if participants:
            teams = "\n".join(
                f"{i}. **{p.team_name}** — captain <@{p.captain_user_id}>"
                for i, p in enumerate(participants, start=1)
            )
            embed.add_field(name=f"Teams ({len(participants)})", value=teams, inline=False)
        else:
            embed.add_field(
                name="Teams (0)",
                value="No teams yet — register with `/tournament register`.",
                inline=False,
            )
        await ctx.followup.send(embed=embed)


def setup(bot):
    bot.add_cog(TournamentCog(bot))
