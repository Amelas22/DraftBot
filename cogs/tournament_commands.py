import random

import discord
from discord.ext import commands
from loguru import logger

from config import get_config
from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from models.tournament import TournamentParticipant
from services.tournament_service import (
    advance_round,
    create_tournament,
    find_current_match,
    get_active_tournament,
    get_standings_data,
    list_participants,
    register_team,
    remove_team,
    set_result,
    start_tournament,
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

    @tournament.command(name="add_team", description="Admin: register a team on a captain's behalf")
    @has_bot_manager_role()
    async def add_team(
        self,
        ctx,
        team: discord.Option(str, "Team name"),
        captain: discord.Option(discord.Member, "The team's captain"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no tournament accepting registrations right now.", ephemeral=True)
                    return
                participant, created = await register_team(session, tournament.id, team, captain.id)
            verb = "registered" if created else "already registered"
            await ctx.followup.send(
                f"✅ **{participant.team_name}** {verb} with {captain.mention} as captain.",
                ephemeral=True,
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="remove_team", description="Admin: remove a team during registration")
    @has_bot_manager_role()
    async def remove_team(
        self,
        ctx,
        team: discord.Option(str, "Team name"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no active tournament.", ephemeral=True)
                    return
                participant = await remove_team(session, tournament.id, team)
            await ctx.followup.send(f"✅ **{participant.team_name}** removed.", ephemeral=True)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="start", description="Admin: close registration and pair round 1")
    @has_bot_manager_role()
    async def start(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no tournament to start.", ephemeral=True)
                    return
                matches = await start_tournament(session, tournament.id, random.Random())
                pairing_lines = await self._format_pairings(session, matches)
            logger.info(f"Tournament {tournament.id} started in guild {ctx.guild.id} by {ctx.author.id}")
            await ctx.followup.send(
                f"🏆 **{tournament.name}** has started!\n\n"
                f"**Round 1 pairings:**\n{pairing_lines}",
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="set_result", description="Admin: record or correct a match result")
    @has_bot_manager_role()
    async def set_result(
        self,
        ctx,
        team: discord.Option(str, "Either team in the match"),
        team_wins: discord.Option(int, "Game wins for that team", min_value=0, max_value=10),
        opponent_wins: discord.Option(int, "Game wins for their opponent", min_value=0, max_value=10),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no active tournament.", ephemeral=True)
                    return
                match = await find_current_match(session, tournament.id, team)
                if match is None:
                    await ctx.followup.send(
                        f"No current-round match found for **{team}**.", ephemeral=True
                    )
                    return
                part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
                # Map the named team onto side A/B of the stored match
                if part_a.team_name.lower() == team.strip().lower():
                    a_wins, b_wins = team_wins, opponent_wins
                else:
                    a_wins, b_wins = opponent_wins, team_wins
                match = await set_result(session, match.id, a_wins, b_wins)
                part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
            logger.info(
                f"Result set for match {match.id} ({part_a.team_name} {match.team_a_wins}-"
                f"{match.team_b_wins} {part_b.team_name}) by {ctx.author.id}"
            )
            await ctx.followup.send(
                f"✅ Result recorded: **{part_a.team_name}** {match.team_a_wins}–"
                f"{match.team_b_wins} **{part_b.team_name}**"
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="next_round", description="Admin: pair the next round (all results must be in)")
    @has_bot_manager_role()
    async def next_round(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no active tournament.", ephemeral=True)
                    return
                new_round = await advance_round(session, tournament.id, random.Random())
                if new_round is None:
                    standings = await get_standings_data(session, tournament.id)
                    winner = standings[0]
                    await ctx.followup.send(
                        f"🏁 **{tournament.name}** is complete! "
                        f"Champion: **{winner.team_name}** 🏆"
                    )
                    return
                from services.tournament_service import _round_matches
                matches = await _round_matches(session, new_round.id)
                pairing_lines = await self._format_pairings(session, matches)
            await ctx.followup.send(
                f"**Round {new_round.round_number} pairings:**\n{pairing_lines}"
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    async def _format_pairings(self, session, matches):
        lines = []
        for match in matches:
            part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
            if match.is_bye:
                lines.append(f"• **{part_a.team_name}** — BYE (auto win)")
            else:
                part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
                lines.append(f"• **{part_a.team_name}** vs **{part_b.team_name}**")
        return "\n".join(lines)

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
            if tournament.status == "registration":
                participants = await list_participants(session, tournament.id)
            else:
                participants = await get_standings_data(session, tournament.id)

        embed = discord.Embed(
            title=f"🏆 {tournament.name}",
            description=(
                f"**Status:** {tournament.status.title()}\n"
                f"**Rounds:** {tournament.current_round}/{tournament.total_rounds}"
            ),
            color=discord.Color.gold(),
        )
        if not participants:
            embed.add_field(
                name="Teams (0)",
                value="No teams yet — register with `/tournament register`.",
                inline=False,
            )
        elif tournament.status == "registration":
            teams = "\n".join(
                f"{i}. **{p.team_name}** — captain <@{p.captain_user_id}>"
                for i, p in enumerate(participants, start=1)
            )
            embed.add_field(name=f"Teams ({len(participants)})", value=teams, inline=False)
        else:
            rows = "\n".join(
                f"{i}. **{p.team_name}** — {p.points} pts "
                f"({p.match_wins}-{p.match_losses}-{p.match_draws})"
                for i, p in enumerate(participants, start=1)
            )
            embed.add_field(name="Standings", value=rows, inline=False)
        await ctx.followup.send(embed=embed)


def setup(bot):
    bot.add_cog(TournamentCog(bot))
