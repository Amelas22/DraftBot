import discord
import random
from discord.ext import commands
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, update, and_
from database.db_session import db_session
from models import DraftSession, QuizSession
from services.draft_analysis import DraftAnalysis
from quiz_views_module.quiz_views import QuizPublicView
from helpers.magicprotools_helper import MagicProtoolsHelper


class QuizCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(
        name='post_quiz',
        description='[MOD] Post a draft pick quiz for the channel',
        guild_ids=None
    )
    @commands.has_permissions(manage_channels=True)  # Mod permission check
    async def post_quiz(self, ctx):
        """Post a public quiz that all users can participate in"""
        logger.info(f"Post quiz command received from user {ctx.author.id} in guild {ctx.guild.id}")
        await ctx.response.defer(ephemeral=True)  # Mod sees ephemeral confirmation

        # Select a random draft from the last year for this guild
        one_year_ago = datetime.now() - timedelta(days=365)

        async with db_session() as session:
            stmt = select(DraftSession).where(
                and_(
                    DraftSession.guild_id == str(ctx.guild.id),
                    DraftSession.spaces_object_key.isnot(None),
                    DraftSession.draft_start_time >= one_year_ago
                )
            )
            result = await session.execute(stmt)
            eligible_drafts = result.scalars().all()

        if not eligible_drafts:
            await ctx.followup.send(
                "No eligible drafts found from the last year in this guild.\n"
                "Drafts must have stored data in Spaces.",
                ephemeral=True
            )
            return

        # Randomly select one draft from the eligible ones
        selected_draft = random.choice(eligible_drafts)
        logger.info(f"Selected draft {selected_draft.session_id} (cube: {selected_draft.cube}) from {len(eligible_drafts)} eligible drafts")

        # 2. Load draft analysis
        try:
            analysis = await DraftAnalysis.from_session(selected_draft)
            if analysis is None:
                logger.error(f"DraftAnalysis.from_session returned None for draft {selected_draft.session_id}")
                await ctx.followup.send(
                    "Failed to load draft data (no analysis data available). Please try again.",
                    ephemeral=True
                )
                return
        except Exception as e:
            logger.error(f"Failed to load draft analysis: {e}", exc_info=True)
            await ctx.followup.send(
                "Failed to load draft data. Please try again.",
                ephemeral=True
            )
            return

        # 3. Trace Pack 0, first 4 picks
        try:
            pack_trace = analysis.trace_pack(pack_num=0, length=4)
        except Exception as e:
            logger.error(f"Failed to trace pack: {e}", exc_info=True)
            await ctx.followup.send(
                "Could not trace pack rotation. Please try again.",
                ephemeral=True
            )
            return

        if not pack_trace or len(pack_trace.picks) < 4:
            logger.warning(f"Pack trace incomplete: {len(pack_trace.picks) if pack_trace else 0} picks")
            await ctx.followup.send(
                "Could not trace pack rotation. Please try again.",
                ephemeral=True
            )
            return

        # 4. Load raw draft data and create MagicProTools visualization
        from services.draft_data_loader import load_from_spaces

        draft_data = None
        mpt_url = None
        if selected_draft.spaces_object_key:
            draft_data = await load_from_spaces(selected_draft.spaces_object_key)
            if draft_data:
                mpt_url = await self.create_pack_visualization_url(pack_trace, draft_data)
                logger.info(f"Generated MagicProTools URL: {mpt_url}")

        # 5. Create QuizSession in database
        quiz_id = f"{ctx.guild.id}-{int(datetime.now().timestamp())}"

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
                guild_id=str(ctx.guild.id),
                channel_id=str(ctx.channel.id),
                draft_session_id=selected_draft.session_id,
                pack_trace_data=pack_trace_data,
                correct_answers=correct_answers,
                posted_by=str(ctx.author.id)
            )
            session.add(quiz_session)
            await session.commit()

        logger.info(f"Created quiz session {quiz_id}")

        # 6. Post public message with quiz
        embed = self.create_quiz_embed(selected_draft, pack_trace, analysis, mpt_url)
        view = QuizPublicView(quiz_id, analysis, pack_trace)

        message = await ctx.channel.send(embed=embed, view=view)

        # 6. Update QuizSession with message_id
        async with db_session() as session:
            await session.execute(
                update(QuizSession)
                .where(QuizSession.quiz_id == quiz_id)
                .values(message_id=str(message.id))
            )
            await session.commit()

        logger.info(f"Posted quiz message {message.id} in channel {ctx.channel.id}")

        # 7. Confirm to mod
        await ctx.followup.send(
            f"✅ Quiz posted! Draft: {selected_draft.cube} from {selected_draft.draft_start_time.strftime('%Y-%m-%d')}",
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

            # Select a random draft from the last year for this guild
            one_year_ago = datetime.now() - timedelta(days=365)

            async with db_session() as session:
                stmt = select(DraftSession).where(
                    and_(
                        DraftSession.guild_id == str(guild.id),
                        DraftSession.spaces_object_key.isnot(None),
                        DraftSession.draft_start_time >= one_year_ago
                    )
                )
                result = await session.execute(stmt)
                eligible_drafts = result.scalars().all()

            if not eligible_drafts:
                logger.warning(f"No eligible drafts found for guild {guild.id}")
                return False

            # Randomly select one draft from the eligible ones
            selected_draft = random.choice(eligible_drafts)
            logger.info(f"Selected draft {selected_draft.session_id} (cube: {selected_draft.cube}) from {len(eligible_drafts)} eligible drafts")

            # Load draft analysis
            try:
                analysis = await DraftAnalysis.from_session(selected_draft)
                if analysis is None:
                    logger.error(f"DraftAnalysis.from_session returned None for draft {selected_draft.session_id}")
                    return False
            except Exception as e:
                logger.error(f"Failed to load draft analysis: {e}", exc_info=True)
                return False

            # Trace Pack 0, first 4 picks
            try:
                pack_trace = analysis.trace_pack(pack_num=0, length=4)
            except Exception as e:
                logger.error(f"Failed to trace pack: {e}", exc_info=True)
                return False

            if not pack_trace or len(pack_trace.picks) < 4:
                logger.warning(f"Pack trace incomplete: {len(pack_trace.picks) if pack_trace else 0} picks")
                return False

            # Load raw draft data and create MagicProTools visualization
            from services.draft_data_loader import load_from_spaces

            draft_data = None
            mpt_url = None
            if selected_draft.spaces_object_key:
                draft_data = await load_from_spaces(selected_draft.spaces_object_key)
                if draft_data:
                    mpt_url = await self.create_pack_visualization_url(pack_trace, draft_data)
                    logger.info(f"Generated MagicProTools URL: {mpt_url}")

            # Create QuizSession in database
            quiz_id = f"{guild.id}-{int(datetime.now().timestamp())}"

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
                    guild_id=str(guild.id),
                    channel_id=str(channel_id),
                    draft_session_id=selected_draft.session_id,
                    pack_trace_data=pack_trace_data,
                    correct_answers=correct_answers,
                    posted_by="scheduler"  # Automated post
                )
                session.add(quiz_session)
                await session.commit()

            logger.info(f"Created quiz session {quiz_id}")

            # Post public message with quiz
            embed = self.create_quiz_embed(selected_draft, pack_trace, analysis, mpt_url)
            view = QuizPublicView(quiz_id, analysis, pack_trace)

            message = await channel.send(embed=embed, view=view)

            # Update QuizSession with message_id
            async with db_session() as session:
                await session.execute(
                    update(QuizSession)
                    .where(QuizSession.quiz_id == quiz_id)
                    .values(message_id=str(message.id))
                )
                await session.commit()

            logger.info(f"Posted scheduled quiz message {message.id} in channel {channel_id}")
            return True

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
