import discord
import random
from typing import Optional, Tuple
from discord.ext import commands
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, update, and_
from database.db_session import db_session
from database.message_management import make_message_sticky
from models import DraftSession, QuizSession
from models.draft_domain import PackTrace
from services.draft_analysis import DraftAnalysis
from services.draft_data_loader import load_from_spaces
from quiz_views_module.quiz_views import QuizPublicView
from helpers.magicprotools_helper import MagicProtoolsHelper
from helpers.pack_compositor import PackCompositor
from config import get_config

# Quiz configuration constants
QUIZ_PACK_NUMBER = 0  # First pack
QUIZ_NUM_PICKS = 4  # Number of picks to quiz on
ELIGIBLE_DRAFT_DAYS = 365  # Look back 1 year for eligible drafts


class QuizCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _select_random_draft(self, guild_id: str) -> Optional[DraftSession]:
        """
        Select a random eligible draft from the last year.

        Args:
            guild_id: Guild ID to search drafts for

        Returns:
            Random DraftSession or None if no eligible drafts found
        """
        one_year_ago = datetime.now() - timedelta(days=ELIGIBLE_DRAFT_DAYS)

        async with db_session() as session:
            stmt = select(DraftSession).where(
                and_(
                    DraftSession.guild_id == str(guild_id),
                    DraftSession.spaces_object_key.isnot(None),
                    DraftSession.draft_start_time >= one_year_ago
                )
            )
            result = await session.execute(stmt)
            eligible_drafts = result.scalars().all()

        if not eligible_drafts:
            logger.warning(f"No eligible drafts found for guild {guild_id}")
            return None

        selected_draft = random.choice(eligible_drafts)
        logger.info(f"Selected draft {selected_draft.session_id} (cube: {selected_draft.cube}) from {len(eligible_drafts)} eligible drafts")
        return selected_draft

    async def _prepare_quiz_data(self, draft_session: DraftSession):
        """
        Load draft analysis, trace pack, and create visualization.

        Args:
            draft_session: Draft session to prepare quiz from

        Returns:
            Tuple of (analysis, pack_trace, mpt_url, draft_data) or None if preparation fails
        """
        # Load draft analysis
        try:
            analysis = await DraftAnalysis.from_session(draft_session)
            if analysis is None:
                logger.error(f"DraftAnalysis.from_session returned None for draft {draft_session.session_id}")
                return None
        except Exception as e:
            logger.error(f"Failed to load draft analysis: {e}", exc_info=True)
            return None

        # Trace pack
        try:
            pack_trace = analysis.trace_pack(pack_num=QUIZ_PACK_NUMBER, length=QUIZ_NUM_PICKS)
        except Exception as e:
            logger.error(f"Failed to trace pack: {e}", exc_info=True)
            return None

        if not pack_trace or len(pack_trace.picks) < QUIZ_NUM_PICKS:
            logger.warning(f"Pack trace incomplete: {len(pack_trace.picks) if pack_trace else 0} picks")
            return None

        # Create MagicProTools visualization and load draft data
        mpt_url = None
        draft_data = None
        if draft_session.spaces_object_key:
            draft_data = await load_from_spaces(draft_session.spaces_object_key)
            if draft_data:
                mpt_url = await self.create_pack_visualization_url(pack_trace, draft_data)
                logger.info(f"Generated MagicProTools URL: {mpt_url}")

        return (analysis, pack_trace, mpt_url, draft_data)

    async def _create_and_post_quiz(
        self,
        guild_id: str,
        channel_id: str,
        channel: discord.TextChannel,
        draft_session: DraftSession,
        analysis: DraftAnalysis,
        pack_trace: PackTrace,
        mpt_url: Optional[str],
        draft_data: Optional[dict],
        posted_by: str
    ) -> Optional[discord.Message]:
        """
        Create quiz session in database and post quiz message.

        Args:
            guild_id: Guild ID
            channel_id: Channel ID
            channel: Discord channel object
            draft_session: Source draft session
            analysis: Draft analysis
            pack_trace: Traced pack
            mpt_url: MagicProTools URL (optional)
            draft_data: Draft data from Spaces (optional)
            posted_by: User ID or "scheduler"

        Returns:
            Posted message or None on failure
        """
        # Create QuizSession in database
        quiz_id = f"{guild_id}-{int(datetime.now().timestamp())}"

        # Serialize pack trace and correct answers
        pack_trace_data = {
            "picks": [
                {
                    "user_name": pick.user_name,
                    "booster_ids": pick.booster_ids,
                    "picked_id": pick.picked_id
                }
                for pick in pack_trace.picks
            ]
        }
        correct_answers = [pick.picked_id for pick in pack_trace.picks]

        async with db_session() as session:
            quiz_session = QuizSession(
                quiz_id=quiz_id,
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                draft_session_id=draft_session.session_id,
                pack_trace_data=pack_trace_data,
                correct_answers=correct_answers,
                posted_by=str(posted_by)
            )
            session.add(quiz_session)
            await session.commit()

        logger.info(f"Created quiz session {quiz_id}")

        # Post public message with quiz
        embed = self.create_quiz_embed(draft_session, pack_trace, analysis, mpt_url)
        view = QuizPublicView(quiz_id, analysis, pack_trace)

        # Generate pack composite image if enabled and draft data available
        pack_image_file = None
        config = get_config(guild_id)
        quiz_images_config = config.get("features", {}).get("quiz_pack_images", {})

        logger.info(f"[QUIZ_IMAGE] Feature enabled: {quiz_images_config.get('enabled', False)}, draft_data available: {draft_data is not None}")

        if quiz_images_config.get("enabled", False) and draft_data:
            try:
                logger.info(f"[QUIZ_IMAGE] Starting pack composite generation for quiz {quiz_id}")
                compositor = PackCompositor(
                    card_width=quiz_images_config.get("card_width", 244),
                    card_height=quiz_images_config.get("card_height", 340),
                    border_pixels=quiz_images_config.get("border_pixels", 5)
                )
                first_pick = pack_trace.picks[0]
                carddata = draft_data.get("carddata", {})
                logger.info(f"[QUIZ_IMAGE] Carddata has {len(carddata)} cards, pack has {len(first_pick.booster_ids)} cards")

                image_bytes = await compositor.create_pack_composite(
                    first_pick.booster_ids,
                    carddata
                )

                if image_bytes:
                    logger.info(f"[QUIZ_IMAGE] Composite created successfully, size: {len(image_bytes.getvalue())} bytes")
                    pack_image_file = discord.File(
                        fp=image_bytes,
                        filename=f"quiz_pack_{quiz_id}.jpg"
                    )
                    # Update embed to reference the attachment
                    embed.set_image(url=f"attachment://quiz_pack_{quiz_id}.jpg")
                    logger.info(f"[QUIZ_IMAGE] Discord.File created and embed.set_image() called for quiz {quiz_id}")
                else:
                    logger.warning(f"[QUIZ_IMAGE] Composite generation returned None")
            except Exception as e:
                logger.error(f"[QUIZ_IMAGE] Pack composite generation failed: {e}", exc_info=True)
        else:
            logger.info(f"[QUIZ_IMAGE] Skipping pack image generation")

        # Post message with embed and optional pack image
        if pack_image_file:
            logger.info(f"[QUIZ_IMAGE] Posting quiz with pack image attachment")
            message = await channel.send(embed=embed, file=pack_image_file, view=view)
        else:
            logger.info(f"[QUIZ_IMAGE] Posting quiz without pack image")
            message = await channel.send(embed=embed, view=view)

        # Update QuizSession with message_id
        async with db_session() as session:
            await session.execute(
                update(QuizSession)
                .where(QuizSession.quiz_id == quiz_id)
                .values(message_id=str(message.id))
            )
            await session.commit()

        logger.info(f"Posted quiz message {message.id} in channel {channel_id}")

        # Make the message sticky (auto-reposts at bottom of channel)
        await make_message_sticky(
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            message=message,
            view=view
        )
        logger.info(f"Made quiz message {message.id} sticky")

        return message

    @discord.slash_command(
        name='post_quiz',
        description='[MOD] Post a draft pick quiz for the channel',
        guild_ids=None
    )
    @commands.has_permissions(manage_roles=True)  # Mod permission check
    async def post_quiz(self, ctx):
        """Post a public quiz that all users can participate in"""
        logger.info(f"Post quiz command received from user {ctx.author.id} in guild {ctx.guild.id}")
        await ctx.response.defer(ephemeral=True)

        # Select random draft
        draft_session = await self._select_random_draft(ctx.guild.id)
        if not draft_session:
            await ctx.followup.send(
                "No eligible drafts found from the last year in this guild.\n"
                "Drafts must have stored data in Spaces.",
                ephemeral=True
            )
            return

        # Prepare quiz data
        quiz_data = await self._prepare_quiz_data(draft_session)
        if not quiz_data:
            await ctx.followup.send(
                "Failed to load draft data. Please try again.",
                ephemeral=True
            )
            return

        analysis, pack_trace, mpt_url, draft_data = quiz_data

        # Create and post quiz
        message = await self._create_and_post_quiz(
            guild_id=str(ctx.guild.id),
            channel_id=str(ctx.channel.id),
            channel=ctx.channel,
            draft_session=draft_session,
            analysis=analysis,
            pack_trace=pack_trace,
            mpt_url=mpt_url,
            draft_data=draft_data,
            posted_by=str(ctx.author.id)
        )

        if not message:
            await ctx.followup.send(
                "Failed to post quiz. Please try again.",
                ephemeral=True
            )
            return

        # Confirm to mod
        await ctx.followup.send(
            f"✅ Quiz posted! Draft: {draft_session.cube} from {draft_session.draft_start_time.strftime('%Y-%m-%d')}",
            ephemeral=True
        )

    async def post_scheduled_quiz(self, channel_id):
        """
        Post a quiz as part of scheduled task (called by unified scheduler).
        Returns True if successful, False otherwise.
        """
        try:
            logger.info(f"Scheduled quiz posting to channel {channel_id}")

            # Get channel object
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                logger.error(f"Channel {channel_id} not found")
                return False

            guild = channel.guild
            if not guild:
                logger.error(f"Guild not found for channel {channel_id}")
                return False

            # Select random draft
            draft_session = await self._select_random_draft(guild.id)
            if not draft_session:
                return False

            # Prepare quiz data
            quiz_data = await self._prepare_quiz_data(draft_session)
            if not quiz_data:
                return False

            analysis, pack_trace, mpt_url, draft_data = quiz_data

            # Create and post quiz
            message = await self._create_and_post_quiz(
                guild_id=str(guild.id),
                channel_id=str(channel_id),
                channel=channel,
                draft_session=draft_session,
                analysis=analysis,
                pack_trace=pack_trace,
                mpt_url=mpt_url,
                draft_data=draft_data,
                posted_by="scheduler"
            )

            return message is not None

        except Exception as e:
            logger.error(f"Error posting scheduled quiz to channel {channel_id}: {e}", exc_info=True)
            return False

    async def create_pack_visualization_url(self, pack_trace, draft_data):
        """
        Create a MagicProTools URL to visualize the pack WITHOUT spoiling the pick.

        Returns the URL or None if creation fails.
        """
        try:
            mpt_helper = MagicProtoolsHelper()

            # Get the first pick to extract user_id and booster
            first_pick_data = pack_trace.picks[0]

            # Find the corresponding user in the original draft data
            user_id = None
            for uid, udata in draft_data["users"].items():
                if udata["userName"] == first_pick_data.user_name:
                    user_id = uid
                    break

            if not user_id:
                logger.warning(f"Could not find user {first_pick_data.user_name} in draft data")
                return None

            # Duplicate the first card and mark the duplicate as picked
            # This shows all 15 real cards while hiding the duplicate
            duplicate_card_id = first_pick_data.booster_ids[0]  # Duplicate first card
            booster_with_duplicate = first_pick_data.booster_ids + [duplicate_card_id]

            # Create a modified draft data that shows all 15 cards by picking the duplicate
            modified_draft_data = {
                "sessionID": draft_data["sessionID"],
                "time": draft_data["time"],
                "carddata": draft_data["carddata"],
                "users": {
                    user_id: {
                        "userName": draft_data["users"][user_id]["userName"],
                        "picks": [
                            {
                                "packNum": 0,
                                "pickNum": 0,
                                "booster": booster_with_duplicate,  # 16 cards (15 real + 1 duplicate)
                                "pick": [15]  # Pick the duplicate at index 15 (last card)
                            }
                        ]
                    }
                }
            }

            # Submit modified data to get a visual pack display without spoilers
            mpt_url = await mpt_helper.submit_to_api(user_id, modified_draft_data)
            return mpt_url

        except Exception as e:
            logger.error(f"Error creating pack visualization: {e}", exc_info=True)
            return None

    def create_quiz_embed(self, draft_session, pack_trace, analysis, mpt_url=None):
        """Create the public quiz embed"""
        embed = discord.Embed(
            title="🎯 Draft Pick Quiz Challenge",
            description="Can you predict which cards these players picked?\n\n"
                        "Click **Make Your Guesses** below to participate!",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Draft Info",
            value=f"**Cube:** {draft_session.cube}\n"
                  f"**Date:** {draft_session.draft_start_time.strftime('%Y-%m-%d')}\n"
                  f"**Pack:** 1 (Pack 0), Picks 1-4",
            inline=False
        )

        # Show player names in order
        player_names = "\n".join([
            f"{i+1}. {pick.user_name}"
            for i, pick in enumerate(pack_trace.picks)
        ])
        embed.add_field(
            name="Players (in pick order)",
            value=player_names,
            inline=False
        )

        # Show available cards (from first pick's booster)
        first_pick = pack_trace.picks[0]
        card_names = sorted([analysis.get_card(cid).name for cid in first_pick.booster_ids])
        cards_text = ", ".join(card_names)

        # Add MagicProTools link if available
        if mpt_url:
            embed.add_field(
                name="Visual Pack Display",
                value=f"[View Pack on MagicProTools]({mpt_url})\n\n"
                      f"(Shows Pack 1, Pick 1 with all 15 cards)",
                inline=False
            )

        embed.add_field(
            name="Available Cards (15 cards)",
            value=cards_text,
            inline=False
        )

        embed.set_footer(text="Submit your guesses and see your results instantly!")

        return embed


def setup(bot):
    bot.add_cog(QuizCommands(bot))
