import random

import discord
from discord.ext import commands
from loguru import logger

from config import get_config, update_setting
from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from models.tournament import Tournament, TournamentMatch, TournamentParticipant, TournamentRound
from sqlalchemy import or_, select
from services.tournament_formatter import create_standings_embed, update_standings_message
from services.tournament_service import (
    advance_round,
    add_match,
    create_tournament,
    finish_tournament,
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
    # On by default; a guild opts out by explicitly setting the flag to false
    # (via /tournament disable or its config file).
    return get_config(guild_id).get("features", {}).get("tournament", True)


def _cube_picker_for_match(interaction, match_id, a_name, b_name):
    from modals import CubeDraftSelectionView
    return CubeDraftSelectionView(
        session_type="premade",
        guild_id=interaction.guild_id,
        session_details_overrides={
            "tournament_match_id": match_id,
            "team_a_name": a_name,
            "team_b_name": b_name,
        },
    )


def _recorded_result_line(a_name, b_name, a_wins, b_wins):
    """The 'result recorded' line that replaces a played match's Play button."""
    return f"✅ Result recorded: **{a_name}** {a_wins}–{b_wins} **{b_name}**"


async def launch_tournament_match(interaction, match_id):
    """'Play this match' button: run the draft lobby in a per-match thread.

    Creates a thread off the pairing message (or reuses an existing one — e.g.
    a thread the organizer already made) and posts the cube picker inside it.
    Every following interaction then happens in the thread, so the draft lobby
    lands there automatically without changing the shared draft flow.
    """
    async with db_session() as session:
        match = await session.get(TournamentMatch, match_id)
        if match is None or match.is_bye:
            await interaction.response.send_message("This match no longer exists.", ephemeral=True)
            return
        if match.team_a_wins is not None:
            # Self-heal the stale button: replace it on the public pairing message
            # with a recorded-result line, so it's gone for everyone (not just an
            # ephemeral notice to the clicker).
            part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
            part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
            result_line = _recorded_result_line(
                part_a.team_name, part_b.team_name, match.team_a_wins, match.team_b_wins)
            edited = False
            if interaction.message is not None:
                try:
                    existing = interaction.message.content or ""
                    new_content = f"{existing}\n{result_line}" if existing else result_line
                    await interaction.response.edit_message(content=new_content, view=None)
                    edited = True
                except discord.HTTPException:
                    edited = False
            if not edited:
                await interaction.response.send_message(
                    "This match already has a recorded result and can't be replayed. "
                    "Ask an admin if it needs correcting.",
                    ephemeral=True,
                )
            return
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        a_name, b_name = part_a.team_name, part_b.team_name
        existing_thread_id = match.thread_id

    prompt = (f"Pick a cube to start **{a_name}** vs **{b_name}** "
              f"(the result records automatically when the draft finishes):")

    if existing_thread_id:
        thread = interaction.guild.get_channel(int(existing_thread_id))
        if thread is None:
            thread = await interaction.guild.fetch_channel(int(existing_thread_id))
        await thread.send(content=prompt, view=_cube_picker_for_match(interaction, match_id, a_name, b_name))
        await interaction.response.send_message(f"Continue your match in {thread.mention}.", ephemeral=True)
        return

    thread = await interaction.message.create_thread(name=f"{a_name} vs {b_name}")
    async with db_session() as session:
        m = await session.get(TournamentMatch, match_id)
        m.thread_id = str(thread.id)
    await thread.send(content=prompt, view=_cube_picker_for_match(interaction, match_id, a_name, b_name))
    await interaction.response.send_message(f"Started your match in {thread.mention}.", ephemeral=True)


class PlayMatchView(discord.ui.View):
    """Persistent single-button view on one match's pairing message."""

    def __init__(self, match_id, label):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=f"▶ {label}"[:80],
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


async def re_register_tournament_views(bot):
    """Re-attach each playable match's Play button after a restart.

    Swiss only has its current round live; all-open formats (round_robin/manual)
    have every round live at once. Reported matches and byes get no button.
    """
    async with db_session() as session:
        stmt = (
            select(TournamentMatch)
            .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
            .join(Tournament, TournamentRound.tournament_id == Tournament.id)
            .where(
                Tournament.status == "active",
                or_(
                    Tournament.format != "swiss",
                    TournamentRound.round_number == Tournament.current_round,
                ),
                TournamentMatch.pairings_message_id.isnot(None),
                TournamentMatch.team_a_wins.is_(None),
                TournamentMatch.is_bye.is_(False),
            )
        )
        matches = (await session.execute(stmt)).scalars().all()
        for m in matches:
            bot.add_view(PlayMatchView(m.id, ""), message_id=int(m.pairings_message_id))
    logger.info(f"Re-registered {len(matches)} tournament play buttons")


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

    @tournament.command(name="enable", description="Admin: enable tournament commands on this server")
    @has_bot_manager_role()
    async def enable(self, ctx):
        # Deliberately not feature-gated: this command manages the gate itself.
        update_setting(ctx.guild.id, "features.tournament", True)
        logger.info(f"Tournament feature enabled in guild {ctx.guild.id} by {ctx.author.id}")
        await ctx.respond("✅ Tournament commands are now **enabled** on this server.", ephemeral=True)

    @tournament.command(name="disable", description="Admin: disable tournament commands on this server")
    @has_bot_manager_role()
    async def disable(self, ctx):
        update_setting(ctx.guild.id, "features.tournament", False)
        logger.info(f"Tournament feature disabled in guild {ctx.guild.id} by {ctx.author.id}")
        await ctx.respond("🔴 Tournament commands are now **disabled** on this server.", ephemeral=True)

    @tournament.command(name="create", description="Create a tournament and open registration")
    @has_bot_manager_role()
    async def create(
        self,
        ctx,
        name: discord.Option(str, "Tournament name"),
        format: discord.Option(
            str, "Pairing format", choices=["swiss", "round_robin", "manual"], default="swiss"
        ),
        rounds: discord.Option(
            int, "Number of Swiss rounds (Swiss only)", min_value=1, max_value=20,
            required=False, default=None,
        ),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        if format == "swiss" and rounds is None:
            await ctx.followup.send("❌ Swiss tournaments need a `rounds` count.", ephemeral=True)
            return
        # Round-robin/manual derive their round count from the schedule at start.
        total_rounds = rounds if format == "swiss" else 0
        try:
            async with db_session() as session:
                tournament = await create_tournament(
                    session, ctx.guild.id, name, total_rounds, format=format
                )
            logger.info(f"Tournament '{name}' ({format}) created in guild {ctx.guild.id} by {ctx.author.id}")
            detail = f"{tournament.total_rounds} rounds" if format == "swiss" else format.replace("_", "-")
            await ctx.followup.send(
                f"✅ Tournament **{tournament.name}** created ({detail}). "
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

    @tournament.command(name="add_match", description="Admin: author a match for a manual-format tournament")
    @has_bot_manager_role()
    async def add_match(
        self,
        ctx,
        team_a: discord.Option(str, "First team"),
        team_b: discord.Option(str, "Second team"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no tournament accepting matches right now.", ephemeral=True)
                    return
                match = await add_match(session, tournament.id, team_a, team_b)
                part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
                part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
            await ctx.followup.send(
                f"✅ Added match: **{part_a.team_name}** vs **{part_b.team_name}**. "
                f"Add more, then `/tournament start`.",
                ephemeral=True,
            )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="start", description="Admin: close registration and open the schedule")
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
                tournament_id = tournament.id
                tournament_name = tournament.name
                await start_tournament(session, tournament.id, random.Random())
            logger.info(f"Tournament {tournament_id} started in guild {ctx.guild.id} by {ctx.author.id}")
            await ctx.followup.send(f"🏆 **{tournament_name}** has started!")
            await self._post_schedule(ctx, tournament_id)
            await self._post_standings(ctx, tournament_id)
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
                tournament_id = tournament.id
            logger.info(
                f"Result set for match {match.id} ({part_a.team_name} {match.team_a_wins}-"
                f"{match.team_b_wins} {part_b.team_name}) by {ctx.author.id}"
            )
            await ctx.followup.send(
                f"✅ Result recorded: **{part_a.team_name}** {match.team_a_wins}–"
                f"{match.team_b_wins} **{part_b.team_name}**"
            )
            await update_standings_message(self.bot, tournament_id)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="finish", description="Admin: end the tournament and crown the champion")
    @has_bot_manager_role()
    async def finish(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no active tournament.", ephemeral=True)
                    return
                tournament_id = tournament.id
                tournament_name = tournament.name
                champion = await finish_tournament(session, tournament.id)
            champ_text = f"Champion: **{champion.team_name}** 🏆" if champion else "No teams competed."
            logger.info(f"Tournament {tournament_id} finished in guild {ctx.guild.id} by {ctx.author.id}")
            await ctx.followup.send(f"🏁 **{tournament_name}** is complete! {champ_text}")
            await update_standings_message(self.bot, tournament_id)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="next_round", description="Admin: pair the next Swiss round (all results must be in)")
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
                tournament_id = tournament.id
                tournament_name = tournament.name
                new_round = await advance_round(session, tournament.id, random.Random())
                if new_round is None:
                    standings = await get_standings_data(session, tournament.id)
                    winner = standings[0]
                    await ctx.followup.send(
                        f"🏁 **{tournament_name}** is complete! "
                        f"Champion: **{winner.team_name}** 🏆"
                    )
                    await update_standings_message(self.bot, tournament_id)
                    return
                new_round_id = new_round.id
                new_round_number = new_round.round_number
            await self._post_round_messages(ctx, new_round_id, new_round_number)
            await update_standings_message(self.bot, tournament_id)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    async def _post_schedule(self, ctx, tournament_id):
        """Post the whole schedule, one message per match. Swiss has one round;
        all-open formats reveal every round at once."""
        async with db_session() as session:
            rounds = (await session.execute(
                select(TournamentRound)
                .where(TournamentRound.tournament_id == tournament_id)
                .order_by(TournamentRound.round_number)
            )).scalars().all()
            round_meta = [(r.id, r.round_number) for r in rounds]
        for round_id, round_number in round_meta:
            await self._post_round_messages(ctx, round_id, round_number)

    async def _post_round_messages(self, ctx, round_id, round_number):
        """Post a week header, then one message per match. Each playable match
        gets its own Play button (its thread is created off this message); byes
        and already-reported matches show as text with no button."""
        await ctx.followup.send(f"**Week {round_number} pairings:**")
        async with db_session() as session:
            matches = (await session.execute(
                select(TournamentMatch).where(TournamentMatch.round_id == round_id)
            )).scalars().all()
            rows = []
            for m in matches:
                part_a = await session.get(TournamentParticipant, m.team_a_participant_id)
                if m.is_bye:
                    rows.append((m.id, f"• **{part_a.team_name}** — BYE (auto win)", None, False))
                else:
                    part_b = await session.get(TournamentParticipant, m.team_b_participant_id)
                    label = f"{part_a.team_name} vs {part_b.team_name}"
                    if m.team_a_wins is None:
                        rows.append((m.id, f"• **{label}**", label, True))
                    else:
                        rows.append((m.id, f"• **{part_a.team_name}** {m.team_a_wins}–"
                                           f"{m.team_b_wins} **{part_b.team_name}**", label, False))

        for match_id, text, label, playable in rows:
            if not playable:
                await ctx.followup.send(text)
                continue
            message = await ctx.followup.send(text, view=PlayMatchView(match_id, label))
            async with db_session() as session:
                m = await session.get(TournamentMatch, match_id)
                m.pairings_channel_id = str(message.channel.id)
                m.pairings_message_id = str(message.id)

    async def _post_standings(self, ctx, tournament_id):
        """Post the standings message and remember it for in-place updates."""
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            participants = await get_standings_data(session, tournament_id)
            embed = create_standings_embed(tournament, participants)
        message = await ctx.followup.send(embed=embed)
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            tournament.standings_channel_id = str(message.channel.id)
            tournament.standings_message_id = str(message.id)

    @tournament.command(name="refresh_standings", description="Admin: re-render the standings message from current results")
    @has_bot_manager_role()
    async def refresh_standings(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament.", ephemeral=True)
                return
            tournament_id = tournament.id
            has_message = tournament.standings_message_id is not None
        if has_message:
            await update_standings_message(self.bot, tournament_id)
        else:
            await self._post_standings(ctx, tournament_id)
        logger.info(f"Standings message refreshed for tournament {tournament_id} by {ctx.author.id}")
        await ctx.followup.send("✅ Standings refreshed.", ephemeral=True)

    @tournament.command(name="recover_draft", description="Admin: recreate channels for a reaped in-progress match draft")
    @has_bot_manager_role()
    async def recover_draft(
        self,
        ctx,
        match_id: discord.Option(int, "Tournament match id to recover"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        from models.draft_session import DraftSession
        from utils import recover_draft_channels

        async with db_session() as session:
            match = await session.get(TournamentMatch, match_id)
            if match is None:
                await ctx.followup.send(f"❌ No tournament match `{match_id}`.", ephemeral=True)
                return
            if match.team_a_wins is not None:
                await ctx.followup.send(
                    f"❌ Match `{match_id}` already has a result — nothing to recover.",
                    ephemeral=True,
                )
                return
            ds = (await session.execute(
                select(DraftSession).where(DraftSession.tournament_match_id == match_id)
            )).scalars().first()
            if ds is None:
                await ctx.followup.send(
                    f"❌ No draft session is linked to match `{match_id}`.", ephemeral=True
                )
                return
            session_id = ds.session_id
            existing_chat = ds.draft_chat_channel

        # Idempotency: refuse if the current draft-chat still resolves to a live channel.
        if existing_chat and ctx.guild.get_channel(int(existing_chat)):
            await ctx.followup.send(
                f"❌ Match `{match_id}`'s draft-chat (<#{existing_chat}>) still exists — "
                f"nothing to recover.",
                ephemeral=True,
            )
            return

        new_channel_id = await recover_draft_channels(self.bot, ctx.guild, session_id)
        if new_channel_id is None:
            await ctx.followup.send("❌ Recovery failed — see logs.", ephemeral=True)
            return
        logger.info(
            f"Recovered draft for match {match_id} (session {session_id}) in guild "
            f"{ctx.guild.id} by {ctx.author.id} -> channel {new_channel_id}"
        )
        await ctx.followup.send(
            f"✅ Recovered match `{match_id}`. New draft-chat: <#{new_channel_id}>.",
            ephemeral=True,
        )

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
                embed = self._registration_embed(tournament, participants)
            else:
                participants = await get_standings_data(session, tournament.id)
                embed = create_standings_embed(tournament, participants)
        await ctx.followup.send(embed=embed)

    def _registration_embed(self, tournament, participants):
        embed = discord.Embed(
            title=f"🏆 {tournament.name}",
            description=f"**Status:** {tournament.status.title()}",
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
        return embed


def setup(bot):
    bot.add_cog(TournamentCog(bot))
