"""
High-level statistics display functions.

This module formats player_stats data into Discord displays. It sits at the top of the dependency chain.
"""
import discord
from player_stats import create_stats_embed, get_player_statistics
from sqlalchemy import Integer, cast, func, select
from database.db_session import AsyncSessionLocal
from models import QuizStats, TrophyQuizSession, TrophyQuizSubmission
from models.player import PlayerStats
from helpers.skill import is_established, skill_rating
from services.ledger_stats import LedgerSnapshot


async def _player_skill_standing(player_id, guild_id):
    """Return (rating, provisional, rank, pool_size) for one player.

    rating/provisional are (None, None) when the player has no stored rating;
    provisional means fewer than enough rated games (random+staked+premade),
    read from the same row as mu/sigma.

    rank is the player's place among this guild's *established* players by
    rating, and pool_size how many of those there are — (None, None) for a
    provisional or unrated player. Ranking excludes short records because the
    display rating shrinks them toward 1500 but not always far enough to keep a
    three-game streak out of the top spots; that also makes the ranked pool the
    same population the "(provisional)" label already distinguishes. Ties share
    the better rank (competition ranking).

    One guild-wide read serves both halves: the rating is computed in Python
    (shrinkage), so ranking can't be a SQL ORDER BY, and once every row is in
    hand a second single-row query for the caller would be re-reading what we
    already have. Guild rosters are small enough that folding here is cheap.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                PlayerStats.player_id,
                PlayerStats.true_skill_mu,
                PlayerStats.true_skill_sigma,
                PlayerStats.games_won,
                PlayerStats.games_lost,
            ).where(
                PlayerStats.guild_id == str(guild_id),
                PlayerStats.true_skill_mu.isnot(None),
                PlayerStats.true_skill_sigma.isnot(None),
            )
        )
        rows = result.all()

    wanted = str(player_id)
    rating = provisional = None
    established = []
    for pid, mu, sigma, games_won, games_lost in rows:
        games = (games_won or 0) + (games_lost or 0)
        player_rating = skill_rating(mu, sigma, games)
        if is_established(games):
            established.append(player_rating)
        if pid == wanted:
            rating, provisional = player_rating, not is_established(games)

    if rating is None or provisional:
        return rating, provisional, None, None

    rank = sum(1 for other in established if other > rating) + 1
    return rating, provisional, rank, len(established)


async def _player_quiz_stats(player_id, guild_id):
    """Lifetime quiz stats for one player, as (pick_quiz, trophy_quiz) dicts —
    either is None when the player hasn't played that quiz type.

    Pick quiz reads the already-aggregated QuizStats row. Trophy quiz has no
    aggregate table, so finalized submissions are summed here, guild-scoped via
    the quiz session (submissions don't carry guild_id) — the same shape the
    trophy quiz leaderboard aggregates."""
    async with AsyncSessionLocal() as session:
        row = await session.get(QuizStats, (str(player_id), str(guild_id)))
        result = await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(TrophyQuizSubmission.points_earned), 0),
                # cast: summing the raw Boolean column makes SQLAlchemy coerce
                # the aggregate back to a bool (True), not a count
                func.coalesce(func.sum(cast(TrophyQuizSubmission.direction_correct, Integer)), 0),
            )
            .join(TrophyQuizSession,
                  TrophyQuizSubmission.quiz_id == TrophyQuizSession.quiz_id)
            .where(
                TrophyQuizSession.guild_id == str(guild_id),
                TrophyQuizSubmission.player_id == str(player_id),
                # Only committed answers count, matching the leaderboard; a
                # pending row is an initial guess never Kept/Changed.
                TrophyQuizSubmission.finalized.is_(True),
            )
        )
        played, points, direction_correct = result.one()

    pick = None
    if row is not None and row.total_quizzes:
        pick = {
            "played": row.total_quizzes,
            # `or 0`: the columns are nullable in the schema; insert-time
            # defaults make NULLs unlikely, but "None pts" must be impossible.
            "accuracy": row.accuracy_percentage or 0.0,
            "points": row.total_points or 0,
            "best": row.highest_quiz_score or 0,
        }
    trophy = None
    if played:
        trophy = {
            "played": played,
            "points": points,
            # Pre-computed like pick's `accuracy`: the embed builder
            # formats, it doesn't do arithmetic.
            "direction_pct": direction_correct / played * 100,
        }
    return pick, trophy


async def get_stats_embed_for_player(
    bot,
    player_id: str,
    guild_id: str,
    display_name: str = None
) -> discord.Embed:
    """
    Get stats embed for any player by their ID.

    This is a DRY helper function that can be used by both the regular /stats command
    and the admin /admin-stats command.

    Args:
        bot: Discord bot instance (for fetching user info)
        player_id: Discord user ID as string
        guild_id: Guild ID for scoping stats
        display_name: Optional display name (will fetch from Discord if None)

    Returns:
        Discord embed with player statistics across weekly, monthly, and lifetime timeframes
    """
    # Get user object for embed (needed for avatar, etc.)
    try:
        user = await bot.fetch_user(int(player_id))
    except Exception:
        # Create a mock user object if fetch fails (e.g., user left Discord)
        class MockUser:
            def __init__(self, user_id, name):
                self.id = int(user_id)
                self.display_name = name or "Unknown Player"
                self.avatar = None
        user = MockUser(player_id, display_name)

    # Get stats for all 3 timeframes -- one guild-history fetch, three folds
    snapshot = await LedgerSnapshot.fetch(guild_id)
    stats_weekly = await get_player_statistics(player_id, 'week', display_name, guild_id,
                                               snapshot=snapshot)
    stats_monthly = await get_player_statistics(player_id, 'month', display_name, guild_id,
                                                snapshot=snapshot)
    stats_lifetime = await get_player_statistics(player_id, None, display_name, guild_id,
                                                 snapshot=snapshot)

    # Skill rating from stored TrueSkill μ/σ, gated on lifetime rated games.
    rating, provisional, server_rank, rank_pool = await _player_skill_standing(player_id, guild_id)
    stats_lifetime['skill_rating'] = rating
    stats_lifetime['skill_provisional'] = provisional
    # Standing among the guild's established players; create_stats_embed decides
    # how deep a rank is still worth printing.
    stats_lifetime['server_rank'] = server_rank
    stats_lifetime['server_rank_pool'] = rank_pool

    # Lifetime quiz stats (pick + trophy), rendered as their own embed field.
    pick_quiz, trophy_quiz = await _player_quiz_stats(player_id, guild_id)
    stats_lifetime['pick_quiz_stats'] = pick_quiz
    stats_lifetime['trophy_quiz_stats'] = trophy_quiz

    # Create and return the embed
    embed = await create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime)
    return embed
