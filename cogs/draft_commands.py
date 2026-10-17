import discord
from discord.commands import option
from discord.ext import commands
from loguru import logger
from modals import CubeDraftSelectionView, StakedCubeDraftSelectionView

from database.db_session import db_session
from session import DraftSession, MatchResult
from views import MatchResultSelect
from config import is_money_server
from services.donation_day import get_event, resolve_draft_kind
from helpers.display_names import get_display_name
from helpers.permissions import is_bot_manager
from helpers.substitutes import is_sub_target_channel, resolve_sub_grant
from helpers.team_names import BLUE, RED

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # @commands.Cog.listener()
    # async def on_application_command_error(self, ctx, error):
    #     logger.error(f"Command error: {error}", exc_info=True)
        
    # # Add this to debug interaction handling
    # @commands.Cog.listener()
    # async def on_interaction(self, interaction):
    #     logger.info(f"Received interaction: {interaction.data}")

    @discord.slash_command(name='draft', description='Start a cube draft with team settings based on server configuration', guild_ids=None)
    @option("kind", str, description="Staked or donation (donation is the default during a donation day)",
            choices=["staked", "donation"], default=None, required=False)
    async def draft(self, ctx, kind: str | None = None):
        logger.info(f"Received draft command from user {ctx.author.id} in guild {ctx.guild.id}")

        event = get_event(ctx.guild.id)
        kind, refusal = resolve_draft_kind(
            kind, money_server=is_money_server(ctx.guild.id),
            event_active=bool(event and event.get("active")),
            # Only asked when it can change the answer: an explicit choice that
            # the role gates.
            is_manager=await is_bot_manager(ctx) if kind else False)
        if refusal:
            await ctx.response.send_message(refusal, ephemeral=True)
            return

        # Check if this is a money server
        if kind is not None:
            logger.info(f"Using {kind} draft view for money server {ctx.guild.id}")
            view = StakedCubeDraftSelectionView(guild_id=ctx.guild.id,
                                                donation=kind == "donation")
            await ctx.response.send_message(f"Select a cube for the {kind} draft:",
                                            view=view, ephemeral=True)
        else:
            logger.info(f"Using standard draft view for free server {ctx.guild.id}")
            view = CubeDraftSelectionView(session_type="random", guild_id=ctx.guild.id)
            await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)
        # The library's cubes gain their badges once the picker is up.
        view.mark_once_sent(ctx.interaction)

    # @discord.slash_command(name='start_draft', description='Start a team draft with random teams', guild_ids=None)
    # async def start_draft(self, ctx):
    #     logger.info("Received start_draft command")
    #     view = CubeDraftSelectionView(session_type="random")
    #     await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)

    # Disabled — Winston cube/draft flow has known bugs (see PR #298 follow-up).
    # Keep the code in place; we'll re-enable once the cube selection issues are fixed.
    # @discord.slash_command(name='winston_draft', description='Start a winston draft', guild_ids=None)
    # async def winston_draft(self, ctx):
    #     logger.info("Received winston_draft command")
    #     view = CubeDraftSelectionView(session_type="winston", guild_id=ctx.guild.id)
    #     await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)

    @discord.slash_command(name='premade_draft', description='Start a team draft with premade teams', guild_ids=None)
    async def premade_draft(self, ctx):
        logger.info("Received premade_draft command")
        overrides = None
        if isinstance(ctx.channel, discord.Thread):
            from match_control_view import match_room_context
            try:
                async with db_session() as session:
                    context = await match_room_context(session, ctx.channel.id)
            except Exception:
                # The tournament lookup is an enhancement to a general-purpose
                # command — it must never be able to break an ordinary draft.
                # Fall through to the pre-existing behaviour (typed names plus
                # the fuzzy nudge); the user sees the difference immediately
                # because the modal asks for team names again.
                logger.exception(
                    f"match_room_context failed for thread {ctx.channel.id}; "
                    "falling back to the ordinary /premade_draft flow"
                )
                context = None
            if context is not None:
                # A match thread names its own teams, so the modal never asks —
                # there is no wrong name to type, and draft side A is match side A.
                _match_id, overrides, block = context
                if block is not None:
                    await ctx.response.send_message(block, ephemeral=True)
                    return
        view = CubeDraftSelectionView(session_type="premade", guild_id=ctx.guild.id,
                                      session_details_overrides=overrides)
        await ctx.response.send_message("Select a cube:", view=view, ephemeral=True)
        view.mark_once_sent(ctx.interaction)
        
    # @discord.slash_command(name='dynamic_stake', description='Start a team draft with random teams and customizable stakes')
    # async def staked_draft(self, ctx):
    #     logger.info("Received stakedraft command")
    #     view = StakedCubeDraftSelectionView()
    #     await ctx.response.send_message("Select a cube for the staked draft:", view=view, ephemeral=True)

    @discord.slash_command(
        name='report_results', 
        description='Report the result of your last unreported match',
        guild_ids=None
    )
    async def report_match(self, ctx):
        """Report the result of your latest unreported match"""
        logger.info(f"Received report command from user {ctx.author.id}")
        await ctx.response.defer(ephemeral=True)
        
        user_id = str(ctx.author.id)
        channel_id = str(ctx.channel_id)
        
        # Get draft session by channel
        draft_session = await DraftSession.get_by_channel_id(channel_id)
        if not draft_session:
            await ctx.followup.send("This command can only be used in active draft channels.", ephemeral=True)
            return
        
        # Check if user is participating
        if not draft_session.is_user_participating(user_id):
            await ctx.followup.send("You are not a participant in this draft.", ephemeral=True)
            return
        
        # Find unreported match for user
        match = await MatchResult.find_unreported_for_user(draft_session.session_id, user_id)
        if not match:
            await ctx.followup.send("You don't have any unreported matches in this draft.", ephemeral=True)
            return
        
        # Create match result UI
        await self._send_match_result_selector(ctx, match, draft_session.session_id)

    async def _send_match_result_selector(self, ctx, match, session_id):
        """Create and send the match result selection UI."""
        # Get player names
        player1 = ctx.guild.get_member(int(match.player1_id))
        player2 = ctx.guild.get_member(int(match.player2_id))
        
        if not player1 or not player2:
            await ctx.followup.send("Could not find one or both players for this match.", ephemeral=True)
            return
            
        player1_name = get_display_name(player1, ctx.guild)
        player2_name = get_display_name(player2, ctx.guild)
        
        # Create the select menu
        select_menu = MatchResultSelect(
            bot=self.bot,
            match_number=match.match_number,
            session_id=session_id,
            player1_name=player1_name,
            player2_name=player2_name
        )
        
        # Create a view and add the select menu
        view = discord.ui.View()
        view.add_item(select_menu)
        
        # Send the response with the select menu
        await ctx.followup.send(
            f"Report result for Match {match.match_number}: {player1_name} vs {player2_name}",
            view=view,
            ephemeral=True
        )

    @discord.slash_command(
        name='add_sub',
        description="Grant a substitute access to this draft's chat and a team chat",
        guild_ids=None
    )
    async def add_sub(
        self,
        ctx,
        user: discord.Option(discord.Member, "The substitute to grant access"),
        team: discord.Option(str, "Sub's team (only needed if you're not in this draft)",
                             choices=[discord.OptionChoice(name=RED.name, value="A"),
                                      discord.OptionChoice(name=BLUE.name, value="B")],
                             required=False, default=None),
    ):
        """Grant a substitute channel access without changing draft data."""
        logger.info(f"Received add_sub from {ctx.author.id} for {user.id} "
                    f"in channel {ctx.channel_id}")
        await ctx.response.defer(ephemeral=True)
        await self._do_add_sub(ctx, user, team)

    async def _do_add_sub(self, ctx, user, team):
        draft_session = await DraftSession.get_by_any_channel_id(ctx.channel_id)
        if not draft_session:
            await ctx.followup.send(
                "This command can only be used in a draft's channels.",
                ephemeral=True)
            return
        if not draft_session.channel_ids:
            await ctx.followup.send(
                "This draft's channels haven't been created yet — "
                "add the sub once teams and channels exist.", ephemeral=True)
            return

        is_admin = await is_bot_manager(ctx)
        decision, error = resolve_sub_grant(
            draft_session, str(ctx.author.id), str(user.id), is_admin, team)
        if error:
            await ctx.followup.send(error, ephemeral=True)
            return

        draft_chat_id = str(draft_session.draft_chat_channel)
        granted, failed = [], []
        for channel_id in draft_session.channel_ids:
            channel = ctx.guild.get_channel(int(channel_id))
            if not channel or not is_sub_target_channel(
                    channel.name, draft_session.friendly_id, decision.channel_prefix):
                continue
            try:
                # Same overwrites teammates get at channel creation
                await channel.set_permissions(
                    user, read_messages=True, manage_messages=True)
                granted.append(channel)
            except discord.HTTPException:
                logger.exception(f"add_sub: failed to set permissions on "
                                 f"channel {channel.id}")
                failed.append(channel)

        if not granted and not failed:
            await ctx.followup.send(
                "Could not find this draft's channels — "
                "they may have been deleted already.", ephemeral=True)
            return

        if granted:
            summary = (f"Granted {user.display_name} access to: "
                       + ", ".join(c.mention for c in granted))
        else:
            summary = f"Could not grant {user.display_name} access to any channels."
        if failed:
            summary += ("\n⚠️ Failed (Discord error): "
                        + ", ".join(c.mention for c in failed))
            if any(str(c.id) == draft_chat_id for c in failed):
                summary += ("\n⚠️ Skipped the public announcement because "
                            "the draft chat grant failed.")
        await ctx.followup.send(summary, ephemeral=True)
        logger.success(f"add_sub: granted {user.id} access to "
                       f"{[c.id for c in granted]} in draft "
                       f"{draft_session.session_id}")

        draft_chat = next(
            (c for c in granted if str(c.id) == draft_chat_id), None)
        if draft_chat:
            try:
                await draft_chat.send(
                    f"{user.mention} was added as a substitute for "
                    f"**{decision.team_display_name}** by "
                    f"{get_display_name(ctx.author, ctx.guild)}.")
            except discord.HTTPException:
                logger.warning(
                    f"add_sub: grant succeeded but the public announcement "
                    f"failed in channel {draft_chat.id}", exc_info=True)

def setup(bot):
    bot.add_cog(DraftCommands(bot))