"""
Record MTGO match results into DraftBot.

Two layers:
  * record_match_result(...): the shared recorder. Mirrors views.MatchResultSelect.callback
    (writes the MatchResult row, then fires stats/Elo/streaks + ring-bearer for rated
    session types, and the victory/standings cascade that also writes the tournament /
    premade result). Kept as a standalone function so the MTGO worker path and (later) a
    refactored Discord select can share ONE implementation without touching the working view.
  * report_mtgo_match(...): the MTGO-worker entry point. Resolves MTGO usernames -> Discord
    ids (via MtgoAccount), finds the unreported MatchResult for that pair, orients the game
    score onto player1/player2, and calls record_match_result.

Safety: read-only observation feeds this; nothing moves assets. The worst failure is a
wrong result, which an admin can re-report or override. Re-reporting a completed match is a
no-op (no unreported row is found).
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.match import MatchResult
from models.draft_session import DraftSession
from models.mtgo_account import MtgoAccount


async def record_match_result(bot, session_id, match_number, player1_wins, player2_wins, winner_indicator):
    """
    Record one pairing's result and fire the same downstream as manual entry.

    winner_indicator: '1' (player1 won), '2' (player2 won), '0' (draw / no match played).
    Returns (ok: bool, detail: str). Safe to re-run — the victory cascade is guarded and the
    stat updates are correction-safe.
    """
    # Imported here (not at module load) to avoid an import cycle with utils.py.
    from utils import (
        update_draft_summary_message,
        check_and_post_victory_or_draw,
        update_player_stats_and_elo,
        store_match_streak_extensions,
    )
    from helpers.skill import rating_counts_for

    winner_id = None
    async with db_session() as session:
        row = (await session.execute(
            select(MatchResult, DraftSession).join(DraftSession).where(
                MatchResult.session_id == session_id,
                MatchResult.match_number == match_number,
            )
        )).first()
        if not row:
            return (False, "match result or draft session not found")
        match_result, draft_session = row
        match_result.player1_wins = int(player1_wins)
        match_result.player2_wins = int(player2_wins)
        if str(winner_indicator) != '0':
            winner_id = match_result.player1_id if str(winner_indicator) == '1' else match_result.player2_id
        match_result.winner_id = winner_id
        match_result.result_submitted_at = datetime.now()
        # Snapshot the fields we need after the session commits/closes.
        result_pk = match_result.id
        p1_id, p2_id = match_result.player1_id, match_result.player2_id
        session_type = draft_session.session_type
        guild_id = str(draft_session.guild_id)
    # committed on context exit

    if rating_counts_for(session_type):
        mr = await MatchResult.get_by_id(result_pk)
        streak_extensions = await update_player_stats_and_elo(mr)
        store_match_streak_extensions(session_id, p1_id, p2_id, streak_extensions)
        if winner_id:
            loser_id = p2_id if winner_id == p1_id else p1_id
            from services.ring_bearer_service import check_match_defeat_transfer
            await check_match_defeat_transfer(
                bot=bot, guild_id=guild_id, winner_id=winner_id, loser_id=loser_id, session_id=session_id)

    await update_draft_summary_message(bot, session_id)
    from livedrafts import update_live_draft_summary
    await update_live_draft_summary(bot, session_id)
    if session_type != "test":
        # The chokepoint: victory/draw detection + tournament record_linked_result +
        # premade Match write + streaks + stakes/debt + leaderboards + messages. Idempotent.
        await check_and_post_victory_or_draw(bot, session_id)

    return (True, f"recorded {player1_wins}-{player2_wins} (session {session_id}, match {match_number})")


async def report_mtgo_match(bot, *, player_a, player_b, winner, games_winner, games_loser,
                            session_id=None, mtgo_match_id=None):
    """
    MTGO worker entry point. player_a / player_b / winner are MTGO usernames; winner must be
    one of the two. games_winner / games_loser are the match score (e.g. 2 and 0).

    Returns (status, detail) with status in:
      ok             recorded
      unlinked       one or both MTGO usernames have no /link_mtgo mapping
      bad_winner     winner is not one of the two players
      no_match       no unreported pairing for these two players (also the re-report case)
    """
    da = await MtgoAccount.discord_for_mtgo(player_a)
    dbid = await MtgoAccount.discord_for_mtgo(player_b)
    unlinked = [u for u, d in ((player_a, da), (player_b, dbid)) if not d]
    if unlinked:
        return ("unlinked", f"no MTGO account linked for: {', '.join(unlinked)} (use /link_mtgo)")

    dw = await MtgoAccount.discord_for_mtgo(winner)
    if dw not in (da, dbid):
        return ("bad_winner", f"winner '{winner}' is not one of the two players")

    row = await MatchResult.find_unreported_for_pair(da, dbid, session_id=session_id)
    if row is None:
        return ("no_match", "no unreported match found for these two players (already reported?)")

    # Orient the reported score onto player1/player2 + set the winner indicator.
    if row.player1_id == dw:
        p1_wins, p2_wins, indicator = games_winner, games_loser, '1'
    else:
        p1_wins, p2_wins, indicator = games_loser, games_winner, '2'

    ok, detail = await record_match_result(bot, row.session_id, row.match_number, p1_wins, p2_wins, indicator)
    if not ok:
        return ("no_match", detail)
    logger.info(
        f"[mtgo] recorded result via MTGO report: {player_a} vs {player_b}, winner={winner} "
        f"({games_winner}-{games_loser}), mtgo_match_id={mtgo_match_id}")
    return ("ok", detail)


async def pending_pairings(session_id=None, limit=200):
    """
    Pending (unreported) pairings where BOTH players have a linked MTGO account.

    The worker calls this to learn which MTGO player-pairs to watch for in the freeform
    room. Only pairings with both usernames resolvable are returned — the worker can't
    match an unlinked player, so there's no point sending it. Scoped to session_id when
    given. Returns a list of dicts:
      {sessionId, matchNumber, playerA, playerB, discordA, discordB, sessionType}
    """
    async with db_session() as session:
        conds = [MatchResult.winner_id == None]
        if session_id is not None:
            conds.append(MatchResult.session_id == session_id)
        stmt = (select(MatchResult, DraftSession).join(DraftSession)
                .where(*conds)
                .order_by(MatchResult.session_id, MatchResult.match_number)
                .limit(limit))
        rows = (await session.execute(stmt)).all()

    discord_ids = set()
    for mr, _ds in rows:
        discord_ids.add(mr.player1_id)
        discord_ids.add(mr.player2_id)
    names = await MtgoAccount.usernames_for_discord_ids(discord_ids)

    out = []
    for mr, ds in rows:
        a = names.get(str(mr.player1_id))
        b = names.get(str(mr.player2_id))
        if not a or not b:
            continue  # both players must be linked for the worker to match them
        out.append({
            "sessionId": mr.session_id,
            "matchNumber": mr.match_number,
            "playerA": a,
            "playerB": b,
            "discordA": mr.player1_id,
            "discordB": mr.player2_id,
            "sessionType": ds.session_type,
        })
    return out
