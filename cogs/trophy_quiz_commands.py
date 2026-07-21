import random
from datetime import datetime, timedelta
from io import BytesIO
from typing import List, Optional, Tuple

import discord
from discord.ext import commands
from loguru import logger
from sqlalchemy import and_, func, select, update

from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from helpers.magicprotools_helper import MagicProtoolsHelper
from helpers.pile_compositor import PileImageBuilder
from models import DraftSession, MatchResult, TrophyQuizSession
from services.draft_data_loader import load_from_spaces
from services.draft_log_store import map_discord_to_draftmancer, split_decklist, build_mtgo_deck_text
from services.trophy_quiz_service import select_two_decks
from quiz_views_module.trophy_quiz_views import TrophyQuizView
from utils import safe_pin

ELIGIBLE_DRAFT_DAYS = 365  # Look back 1 year for eligible drafts
MAX_POST_ATTEMPTS = 5  # trophy quiz: try up to N eligible drafts if prep fails


async def _generate_next_display_id(guild_id: str) -> int:
    """
    Generate the next sequential display ID for a guild.
    Returns the next available ID (max + 1), or 1 if no trophy quizzes exist.
    """
    async with db_session() as session:
        stmt = select(func.max(TrophyQuizSession.display_id)).where(
            TrophyQuizSession.guild_id == str(guild_id)
        )
        result = await session.execute(stmt)
        max_id = result.scalar()
        return (max_id or 0) + 1


async def _select_eligible_draft(
    guild_id: str, rng=random, exclude_draft_ids=None
) -> Tuple[Optional[DraftSession], Optional[List[dict]], Optional[dict]]:
    """
    Select an eligible draft and its 2-deck trophy quiz pair.

    Eligible = spaces_object_key set, session_type != "swiss", within
    ELIGIBLE_DRAFT_DAYS, and draft_session_id not already used for a trophy
    quiz in this guild. Lazily checks drafts (in random order), loading each
    draft's log + match results and running select_two_decks, stopping at the
    first draft that yields a non-None deck pair (select_two_decks already
    enforces the fully-reported / extreme / bucket rules).

    Returns (DraftSession, deck_pair, draft_data) or (None, None, None) if no
    eligible draft yields a valid pair. draft_data is returned alongside so
    callers can build MPT deck links without reloading it from Spaces.
    """
    cutoff = datetime.now() - timedelta(days=ELIGIBLE_DRAFT_DAYS)
    exclude_draft_ids = exclude_draft_ids or set()

    async with db_session() as session:
        used_stmt = select(TrophyQuizSession.draft_session_id).where(
            TrophyQuizSession.guild_id == str(guild_id)
        )
        used_result = await session.execute(used_stmt)
        used_draft_ids = {row[0] for row in used_result.fetchall()} | set(exclude_draft_ids)

        stmt = select(DraftSession).where(
            and_(
                DraftSession.guild_id == str(guild_id),
                DraftSession.spaces_object_key.isnot(None),
                DraftSession.session_type != "swiss",
                DraftSession.draft_start_time >= cutoff,
            )
        )
        result = await session.execute(stmt)
        eligible_drafts = [
            draft for draft in result.scalars().all()
            if draft.session_id not in used_draft_ids
        ]

    if not eligible_drafts:
        logger.warning(f"No eligible drafts found for trophy quiz in guild {guild_id}")
        return None, None, None

    rng.shuffle(eligible_drafts)

    drafts_checked = 0
    for draft in eligible_drafts:
        drafts_checked += 1
        try:
            draft_data = await load_from_spaces(draft.spaces_object_key)
            if not draft_data:
                continue

            async with db_session() as session:
                matches_result = await session.execute(
                    select(MatchResult).where(MatchResult.session_id == draft.session_id)
                )
                match_results = list(matches_result.scalars().all())

            sign_ups = draft.sign_ups or {}
            deck_pair = select_two_decks(draft_data, sign_ups, match_results, rng)
            if deck_pair is None:
                continue

            logger.info(
                f"Selected draft {draft.session_id} (cube: {draft.cube}) for trophy quiz "
                f"after checking {drafts_checked} drafts"
            )
            return draft, deck_pair, draft_data

        except Exception as e:
            logger.warning(f"Error preparing trophy quiz decks for draft {draft.session_id}: {e}")
            continue

    logger.warning(
        f"No eligible draft yielded a valid trophy quiz pair for guild {guild_id} "
        f"(checked {drafts_checked} drafts)"
    )
    return None, None, None


class TrophyQuizCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _create_and_post_trophy_quiz(
        self,
        guild_id: str,
        channel: discord.TextChannel,
        draft_session: DraftSession,
        deck_pair: List[dict],
        posted_by: str,
        draft_data: dict,
    ) -> Optional[discord.Message]:
        """
        Create the TrophyQuizSession in the database and post the quiz message.

        Resolves each deck's MagicProTools deck-view link first (via
        Draftmancer id + MPT submission); aborts (no session created, no
        message posted) if either deck's link fails to build.

        Returns the posted message, or None on failure.
        """
        channel_id = str(channel.id)
        quiz_id = f"{guild_id}-{int(datetime.now().timestamp())}"

        try:
            display_id = await _generate_next_display_id(guild_id)
        except Exception as e:
            logger.error(f"Failed to generate display_id: {e}")
            return None  # Abort quiz creation

        mapping = map_discord_to_draftmancer(draft_data, draft_session.sign_ups or {})
        carddata = draft_data.get("carddata", {})
        splits = {
            deck["drafter_id"]: split_decklist(draft_data, mapping[deck["drafter_id"]])
            for deck in deck_pair
            if mapping.get(deck["drafter_id"])
        }

        mpt = MagicProtoolsHelper()
        for deck in deck_pair:
            dm_id = mapping.get(deck["drafter_id"])
            split = splits.get(deck["drafter_id"])
            deck_text = build_mtgo_deck_text(split, carddata) if split else None
            url = await mpt.submit_deck_view(dm_id, draft_data, deck_text) if deck_text else None
            if not url:
                logger.error(
                    f"[trophy-quiz] MPT deck view failed for draft {draft_session.session_id} "
                    f"drafter {deck['drafter_id']} (dm {dm_id}); aborting"
                )
                return None  # Abort quiz creation - no session, no post
            deck["mpt_url"] = url

        # Pile images are kept in a list parallel to deck_pair/decks, never
        # attached to the deck dicts, so the raw bytes can't leak into the
        # DB row or the TrophyQuizView metadata.
        pile = PileImageBuilder()
        pile_images = []
        for deck in deck_pair:
            split = splits.get(deck["drafter_id"])
            image = await pile.build(split["main"], split["side"], carddata) if split else None
            if not image:
                logger.error(
                    f"[trophy-quiz] pile image failed for draft {draft_session.session_id} "
                    f"drafter {deck['drafter_id']} (dm {mapping.get(deck['drafter_id'])}); aborting"
                )
                return None  # abort: no session, no post (triggers try-next-draft)
            pile_images.append(image.getvalue())

        # Assign slots A/B; keep the pool text (and mpt_url) the view renders from.
        decks = [{"slot": "A", **deck_pair[0]}, {"slot": "B", **deck_pair[1]}]

        async with db_session() as session:
            quiz_session = TrophyQuizSession(
                quiz_id=quiz_id,
                display_id=display_id,
                guild_id=str(guild_id),
                channel_id=channel_id,
                draft_session_id=draft_session.session_id,
                decks=decks,
                posted_by=str(posted_by),
            )
            session.add(quiz_session)
            await session.commit()

        logger.info(f"Created trophy quiz session {quiz_id} with display_id=#{display_id}")

        embed = self.create_trophy_quiz_embed(draft_session, display_id)
        view = TrophyQuizView(quiz_id, decks)

        image_embeds, files = [], []
        for deck, image_bytes in zip(decks, pile_images):
            fname = f"deck_{deck['slot'].lower()}_{quiz_id}.jpg"
            files.append(discord.File(fp=BytesIO(image_bytes), filename=fname))
            img_embed = discord.Embed(title=f"Deck {deck['slot']}", color=discord.Color.gold())
            img_embed.set_image(url=f"attachment://{fname}")
            image_embeds.append(img_embed)

        try:
            message = await channel.send(embeds=[embed, *image_embeds], files=files, view=view)
        except Exception as e:
            logger.error(f"Failed to post trophy quiz message: {e}", exc_info=True)
            return None

        try:
            async with db_session() as session:
                await session.execute(
                    update(TrophyQuizSession)
                    .where(TrophyQuizSession.quiz_id == quiz_id)
                    .values(message_id=str(message.id))
                )
                await session.commit()
            logger.info(f"Posted trophy quiz message {message.id} in channel {channel_id}")
        except Exception as e:
            # The quiz is already live (posted above) — a failure to stamp
            # message_id must not be reported to the mod as a failed post.
            # Only the share deep-link degrades (falls back to "Trophy Quiz #N").
            logger.error(
                f"[trophy-quiz] failed to record message_id for quiz {quiz_id} "
                f"(message {message.id} posted successfully): {e}", exc_info=True
            )

        await safe_pin(message)

        return message

    async def _select_prep_and_post(
        self, guild_id: str, channel: discord.TextChannel, posted_by: str
    ) -> Optional[discord.Message]:
        """Select an eligible draft, prep it (MPT links + pile images), and post.
        If prep fails, exclude that draft and try the next eligible one, up to
        MAX_POST_ATTEMPTS."""
        exclude: set = set()
        for attempt in range(MAX_POST_ATTEMPTS):
            draft_session, deck_pair, draft_data = await _select_eligible_draft(
                guild_id, exclude_draft_ids=exclude
            )
            if not draft_session:
                break
            message = await self._create_and_post_trophy_quiz(
                guild_id=guild_id,
                channel=channel,
                draft_session=draft_session,
                deck_pair=deck_pair,
                posted_by=posted_by,
                draft_data=draft_data,
            )
            if message:
                return message
            logger.warning(
                f"[trophy-quiz] prep failed for draft {draft_session.session_id}; "
                f"trying next eligible draft (attempt {attempt + 1}/{MAX_POST_ATTEMPTS})"
            )
            exclude.add(draft_session.session_id)
        logger.error(
            f"[trophy-quiz] no eligible draft could be prepped for guild {guild_id} "
            f"after {MAX_POST_ATTEMPTS} attempts"
        )
        return None

    def create_trophy_quiz_embed(self, draft_session: DraftSession, display_id: Optional[int] = None) -> discord.Embed:
        """Create the public trophy quiz embed."""
        title = "🏆 Trophy Record Quiz"
        if display_id:
            title = f"🏆 Trophy Record Quiz #{display_id}"

        embed = discord.Embed(
            title=title,
            description=(
                "Two decks from the same pod: **one finished with a winning record "
                "(2-1 or 3-0), the other with a losing record (1-2 or 0-3)** — but which "
                "is which?\n\n"
                "Pick a record for **Deck A** and **Deck B** below, then hit **Submit**!"
            ),
            color=discord.Color.gold(),
        )

        # Deliberately no cube/date on the public embed — those are a lookup key
        # to the pod (find who trophied without reading the decks). Keep it anonymous.

        footer_text = "Guess both records, submit, and share your score!"
        if display_id:
            footer_text = f"Trophy Quiz #{display_id} • {footer_text}"
        embed.set_footer(text=footer_text)

        return embed

    @discord.slash_command(
        name='post_trophy_quiz',
        description='[MOD] Post a trophy record quiz for the channel',
        guild_ids=None
    )
    @has_bot_manager_role()  # accepts Bot Lord / Bot Manager roles OR Manage Roles
    async def post_trophy_quiz(self, ctx):
        """Post a public trophy record quiz that all users can participate in."""
        logger.info(f"Post trophy quiz command received from user {ctx.author.id} in guild {ctx.guild.id}")
        await ctx.response.defer(ephemeral=True)

        try:
            message = await self._select_prep_and_post(str(ctx.guild.id), ctx.channel, str(ctx.author.id))
        except Exception as e:
            # Mirror the scheduler's guard: an unexpected raise must still be
            # flagged to the mod (the interaction is already deferred), not
            # propagate and leave a silently-expired command.
            logger.error(f"Error posting trophy quiz for guild {ctx.guild.id}: {e}", exc_info=True)
            message = None

        if not message:
            await ctx.followup.send(
                "No eligible draft could be turned into a trophy quiz right now "
                "(couldn't build the deck views/images). Please try again.",
                ephemeral=True,
            )
            return
        await ctx.followup.send("✅ Trophy quiz posted!", ephemeral=True)

    async def post_scheduled_trophy_quiz(self, channel_id) -> bool:
        """
        Post a trophy quiz as part of scheduled task (called by unified scheduler).
        Returns True if successful, False otherwise.
        """
        try:
            logger.info(f"Scheduled trophy quiz posting to channel {channel_id}")

            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                logger.error(f"Channel {channel_id} not found")
                return False

            guild = channel.guild
            if not guild:
                logger.error(f"Guild not found for channel {channel_id}")
                return False

            message = await self._select_prep_and_post(str(guild.id), channel, "scheduler")
            return message is not None

        except Exception as e:
            logger.error(f"Error posting scheduled trophy quiz to channel {channel_id}: {e}", exc_info=True)
            return False


def setup(bot):
    bot.add_cog(TrophyQuizCommands(bot))
