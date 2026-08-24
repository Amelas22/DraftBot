import random

import discord
from discord.ext import commands
from loguru import logger

from config import get_config, update_setting
from helpers.match_control import render_pairing_line
from helpers.tournament_channels import (
    PAIRINGS,
    STANDINGS,
    ensure_channel,
    resolve_channel,
)
from database.db_session import db_session
from helpers.money_gate import gate_serve, linked_username
from helpers.display_names import get_display_name
from helpers.permissions import bot_manager_button, has_bot_manager_role, is_bot_manager
from helpers.pin_helpers import safe_pin
from helpers.utils import ui_button
from match_control_view import (
    MatchControlView,
    create_match_room,
    match_facts,
    safe_refresh_match_views,
)
from models.tournament import (
    STAGE_SWISS,
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
)
from sqlalchemy import or_, select
from services.tournament_formatter import (
    create_registration_embed,
    create_standings_embed,
    post_registration_board,
    refresh_boards,
    round_label,
    update_standings_message,
)
from services.tournament_service import (
    advance_round,
    add_match,
    add_teammate,
    count_unreported_matches,
    create_tournament,
    current_round_stage,
    finish_tournament,
    find_current_match,
    find_participant_by_name,
    find_participants_for_captain,
    get_active_tournament,
    get_final_placement,
    get_latest_completed_tournament,
    get_rosters,
    get_standings_data,
    is_playoff,
    list_participants,
    other_teams_for_user,
    register_team,
    remove_teammate,
    set_result,
    start_playoff,
    store_role_ids,
    SwissComplete,
)
from services.mtgo_tradebot_client import EVENT_TICKET
from services import tournament_escrow_service as escrow
from services import wallet_service
from services.tournament_roles import create_team_roles, delete_team_roles, sync_member


# Registering records only the captain, so every successful registration has to say
# how the rest of the team gets on the board -- otherwise the roster silently stays
# a team of one.
ROSTER_PROMPT = ("Now add your teammates with `/tournament add_teammate @player` "
                 "— one command per player.")


def tournament_enabled(guild_id):
    # On by default; a guild opts out by explicitly setting the flag to false
    # (via /tournament disable or its config file).
    return get_config(guild_id).get("features", {}).get("tournament", True)


async def re_register_tournament_views(bot):
    """Re-attach each playable match's control view after a restart.

    Swiss only has its current round live; all-open formats (round_robin/manual)
    have every round live at once. Reported matches, byes, and matches with no
    control message (room creation failed or hasn't happened) get no view.
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
                TournamentMatch.control_message_id.isnot(None),
            )
        )
        matches = (await session.execute(stmt)).scalars().all()
        for m in matches:
            bot.add_view(MatchControlView(m.id), message_id=int(m.control_message_id))
    logger.info(f"Re-registered {len(matches)} tournament control views")


_PLACE_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _format_payout_lines(allocations):
    """Render [(place, captain_id, team_name, amount)] as medal-prefixed payout lines."""
    return "\n".join(
        f"{_PLACE_MEDALS.get(place, f'{place}.')} <@{cap}> (**{name}**) — **{amt} tix**"
        for place, cap, name, amt in allocations
    )


class PayoutConfirmView(discord.ui.View):
    """Final confirmation before disbursing a prize pool. Mirrors SettlementConfirmView:
    invoker-only, 120s timeout, double-click guarded. Disburses only on Confirm; the actual
    transfer (escrow.execute_payout) is idempotent, so a race can't double-pay."""

    def __init__(self, guild_id, tournament_id, t_name, pool, struct, allocations, author_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.tournament_id = tournament_id
        self.t_name = t_name
        self.pool = pool
        self.struct = struct
        self.allocations = allocations
        self.author_id = str(author_id)
        self.message = None
        self._processing = False

    async def interaction_check(self, interaction):
        if str(interaction.user.id) != self.author_id:
            await interaction.response.send_message(
                "Only the admin who started this payout can confirm it.", ephemeral=True)
            return False
        return True

    def _set_buttons(self, disabled: bool):
        for item in self.children:
            item.disabled = disabled

    async def _fail(self, interaction, msg: str):
        """Re-arm the view and surface the error so the admin can retry."""
        self._processing = False
        self._set_buttons(False)
        try:
            await interaction.edit_original_response(content=msg, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Confirm payout", style=discord.ButtonStyle.success, emoji="🏦")
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self._processing:
            await interaction.response.send_message("Payout is already processing…", ephemeral=True)
            return
        self._processing = True
        await interaction.response.defer()
        self._set_buttons(True)
        try:
            res = await escrow.execute_payout(self.guild_id, self.tournament_id, self.allocations)
        except Exception as e:
            logger.error(f"[PayoutConfirm] execute failed: {e}")
            await self._fail(interaction, f"❌ Payout error: {e}")
            return
        if not res.get("ok"):
            await self._fail(interaction, f"❌ Payout failed: {res.get('error')}")
            return
        if res.get("already_paid"):
            await interaction.edit_original_response(
                content=f"**{self.t_name}** was already paid out.", embed=None, view=None)
            self.stop()
            return
        await interaction.edit_original_response(
            content=f"✅ Paid out **{res['total']} tix** for **{self.t_name}**.", embed=None, view=None)
        announcement = (
            f"🏦 **{self.t_name}** — prize pool of **{self.pool} tix** paid out "
            f"(*{escrow.describe_structure(self.struct)}*):\n{_format_payout_lines(self.allocations)}\n"
            f"Winners can `/wallet withdraw` to MTGO or `/wallet pay` teammates."
        )
        try:
            await interaction.channel.send(announcement)
        except Exception as e:
            logger.warning(f"[PayoutConfirm] public announcement failed: {e}")
        logger.info(f"Tournament {self.tournament_id} paid out {res['total']} tix by {interaction.user.id}")
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self._processing:
            await interaction.response.send_message("Payout is processing, can't cancel now.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Payout cancelled.", embed=None, view=None)
        self.stop()

    async def on_timeout(self):
        self._set_buttons(True)
        if self.message is not None:
            try:
                await self.message.edit(content="Payout confirmation expired — run `/tournament payout` again.",
                                        embed=None, view=None)
            except Exception:
                pass


class PlayoffPromptView(discord.ui.View):
    """End-of-swiss choice: start the bracket, or finish now.

    Times out like any view, which is why /tournament playoff exists as an
    explicit path — a TO returning later must not be stranded by a dead prompt.
    """

    def __init__(self, cog, tournament_id, cut_to, disabled=False):
        super().__init__(timeout=900)
        self.cog = cog
        self.tournament_id = tournament_id
        self.cut_to = cut_to
        # Set by the poster, so the prompt can be rewritten when it expires or
        # when an answer fails. Without it the buttons still LOOK live 15
        # minutes later and clicking one gets Discord's "interaction failed".
        self.message = None
        self.start_playoff_button.disabled = disabled
        self.start_playoff_button.label = f"Start top-{cut_to} playoff"

    async def _rewrite(self, content):
        """Replace the prompt's text and take its buttons away, if we have the
        message. Never raises: the prompt is a view, and losing this edit must
        not take the handler down with it."""
        if self.message is None:
            return
        try:
            await self.message.edit(content=content, view=None)
        except Exception as e:
            logger.warning(f"[PlayoffPrompt] could not rewrite the prompt: {e}")

    async def _settled(self, message):
        """Replace the "⏳ …" line once the answer has actually landed.

        _answer closes the prompt with a progress line before its slow work,
        and _failed replaces that line when the work raises. Success had
        nothing: the public message went on claiming the cut was still running
        long after the bracket was posted, which reads as a hung command to
        everyone in the channel except the person who clicked."""
        await self._rewrite(message)

    async def _failed(self, interaction, message):
        """Report a failed answer to the clicker AND on the prompt itself.

        Both handlers close the prompt with a "⏳ …" line before their slow
        work, so without this the public message is left claiming the work is
        still running while only the clicker's ephemeral reply says otherwise.
        """
        await interaction.followup.send(message, ephemeral=True)
        await self._rewrite(message)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        await self._rewrite(
            f"This prompt expired — cut with `/tournament playoff top:{self.cut_to}`, "
            f"or crown the Swiss leader with `/tournament finish`.")

    async def _answer(self, interaction, taken):
        """Close the prompt before doing any slow work.

        Starting the bracket posts a whole round — pairing message, thread and
        control message per match, seconds of Discord I/O. While that runs both
        buttons stay clickable on a PUBLIC message, so a manager who sees
        nothing happen (or a second one answering the same prompt) can hit
        "Finish now" and irreversibly complete the tournament mid-bracket —
        the exact accident this prompt exists to prevent. Disabling the items
        and editing the message also acknowledges the interaction, so the
        handlers use followup from here on.
        """
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content=taken, view=self)
        except Exception as e:
            logger.warning(f"[PlayoffPrompt] could not close the prompt: {e}")
            try:
                await interaction.response.defer()
            except Exception:
                pass

    # The prompt is posted publicly (any bot manager should be able to answer
    # it, not only the one who ran next_round) — bot_manager_button is what
    # keeps a non-manager from clicking either option, since finish_button
    # below does exactly what the role-gated /tournament finish does.
    @bot_manager_button
    @ui_button(label="Start playoff", style=discord.ButtonStyle.success)
    async def start_playoff_button(self, button, interaction):
        await self._answer(interaction, f"⏳ Cutting to the top {self.cut_to}…")
        try:
            await self.cog._run_playoff(interaction, self.tournament_id)
        except ValueError as e:
            await self._failed(interaction, f"❌ {e}")
            return
        await self._settled(f"✅ Cut to the top {self.cut_to} — the bracket is posted.")
        await interaction.followup.send(f"🏆 Top {self.cut_to} playoff started.")
        self.stop()

    @bot_manager_button
    @ui_button(label="Finish now — crown the Swiss leader", style=discord.ButtonStyle.secondary)
    async def finish_button(self, button, interaction):
        await self._answer(interaction, "⏳ Finishing now — crowning the Swiss leader…")
        try:
            announcement = await self.cog._run_finish(interaction, self.tournament_id)
        except ValueError as e:
            await self._failed(interaction, f"❌ {e}")
            return
        await self._settled("✅ Finished — the Swiss leader was crowned.")
        await interaction.followup.send(announcement)
        self.stop()


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

    async def _refresh_board(self, tournament_id):
        """Refresh a tournament's registration board. The board is a view — never let a
        Discord failure break the command that changed the roster. Open vs closed is
        derived from the tournament's own status inside the refresh."""
        await refresh_boards(self.bot, [tournament_id])

    async def _create_roles_for_start(self, ctx):
        """Create a role per paid team, before the tournament starts.

        Returns {participant id: role id}, or raises. Ordered before
        close_registration_and_seed on purpose: that call commits the start, so
        a role failure afterwards would mean un-starting a started tournament.
        The cheap read here means 42 roles are not created for a tournament
        that was never going to start.

        This reads the tournament and its participants in their own session,
        released before close_registration_and_seed re-reads under MONEY_LOCK --
        so the two reads are not atomic with each other. The status check is
        reconciled by that second read; the participant SET is not. A payment
        landing in the gap seeds a paid team with role_id=None (no role, ever,
        until someone notices); a refund-driven drop in the gap leaves that
        team's already-created role stranded with nothing recording it. Both are
        documented, recoverable drift, not corruption, and this is a deliberate
        choice to leave the window open: closing it means creating roles inside
        the money lock, which the spec rejects.
        """
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None or tournament.status != "registration":
                return {}
            paid = [p for p in await list_participants(session, tournament.id)
                    if p.status == "paid"]
            if len(paid) < 2:
                return {}
            return await create_team_roles(ctx.guild, paid)

    @tournament.command(name="enable", description="Admin: enable tournament commands on this server")
    @has_bot_manager_role()
    async def enable(self, ctx):
        # Deliberately not feature-gated: this command manages the gate itself.
        update_setting(ctx.guild.id, "features.tournament", True)
        logger.info(f"Tournament feature enabled in guild {ctx.guild.id} by {ctx.author.id}")
        await ctx.respond("✅ Tournament commands are now **enabled** on this server.", ephemeral=True)

    @tournament.command(name="setup_channels", description="Admin: set fixed homes for standings and pairings")
    @has_bot_manager_role()
    async def setup_channels(
        self,
        ctx,
        standings: discord.Option(
            discord.TextChannel, "Use this channel for standings instead of making one",
            required=False, default=None),
        play: discord.Option(
            discord.TextChannel, "Use this channel for pairings instead of making one",
            required=False, default=None),
    ):
        """Give this season's messages a home.

        Both channels are created in the category of the channel you run this
        from — put yourself in your league/tournament category and the season
        lands there. Pass a channel to either option to adopt an existing one
        instead. Standings is created read-only (one message, edited all
        season); pairings stays writable, since players click Play in it.
        """
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)
        category = getattr(ctx.channel, "category", None)
        try:
            standings_channel, standings_new = await ensure_channel(
                ctx.guild, config, STANDINGS, category, standings)
            play_channel, play_new = await ensure_channel(
                ctx.guild, config, PAIRINGS, category, play)
        except discord.Forbidden:
            await ctx.followup.send(
                "❌ I need **Manage Channels** to create tournament channels — grant it, "
                "or pass existing `standings:` and `play:` channels.", ephemeral=True)
            return

        update_setting(ctx.guild.id, f"tournament.{STANDINGS.setting}", str(standings_channel.id))
        update_setting(ctx.guild.id, f"tournament.{PAIRINGS.setting}", str(play_channel.id))
        logger.info(
            f"Tournament channels set in guild {ctx.guild.id} by {ctx.author.id}: "
            f"standings={standings_channel.id} (created={standings_new}) "
            f"play={play_channel.id} (created={play_new}) category={category.id if category else None}")

        def clause(channel, created):
            return f"{'🆕 created' if created else '📌 using'} {channel.mention}"

        where = f" in **{category.name}**" if category else ""
        await ctx.followup.send(
            f"Tournament channels{where}: {clause(standings_channel, standings_new)} for "
            f"standings and {clause(play_channel, play_new)} for pairings.\n"
            f"Existing standings messages stay where they are — this takes effect from the next "
            f"`/tournament start`.", ephemeral=True)

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
        entry_fee: discord.Option(
            int, "Per-team entry fee in tix (0 = free)", min_value=0, default=0
        ),
        payout: discord.Option(
            str, "Prize-pool split for entry-fee tournaments",
            choices=list(escrow.PAYOUT_CHOICES), default="winner_take_all"
        ),
        cut: discord.Option(
            int, "Cut to a top-N single-elimination playoff after Swiss (blank = no cut)",
            min_value=2, max_value=32, required=False, default=None,
        ),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        if format == "swiss" and rounds is None:
            await ctx.followup.send("❌ Swiss tournaments need a `rounds` count.", ephemeral=True)
            return
        if cut is not None and format != "swiss":
            # start_playoff refuses any format but swiss, so storing cut_to here
            # would put a cut on the registration board that can never be run.
            await ctx.followup.send(
                f"❌ A cut is seeded from Swiss standings — `format:swiss` is required "
                f"(this one is {format.replace('_', '-')}). Create it without `cut:`.",
                ephemeral=True)
            return
        if entry_fee > 0:
            gate = gate_serve(ctx)
            if gate:
                await ctx.followup.send(f"❌ Can't charge an entry fee here: {gate}", ephemeral=True)
                return
        # Round-robin/manual derive their round count from the schedule at start.
        total_rounds = rounds if format == "swiss" else 0
        try:
            async with db_session() as session:
                tournament = await create_tournament(
                    session, ctx.guild.id, name, total_rounds, format=format,
                    entry_fee=entry_fee, payout_structure=payout, cut_to=cut
                )
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)
            return
        logger.info(f"Tournament '{name}' ({format}) created in guild {ctx.guild.id} by {ctx.author.id}")
        detail = f"{tournament.total_rounds} rounds" if format == "swiss" else format.replace("_", "-")
        fee_line = (
            f" Entry fee: **{entry_fee} {EVENT_TICKET}(s)** per team — a team is registered "
            f"once its captain's escrow is received. Payout: **{escrow.describe_structure(payout)}**."
            if entry_fee > 0 else ""
        )
        # The ephemeral confirmation rides the interaction token, which can be flaky
        # (ack races, expiry) independently of everything else here — a failure to
        # notify the invoker must never prevent the registration board (the actual
        # source of truth for onlookers) from posting to the channel below.
        try:
            await ctx.followup.send(
                f"✅ Tournament **{tournament.name}** created ({detail}). "
                f"Registration is open — captains can join with `/tournament register`.{fee_line}",
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"Could not send tournament-create confirmation for {tournament.id}: {e}")
        try:
            # Deliberately NOT routed through _destination: the board belongs
            # wherever registration is being announced, which is why the
            # organizer ran /tournament create there. Only standings (buried by
            # its own updates) and pairings get fixed homes.
            await post_registration_board(ctx.channel, tournament.id)
        except Exception as e:
            logger.warning(f"Could not post registration board: {e}")

    @tournament.command(name="register", description="Register your team for the open tournament")
    async def register(
        self,
        ctx,
        team: discord.Option(str, "Your team name"),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)

        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
        if tournament is None:
            await ctx.followup.send("There is no tournament accepting registrations right now.", ephemeral=True)
            return
        t_id, t_name, fee = tournament.id, tournament.name, (tournament.entry_fee or 0)

        guild_id = str(ctx.guild.id)
        captain_id = str(ctx.author.id)

        # A paid tournament needs the money stack + the captain's MTGO link BEFORE we create
        # anything, so we never leave a pending row that can't be paid.
        captain_user = None
        if fee > 0:
            gate = gate_serve(ctx)
            if gate:
                await ctx.followup.send(f"❌ {gate}", ephemeral=True)
                return
            captain_user = await linked_username(ctx.author.id)
            if not captain_user:
                await ctx.followup.send(
                    "Link your MTGO account first with `/link_mtgo <username>`, then register.",
                    ephemeral=True)
                return

        # Create (or find) the participant.
        try:
            async with db_session() as session:
                participant, created = await register_team(session, t_id, team, ctx.author.id)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)
            return
        p_id, p_name = participant.id, participant.team_name

        # Free tournament: registration is complete on creation (existing behavior).
        if fee == 0:
            if created:
                logger.info(f"Team '{p_name}' registered for tournament {t_id} by {ctx.author.id}")
                await self._refresh_board(t_id)
                await ctx.followup.send(
                    f"✅ **{p_name}** is registered for **{t_name}** with "
                    f"{ctx.author.mention} as captain.\n"
                    f"{ROSTER_PROMPT}", ephemeral=True)
            else:
                await ctx.followup.send(
                    f"**{p_name}** is already registered for **{t_name}** "
                    f"(captain: <@{participant.captain_user_id}>).", ephemeral=True)
            return

        # Paid tournament.
        if participant.status == "paid":
            await ctx.followup.send(
                f"**{p_name}** is already registered and paid for **{t_name}**.", ephemeral=True)
            return
        # Escrow is paid from the captain's wallet, so only the captain can complete it.
        if captain_id != participant.captain_user_id:
            await ctx.followup.send(
                f"**{p_name}** is registered (pending). Only its captain "
                f"<@{participant.captain_user_id}> can complete the **{fee}-tix** escrow.",
                ephemeral=True)
            return

        # Try to hold the fee from the captain's wallet right away.
        res = await escrow.secure_from_wallet(guild_id, captain_id, p_id, t_id, fee, p_name)
        if res.get("done"):
            await self._refresh_board(t_id)
            await ctx.followup.send(
                f"✅ **{p_name}** is registered for **{t_name}** — **{fee} "
                f"{EVENT_TICKET}(s)** paid into the prize pool. You're in.\n"
                f"{ROSTER_PROMPT}", ephemeral=True)
            return
        if not res.get("ok"):
            # register_team already committed a pending participant above, so the board
            # must still reflect it even though the escrow hold itself failed — otherwise
            # this stuck-pending team never appears (the watchdog only refreshes boards
            # for entries it COMPLETES).
            await self._refresh_board(t_id)
            await ctx.followup.send(
                f"⚠️ **{p_name}** is registered but I couldn't hold the escrow: {res.get('error')}. "
                f"Try `/tournament register` again.", ephemeral=True)
            return

        # Wallet short: hold the spot and let the captain top up on their own schedule.
        # No trade is opened here — /wallet deposit does that whenever they're ready, and
        # the escrow sweep completes this registration the moment the funds cover the fee.
        deficit = res["deficit"]
        have = res.get("available", 0)
        await self._refresh_board(t_id)
        await ctx.followup.send(
            f"**{p_name}** is registered (pending) for **{t_name}** — your spot is held.\n"
            f"To finish, run `/wallet deposit {deficit}` whenever you're ready (you have "
            f"{have} tix; the fee is {fee}). Registration completes automatically once the "
            f"tix land — no need to re-register.", ephemeral=True)

    async def _roster_target(self, ctx, session, tournament, team):
        """The participant whose roster this command edits, or None if it can't.

        Naming a ``team`` you captain is allowed and is how a captain of several
        teams picks one; naming someone else's needs the bot-manager role, which is
        also how a mis-added player gets fixed and how the imported teams with no
        real captain (captain_user_id "0") get a roster at all.

        Returns None only after replying, so callers just return.
        """
        if team:
            participant = await find_participant_by_name(session, tournament.id, team)
            if participant is None:
                await ctx.followup.send(
                    f"❌ '{team}' is not registered for **{tournament.name}**.",
                    ephemeral=True)
                return None
            if (participant.captain_user_id != str(ctx.author.id)
                    and not await is_bot_manager(ctx)):
                await ctx.followup.send(
                    f"**{participant.team_name}** isn't your team — only its captain "
                    f"<@{participant.captain_user_id}> or a bot manager can edit its "
                    f"roster.", ephemeral=True)
                return None
            return participant

        owned = await find_participants_for_captain(session, tournament.id, ctx.author.id)
        if not owned:
            await ctx.followup.send(
                f"You don't captain a team in **{tournament.name}**. Register one with "
                f"`/tournament register <team name>` first.", ephemeral=True)
            return None
        if len(owned) > 1:
            # Silently editing the first would be the worst outcome: nothing would
            # tell them the change landed on the other team.
            names = ", ".join(f"**{p.team_name}**" for p in owned)
            await ctx.followup.send(
                f"You captain {names} in **{tournament.name}** — say which one with "
                f"the `team` option.", ephemeral=True)
            return None
        return owned[0]

    @tournament.command(name="add_teammate",
                        description="Add a player to your team's roster")
    async def add_teammate_cmd(
        self,
        ctx,
        player: discord.Option(discord.Member, "The teammate to add"),
        team: discord.Option(str, "Which team (needed if you captain more than one)",
                             required=False, default=None),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        if player.bot:
            await ctx.followup.send("Bots can't be on a team's roster.", ephemeral=True)
            return

        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no tournament running right now.",
                                        ephemeral=True)
                return
            t_id, t_name = tournament.id, tournament.name
            participant = await self._roster_target(ctx, session, tournament, team)
            if participant is None:
                return
            try:
                _, created = await add_teammate(
                    session, participant, player.id,
                    get_display_name(player, ctx.guild))
            except ValueError as e:
                await ctx.followup.send(f"❌ {e}", ephemeral=True)
                return
            p_name = participant.team_name
            role_id = participant.role_id
            # Sharing a player between teams is allowed, but it should never happen
            # unnoticed — say so on the reply instead of blocking the add.
            others = await other_teams_for_user(
                session, t_id, player.id, participant.id)
            also_on = ", ".join(f"**{p.team_name}**" for p in others)

        # Outside the session: the board reads the roster back in its own session,
        # so refreshing before this one commits would render the pre-change state.
        await self._refresh_board(t_id)
        # Also outside the session, and load-bearing, not just tidy: a Discord
        # round-trip here would otherwise hold this write session's SQLite
        # connection open for its whole duration. Safe only because
        # AsyncSessionLocal sets expire_on_commit=False, so the role_id
        # captured above survives past the commit.
        # Unconditional: add_roles is idempotent, so re-syncing a player who was
        # already on the roster repairs a role they somehow lack.
        synced = await sync_member(ctx.guild, role_id, str(player.id), add=True)
        warning = ("" if synced else
                   f"\n⚠️ Couldn't give them the **{p_name}** role — check that "
                   f"the bot's own role sits above it.")
        shared = f"\nAlso on {also_on} in this tournament." if also_on else ""
        if created:
            logger.info(f"{player.id} added to team '{p_name}' in tournament {t_id} "
                        f"by {ctx.author.id}")
            await ctx.followup.send(
                f"✅ {player.mention} is on **{p_name}**'s roster for **{t_name}**."
                f"{shared}{warning}",
                ephemeral=True)
        else:
            await ctx.followup.send(
                f"{player.mention} is already on **{p_name}**'s roster.{shared}{warning}",
                ephemeral=True)

    @tournament.command(name="remove_teammate",
                        description="Take a player off your team's roster")
    async def remove_teammate_cmd(
        self,
        ctx,
        player: discord.Option(discord.Member, "The teammate to remove"),
        team: discord.Option(str, "Which team (needed if you captain more than one)",
                             required=False, default=None),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)

        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no tournament running right now.",
                                        ephemeral=True)
                return
            t_id = tournament.id
            participant = await self._roster_target(ctx, session, tournament, team)
            if participant is None:
                return
            try:
                removed = await remove_teammate(session, participant, player.id)
            except ValueError as e:
                await ctx.followup.send(f"❌ {e}", ephemeral=True)
                return
            p_name = participant.team_name
            role_id = participant.role_id

        # Gated on `removed`, matching the reply below: the captain is
        # deliberately never in the roster table, so remove_teammate returns
        # False for them. Syncing unconditionally would strip the captain's
        # team role, dropping them out of every match room for the rest of
        # the event. Bundling _refresh_board here too keeps both roster
        # commands syncing the role in the same position relative to the
        # board refresh.
        synced = True
        if removed:
            await self._refresh_board(t_id)
            # Outside the session and load-bearing, not just tidy: a Discord
            # round-trip here would otherwise hold this write session's
            # SQLite connection open for its whole duration. Safe only
            # because AsyncSessionLocal sets expire_on_commit=False, so the
            # role_id captured above survives past the commit.
            synced = await sync_member(ctx.guild, role_id, str(player.id), add=False)

        if not removed:
            await ctx.followup.send(
                f"{player.mention} isn't on **{p_name}**'s roster.", ephemeral=True)
            return
        logger.info(f"{player.id} removed from team '{p_name}' in tournament {t_id} "
                    f"by {ctx.author.id}")
        warning = ("" if synced else
                   f"\n⚠️ Couldn't remove the **{p_name}** role — check that "
                   f"the bot's own role sits above it.")
        await ctx.followup.send(
            f"✅ {player.mention} is off **{p_name}**'s roster.{warning}", ephemeral=True)

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
                # Admin add is a comp: paid with no escrow, so it's seed-eligible even in a
                # paid tournament. Same transaction as the registration, so a crash can't
                # leave the team registered-but-pending. The captain isn't billed.
                comped = escrow.comp(participant)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)
            return
        verb = "registered" if created else "already registered"
        note = " (entry fee comped)" if comped else ""
        await self._refresh_board(tournament.id)
        await ctx.followup.send(
            f"✅ **{participant.team_name}** {verb} with {captain.mention} as captain{note}.",
            ephemeral=True,
        )

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
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament.", ephemeral=True)
                return
            t_id = tournament.id
        try:
            # Atomic: delete the participant and release its escrow hold together.
            res = await escrow.drop_with_refund(t_id, team)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)
            return
        refunded = res.get("refunded", 0)
        note = f" Entry fee ({refunded} tix) refunded to the captain's wallet." if refunded else ""
        await self._refresh_board(t_id)
        await ctx.followup.send(f"✅ **{res['team_name']}** removed.{note}", ephemeral=True)

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
            role_ids = await self._create_roles_for_start(ctx)
        except discord.HTTPException as e:
            # discord.Forbidden (missing "Manage Roles") is a subclass of this.
            # Scoped to only this call: a Discord failure from a LATER step
            # (posting the schedule, refreshing the board, ...) must keep
            # propagating to py-cord's on_error instead of being misreported
            # here as a role-creation failure.
            await ctx.followup.send(f"❌ Could not create team roles: {e}", ephemeral=True)
            return
        try:
            try:
                res = await escrow.close_registration_and_seed(ctx.guild.id, random.Random())
            except Exception:
                # The start did not happen, so the roles must not survive it.
                # A failure in the rollback itself must not replace the
                # exception below (e.g. "already started") with an
                # unrelated transport error, so it's logged, not raised.
                try:
                    await delete_team_roles(ctx.guild, role_ids.values())
                except Exception:
                    logger.exception(
                        f"Could not roll back tournament roles {list(role_ids.values())} "
                        f"after a failed start in guild {ctx.guild.id}"
                    )
                raise
            try:
                await store_role_ids(role_ids)
            except Exception:
                # The tournament HAS started at this point (that transaction
                # already committed) -- the roles are real and already
                # assigned to players, so they must NOT be deleted to tidy a
                # bookkeeping gap. Log for manual recovery and keep going.
                logger.error(
                    f"Tournament {res['tournament_id']} started but role ids "
                    f"{role_ids} were not persisted; recover by hand"
                )
            tournament_id = res["tournament_id"]
            logger.info(f"Tournament {tournament_id} started in guild {ctx.guild.id} by {ctx.author.id}")
            await self._refresh_board(tournament_id)
            pot_line = f" 🏦 Prize pool: **{res['pot']} tix**." if res["fee"] > 0 else ""
            play = self._destination(ctx, PAIRINGS.setting)
            standings = self._destination(ctx, STANDINGS.setting)
            where = "" if play == standings == ctx.channel else (
                f" Pairings in {play.mention}, standings in {standings.mention}.")
            await ctx.followup.send(f"🏆 **{res['name']}** has started!{pot_line}{where}")
            await self._post_schedule(play, tournament_id)
            await self._post_standings(standings, tournament_id)
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
            await safe_refresh_match_views(self.bot, match.id)
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
            await ctx.followup.send(await self._run_finish(ctx, tournament_id))
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    async def _run_finish(self, source, tournament_id):
        """Complete a tournament, refresh the pinned standings, release its
        team roles, and return the announcement text.

        Shared by /tournament finish and the end-of-swiss prompt's "Finish now"
        button. The button had inlined its own two-line version, which silently
        dropped everything else this does: the payout hint (on a money
        tournament, the only prompt that tells the TO to pay it out), the audit
        line, the standings refresh, and the tournament's name and id. The
        prompt is the path a TO answers by default, so it must not be the
        poorer one.

        ``source`` is a ctx or an Interaction; only .guild and the invoking user
        are read. Raises ValueError from finish_tournament -- callers own how
        they surface it, and their own success wording around the text returned.
        """
        guild_id = str(source.guild.id)
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            if tournament is None:
                raise ValueError("Tournament not found.")
            tournament_name = tournament.name
            fee = tournament.entry_fee or 0
            champion = await finish_tournament(session, tournament_id)
        champ_text = f"Champion: **{champion.team_name}** 🏆" if champion else "No teams competed."
        actor = getattr(source, "author", None) or getattr(source, "user", None)
        logger.info(f"Tournament {tournament_id} finished in guild {guild_id} by "
                    f"{getattr(actor, 'id', 'unknown')}")
        payout_hint = ""
        if fee > 0:
            pool = await escrow.prize_pool(guild_id, tournament_id)
            if pool > 0 and not await escrow.is_paid_out(tournament_id):
                payout_hint = (f"\n🏦 Prize pool: **{pool} tix** — run `/tournament payout` "
                               f"(this tournament is #{tournament_id}) to distribute it.")
        await update_standings_message(self.bot, tournament_id)
        await self._drop_team_roles(source.guild, tournament_id)
        return (f"🏁 **{tournament_name}** (#{tournament_id}) is complete! "
                f"{champ_text}{payout_hint}")

    async def _drop_team_roles(self, guild, tournament_id):
        """Delete a finished tournament's team roles, and clear role_id only
        for the ones actually confirmed gone.

        Called from BOTH completion paths -- /tournament finish and next_round
        past the final. Never raises: a tournament is over either way, and a
        role that outlives it costs a slot against the guild's 250-role cap,
        not correctness.
        """
        try:
            async with db_session() as session:
                role_ids = [p.role_id
                            for p in await list_participants(session, tournament_id)]
            # Delete the real roles BEFORE forgetting their ids. delete_team_roles
            # reports back exactly which ids are now gone (deleted, or already
            # absent) -- a role Discord refuses to delete (its role moved above
            # the bot's, a 5xx, ...) is NOT in that set, so its id is left alone
            # below and stays in the database, which is the only thing that
            # makes it recoverable by hand.
            gone = await delete_team_roles(guild, role_ids)
            survivors = [r for r in role_ids if r and r not in gone]
            if survivors:
                logger.warning(
                    f"[team-roles] tournament {tournament_id}: roles {survivors} "
                    f"could not be deleted; their ids were kept for manual recovery")
            async with db_session() as session:
                for participant in await list_participants(session, tournament_id):
                    if participant.role_id in gone:
                        participant.role_id = None
        except Exception:
            logger.opt(exception=True).warning(
                f"[team-roles] cleanup failed for tournament {tournament_id}")

    @tournament.command(name="payout", description="Admin: distribute a tournament's prize pool to the winners")
    @has_bot_manager_role()
    async def payout(
        self,
        ctx,
        tournament_id: discord.Option(
            int, "Which tournament to pay out (defaults to the most recently finished)",
            required=False, default=None
        ),
        structure: discord.Option(
            str, "Override the declared payout split",
            choices=list(escrow.PAYOUT_CHOICES), required=False, default=None
        ),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        async with db_session() as session:
            if tournament_id is not None:
                tournament = await session.get(Tournament, tournament_id)
                if tournament is None or str(tournament.guild_id) != str(ctx.guild.id):
                    await ctx.followup.send(f"No tournament #{tournament_id} in this server.", ephemeral=True)
                    return
                if tournament.status != "completed":
                    await ctx.followup.send(
                        f"**{tournament.name}** is {tournament.status}, not completed — only a "
                        f"finished tournament can be paid out.", ephemeral=True)
                    return
            else:
                tournament = await get_latest_completed_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send(
                        "There is no completed tournament to pay out. Pass a `tournament_id` to target one.",
                        ephemeral=True)
                    return
            t_id, t_name = tournament.id, tournament.name
            fee = tournament.entry_fee or 0
            struct = structure or tournament.payout_structure or "winner_take_all"
            # Finishing order, not standings order: a cut tournament pays the
            # bracket winner, who may not be the swiss leader.
            placement = await get_final_placement(session, tournament.id)
            # Only teams that actually completed registration can win the pot.
            ranked = [(p.captain_user_id, p.team_name) for p in placement if p.status == "paid"]
            # A tournament can be finished early with results still missing — warn before paying.
            unreported = await count_unreported_matches(session, tournament.id)

        if fee <= 0:
            await ctx.followup.send(f"**{t_name}** had no entry fee — nothing to pay out.", ephemeral=True)
            return
        if await escrow.is_paid_out(t_id):
            await ctx.followup.send(f"**{t_name}** (#{t_id}) has already been paid out.", ephemeral=True)
            return
        pool = await escrow.prize_pool(str(ctx.guild.id), t_id)
        if pool <= 0:
            await ctx.followup.send(f"**{t_name}** has no prize pool to distribute.", ephemeral=True)
            return
        allocations = escrow.compute_allocations(pool, struct, ranked)
        if not allocations:
            await ctx.followup.send("No eligible (paid) winners to pay.", ephemeral=True)
            return

        # Preview + require confirmation before disbursing real value.
        warning = ""
        if unreported > 0:
            warning = (f"\n\n⚠️ **{unreported} match(es) still unreported** — these count as 0–0, so "
                       f"standings may not be final. Report the results first unless you're finishing "
                       f"intentionally.")
        embed = discord.Embed(
            title=f"Confirm payout — {t_name} (#{t_id})",
            description=(
                f"Prize pool: **{pool} tix** · Split: **{escrow.describe_structure(struct)}**\n\n"
                f"{_format_payout_lines(allocations)}"
                f"{warning}\n\n"
                f"This credits real tix to the winners' wallets and can't be undone."),
            color=discord.Color.orange() if unreported > 0 else discord.Color.gold(),
        )
        view = PayoutConfirmView(
            str(ctx.guild.id), t_id, t_name, pool, struct, allocations, ctx.author.id)
        view.message = await ctx.followup.send(embed=embed, view=view, ephemeral=True)

    @tournament.command(name="next_round", description="Admin: pair the next Swiss round (all results must be in)")
    @has_bot_manager_role()
    async def next_round(self, ctx):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        try:
            completed = None
            async with db_session() as session:
                tournament = await get_active_tournament(session, ctx.guild.id)
                if tournament is None:
                    await ctx.followup.send("There is no active tournament.", ephemeral=True)
                    return
                tournament_id = tournament.id
                tournament_name = tournament.name
                try:
                    new_round = await advance_round(session, tournament.id, random.Random())
                except SwissComplete as done:
                    # Completing is irreversible and one call away, so the TO
                    # chooses rather than having the choice made for them.
                    short = done.eligible < done.cut_to
                    view = PlayoffPromptView(self, tournament_id, done.cut_to, disabled=short)
                    # `top:` has min_value=2, so suggesting top:1 (or top:0)
                    # hands the TO a command Discord will refuse to send.
                    fallback = (
                        f" Use `/tournament playoff top:{done.eligible}` or a smaller size."
                        if done.eligible >= 2
                        else " Finish now to crown the Swiss leader."
                    )
                    note = (
                        f"\n\n⚠️ Only **{done.eligible}** eligible team(s) — not enough for a "
                        f"top {done.cut_to}.{fallback}"
                    ) if short else ""
                    view.message = await ctx.followup.send(
                        f"Swiss is over for **{tournament_name}**. Cut to top "
                        f"**{done.cut_to}**?{note}", view=view)
                    return
                if new_round is None:
                    # Finishing order, not standings: after a bracket the
                    # champion is whoever won the final, not the swiss leader.
                    placement = await get_final_placement(session, tournament.id)
                    # Defensive, not reachable today: start_tournament refuses
                    # fewer than 2 paid teams and remove_team refuses once
                    # registration closes, so an active tournament always has
                    # participants. Guards anyway in case those invariants move.
                    if not placement:
                        await ctx.followup.send("Tournament complete.", ephemeral=True)
                        return
                    winner = placement[0]
                    await ctx.followup.send(
                        f"🏁 **{tournament_name}** is complete! "
                        f"Champion: **{winner.team_name}** 🏆"
                    )
                    completed = tournament_id
                else:
                    new_round_id = new_round.id
                    new_round_number = new_round.round_number
                    new_round_label = round_label(
                        tournament.total_rounds, new_round_number, new_round.stage,
                        swiss_noun="Week")
            # Outside the session: both of these do Discord work, and the role
            # cleanup opens a session of its own.
            if completed is not None:
                await update_standings_message(self.bot, completed)
                await self._drop_team_roles(ctx.guild, completed)
                return
            play = self._destination(ctx, PAIRINGS.setting)
            await self._post_round_messages(play, new_round_id, new_round_number)
            if play != ctx.channel:
                await ctx.followup.send(f"✅ {new_round_label} pairings posted in {play.mention}.")
            await update_standings_message(self.bot, tournament_id)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)

    @tournament.command(name="playoff",
                        description="Admin: cut to a top-N single-elimination bracket")
    @has_bot_manager_role()
    async def playoff(
        self,
        ctx,
        top: discord.Option(
            int, "Cut size (defaults to the size declared at creation)",
            min_value=2, max_value=32, required=False, default=None,
        ),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer()
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament.", ephemeral=True)
                return
            tournament_id = tournament.id
        try:
            play, size = await self._run_playoff(ctx, tournament_id, top)
        except ValueError as e:
            await ctx.followup.send(f"❌ {e}", ephemeral=True)
            return
        await ctx.followup.send(f"🏆 Top {size} playoff started — bracket posted in {play.mention}.")

    async def _run_playoff(self, source, tournament_id, size=None):
        """Cut to the bracket, post its round, and refresh the pinned standings.

        Shared by /tournament playoff and the end-of-swiss prompt's "Start
        playoff" button. The refresh lives in here rather than in the callers
        because the button had been missing it: the bracket was posted while the
        pinned standings window still showed the last swiss round. Every other
        refresh path goes through that window, and this is what keeps this one
        there too.

        ``source`` is a ctx or an Interaction (only .guild/.channel are read, via
        _destination). Returns (channel, size) -- where the bracket was posted
        and the cut size actually used -- so callers word their own success
        message. Raises ValueError from start_playoff.
        """
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            declared = tournament.cut_to if tournament is not None else None
            new_round = await start_playoff(session, tournament_id, size)
            round_id, round_number = new_round.id, new_round.round_number
        play = self._destination(source, PAIRINGS.setting)
        await self._post_round_messages(play, round_id, round_number)
        await update_standings_message(self.bot, tournament_id)
        return play, size or declared

    @tournament.command(name="open_rooms",
                        description="Admin: open match rooms for a round that was posted without them")
    @has_bot_manager_role()
    async def open_rooms(
        self,
        ctx,
        round_number: discord.Option(
            int, "Round to open (defaults to the earliest round still missing rooms)",
            required=False, default=None,
        ),
    ):
        if not await self._check_enabled(ctx):
            return
        await ctx.defer(ephemeral=True)
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament.", ephemeral=True)
                return
            target_round, target_stage, match_ids = await self._rooms_needed(
                session, tournament.id, round_number)
            total_rounds = tournament.total_rounds

        if not match_ids:
            if round_number is not None:
                await ctx.followup.send(
                    f"Nothing to do — {round_label(total_rounds, round_number, target_stage, swiss_noun='Week')} "
                    f"has no room-less matches "
                    f"(every playable match already has a room, or that round doesn't exist).",
                    ephemeral=True)
            else:
                await ctx.followup.send(
                    "Nothing to do — every round already has its rooms.", ephemeral=True)
            return

        opened = 0
        for match_id in match_ids:
            async with db_session() as session:
                facts = await match_facts(session, match_id)
            if facts is None:
                continue
            match, a_name, b_name, _label, _draft = facts
            if not match.pairings_channel_id or not match.pairings_message_id:
                continue
            channel = self.bot.get_channel(int(match.pairings_channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(match.pairings_channel_id))
                except discord.HTTPException as e:
                    logger.warning(f"open_rooms: pairings channel for match {match_id} unreachable: {e}")
                    continue
            try:
                message = await channel.fetch_message(int(match.pairings_message_id))
            except discord.HTTPException as e:
                logger.warning(f"open_rooms: pairing message for match {match_id} unreachable: {e}")
                continue
            thread = await create_match_room(message, match_id)
            if thread is None:
                continue
            try:
                await message.edit(content=render_pairing_line(a_name, b_name, str(thread.id)))
            except discord.HTTPException as e:
                logger.warning(f"open_rooms: could not add the room link for match {match_id}: {e}")
            opened += 1

        logger.info(f"open_rooms opened {opened} room(s) for "
                    f"{round_label(total_rounds, target_round, target_stage, swiss_noun='Week')} of tournament "
                    f"{tournament.id} by {ctx.author.id}")
        if opened == 0:
            await ctx.followup.send(
                f"Nothing to do — {round_label(total_rounds, target_round, target_stage, swiss_noun='Week')}'s "
                f"matches already have rooms, or Discord "
                f"refused every one of them.", ephemeral=True)
        else:
            await ctx.followup.send(
                f"✅ Opened {opened} room(s) for "
                f"{round_label(total_rounds, target_round, target_stage, swiss_noun='Week')}.",
                ephemeral=True)

    async def _rooms_needed(self, session, tournament_id, round_number):
        """(round_number, stage, [match_id, ...]) of the playable, unreported,
        non-bye matches in that round that have a pairing message but no
        room -- exactly what /tournament open_rooms creates rooms for.

        round_number=None searches every round in ascending order and returns
        the first that has any; that round's number, stage and matches come
        back together, so a caller never has to re-derive them. The stage
        comes from the row rather than from comparing the number against
        total_rounds -- that arithmetic proxy is what named the semifinal
        "Week 4". (None, None, []) means nothing needs a room anywhere in the
        tournament (or, when a specific round_number was passed, in that
        round).
        """
        stmt = (
            select(TournamentMatch, TournamentRound.round_number, TournamentRound.stage)
            .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
            .where(
                TournamentRound.tournament_id == tournament_id,
                TournamentMatch.is_bye.is_(False),
                TournamentMatch.team_a_wins.is_(None),
                TournamentMatch.pairings_message_id.isnot(None),
                TournamentMatch.thread_id.is_(None),
            )
            .order_by(TournamentRound.round_number)
        )
        if round_number is not None:
            stmt = stmt.where(TournamentRound.round_number == round_number)
        rows = (await session.execute(stmt)).all()
        if not rows:
            return None, None, []
        found_round = round_number if round_number is not None else rows[0][1]
        stage = next(st for _, r, st in rows if r == found_round)
        return found_round, stage, [m.id for m, r, _ in rows if r == found_round]

    def _destination(self, ctx, setting):
        """Where a tournament message goes: the configured channel, else here.

        Falling back to the invoking channel keeps every guild that never ran
        /tournament setup_channels working exactly as it did before.
        """
        return resolve_channel(ctx.guild, get_config(ctx.guild.id), setting) or ctx.channel

    async def _post_schedule(self, channel, tournament_id):
        """Post the whole schedule, one message per match. Swiss has one round;
        all-open formats reveal every round at once.

        Only the first round gets rooms created here. An all-open format's
        remaining rounds can number in the dozens of matches, and opening a
        room is ~5 Discord calls per match -- doing that for every round in
        one command would stall /tournament start for the better part of a
        minute. Their lines still post (so standings/pairings are complete
        immediately); an admin opens their rooms afterward with
        /tournament open_rooms. Swiss is unaffected: start only ever creates
        round 1, so this loop never sees a second round for it.
        """
        async with db_session() as session:
            rounds = (await session.execute(
                select(TournamentRound)
                .where(TournamentRound.tournament_id == tournament_id)
                .order_by(TournamentRound.round_number)
            )).scalars().all()
            round_meta = [(r.id, r.round_number) for r in rounds]
        for i, (round_id, round_number) in enumerate(round_meta):
            await self._post_round_messages(channel, round_id, round_number, create_rooms=(i == 0))

    async def _post_round_messages(self, channel, round_id, round_number, create_rooms=True):
        """Post a week header, then one message per match. Each playable match
        gets its own room (a thread + pinned control message, created off its
        pairing message) and its line carries a link to it; byes and
        already-reported matches show as text with no room.

        create_rooms=False skips room creation for every playable match (used
        by _post_schedule for an all-open format's later rounds); the header
        then says how to open them later, and the pairing line/ids still post
        and persist so /tournament open_rooms has something to act on.

        Takes a channel rather than the interaction: followup.send can only
        answer in the channel the command was typed in, and pairings may be
        destined for the play channel instead.
        """
        async with db_session() as session:
            matches = (await session.execute(
                select(TournamentMatch).where(TournamentMatch.round_id == round_id)
            )).scalars().all()
            # A swiss bye is a result (points are awarded); a bracket bye is
            # the absence of a match — the seed simply sits this round out and
            # scores nothing, so it must not be announced as an "auto win".
            round_ = await session.get(TournamentRound, round_id)
            bye_note = ("— bye, advances (no match, no points)"
                        if is_playoff(round_)
                        else "— BYE (auto win)")
            tournament = (await session.get(Tournament, round_.tournament_id)
                          if round_ is not None else None)
            # Bracket rounds are numbered past total_rounds, so the header used
            # to post the semifinal of a 3-round swiss as "Week 4 pairings".
            label = round_label(
                tournament.total_rounds if tournament is not None else round_number,
                round_number,
                round_.stage if round_ is not None else STAGE_SWISS,
                swiss_noun="Week")
            rows = []
            for m in matches:
                part_a = await session.get(TournamentParticipant, m.team_a_participant_id)
                if m.is_bye:
                    rows.append((m.id, f"• **{part_a.team_name}** {bye_note}", None))
                else:
                    part_b = await session.get(TournamentParticipant, m.team_b_participant_id)
                    if m.team_a_wins is None:
                        rows.append((m.id, None, (part_a.team_name, part_b.team_name)))
                    else:
                        rows.append((m.id, f"• **{part_a.team_name}** {m.team_a_wins}–"
                                           f"{m.team_b_wins} **{part_b.team_name}**", None))

        if create_rooms:
            await channel.send(f"**{label} pairings:**")
        else:
            await channel.send(
                f"**{label} pairings:** — rooms open with "
                f"`/tournament open_rooms {round_number}`")

        for match_id, text, names in rows:
            # One match's post failing (e.g. a transient Discord error) must not
            # take the rest of the round down with it -- a round missing one
            # line is far better than a round missing everything after it.
            try:
                # A bye or an already-reported match carries its own text and no
                # names; only a still-playable match needs the pairing line built.
                if names is None:
                    await channel.send(text)
                    continue
                a_name, b_name = names
                message = await channel.send(render_pairing_line(a_name, b_name))
            except discord.HTTPException as e:
                logger.error(f"Could not post the pairing line for match {match_id}; skipping it: {e}")
                continue
            async with db_session() as session:
                m = await session.get(TournamentMatch, match_id)
                m.pairings_channel_id = str(message.channel.id)
                m.pairings_message_id = str(message.id)
            if not create_rooms:
                continue
            thread = await create_match_room(message, match_id)
            if thread is not None:
                try:
                    await message.edit(content=render_pairing_line(a_name, b_name, str(thread.id)))
                except discord.HTTPException as e:
                    # The room exists and the control message is in it; losing the
                    # link costs discoverability, not the round.
                    logger.warning(f"Could not add the room link for match {match_id}: {e}")

    async def _post_standings(self, channel, tournament_id):
        """Post the standings message and remember it for in-place updates.

        Pinned on the way out: the message is edited all season and never
        reposted, so pinning is what keeps it reachable as the channel fills.
        """
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            participants = await get_standings_data(session, tournament_id)
            embed = create_standings_embed(
                tournament, participants, await current_round_stage(session, tournament))
        message = await channel.send(embed=embed)
        await safe_pin(message)
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
            await self._post_standings(self._destination(ctx, STANDINGS.setting), tournament_id)
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
        # expire_on_commit=False lets the ORM objects be read after the session closes.
        async with db_session() as session:
            tournament = await get_active_tournament(session, ctx.guild.id)
            if tournament is None:
                await ctx.followup.send("There is no active tournament in this server.", ephemeral=True)
                return
            rosters = {}
            if tournament.status == "registration":
                participants = await list_participants(session, tournament.id)
                rosters = await get_rosters(session, tournament.id)
            else:
                participants = await get_standings_data(session, tournament.id)
            stage = await current_round_stage(session, tournament)

        fee = tournament.entry_fee or 0
        if tournament.status == "registration":
            held = await escrow.prize_pool(str(ctx.guild.id), tournament.id) if fee > 0 else 0
            deficits = {}
            if fee > 0:
                pending = [p for p in participants if p.status != "paid"]
                balances = await wallet_service.balances_for(
                    str(ctx.guild.id), [p.captain_user_id for p in pending])
                deficits = {p.id: max(fee - balances.get(p.captain_user_id, 0), 0)
                            for p in pending}
            embed = create_registration_embed(tournament, participants, held, deficits,
                                              rosters=rosters)
        else:
            embed = create_standings_embed(tournament, participants, stage)
            if fee > 0:
                pool = await escrow.prize_pool(str(ctx.guild.id), tournament.id)
                embed.add_field(name="🏦 Prize pool", value=f"{pool} tix", inline=False)
        await ctx.followup.send(embed=embed)


def setup(bot):
    bot.add_cog(TournamentCog(bot))
