import random

import discord
from discord.ext import commands
from loguru import logger

from config import get_config
from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from models.tournament import Tournament, TournamentMatch, TournamentParticipant, TournamentRound
from sqlalchemy import select
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


async def launch_tournament_match(interaction, match_id):
    """'Play this match' button: open a cube picker that launches the premade
    draft pre-named with the pairing's teams and linked for auto-recording."""
    async with db_session() as session:
        match = await session.get(TournamentMatch, match_id)
        if match is None or match.is_bye:
            await interaction.response.send_message("This match no longer exists.", ephemeral=True)
            return
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)

    from modals import CubeDraftSelectionView
    view = CubeDraftSelectionView(
        session_type="premade",
        guild_id=interaction.guild_id,
        session_details_overrides={
            "tournament_match_id": match.id,
            "team_a_name": part_a.team_name,
            "team_b_name": part_b.team_name,
        },
    )
    await interaction.response.send_message(
        f"Pick a cube to launch **{part_a.team_name}** vs **{part_b.team_name}** "
        f"(result will record automatically when the draft finishes):",
        view=view,
        ephemeral=True,
    )


class MatchActionView(discord.ui.View):
    """Persistent per-round view with one 'Play this match' button per pairing."""

    def __init__(self, match_entries):
        super().__init__(timeout=None)
        for match_id, label in match_entries:
            button = discord.ui.Button(
                label=f"▶ {label}",
                style=discord.ButtonStyle.primary,
                custom_id=f"tournament_play:{match_id}",
            )
            button.callback = self._make_callback(match_id)
            self.add_item(button)

    @staticmethod
    def _make_callback(match_id):
        async def callback(interaction):
            await launch_tournament_match(interaction, match_id)
        return callback


async def _match_entries(session, matches):
    """Build (match_id, 'A vs B') entries for the playable (non-bye) matches."""
    entries = []
    for match in matches:
        if match.is_bye:
            continue
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        entries.append((match.id, f"{part_a.team_name} vs {part_b.team_name}"))
    return entries


async def re_register_tournament_views(bot):
    """Re-attach Play buttons on the current round's pairings message after a restart."""
    async with db_session() as session:
        stmt = (
            select(TournamentRound, Tournament)
            .join(Tournament, TournamentRound.tournament_id == Tournament.id)
            .where(
                Tournament.status == "active",
                TournamentRound.round_number == Tournament.current_round,
                TournamentRound.pairings_message_id.isnot(None),
            )
        )
        rows = (await session.execute(stmt)).all()
        for round_, _tournament in rows:
            stmt = select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
            matches = (await session.execute(stmt)).scalars().all()
            entries = await _match_entries(session, matches)
            if entries:
                bot.add_view(MatchActionView(entries), message_id=int(round_.pairings_message_id))
                logger.info(f"Re-registered tournament play buttons for round {round_.id}")


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
            await self._post_pairings(
                ctx,
                f"🏆 **{tournament.name}** has started!\n\n"
                f"**Round 1 pairings:**\n{pairing_lines}",
                matches,
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
            await self._post_pairings(
                ctx,
                f"**Round {new_round.round_number} pairings:**\n{pairing_lines}",
                matches,
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    async def _post_pairings(self, ctx, content, matches):
        """Post pairings with Play buttons and remember the message for restarts."""
        async with db_session() as session:
            entries = await _match_entries(session, matches)
        if entries:
            message = await ctx.followup.send(content, view=MatchActionView(entries))
        else:
            message = await ctx.followup.send(content)
        async with db_session() as session:
            round_ = await session.get(TournamentRound, matches[0].round_id)
            round_.pairings_channel_id = str(message.channel.id)
            round_.pairings_message_id = str(message.id)

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
