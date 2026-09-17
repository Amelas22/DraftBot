"""Rehearse dropping teams from the live Lotus League tournament, on a COPY.

Local only. Reads and writes ./drafts.db in this worktree, which is a disposable
copy of production -- nothing here touches the real database or Discord.

What it proves, against the real 40-team field rather than a fixture:

  * a drop before the next round is refused while round 2 still has results
    outstanding -- the drop does not close anybody's match;
  * once those are recorded, the next round pairs the teams that are left,
    and the field going odd produces exactly one bye;
  * the dropped teams appear nowhere in the round-3 pairings, and their
    records stay in the standings, marked.

Usage:  python scripts/rehearse_lotus_drop.py [team ...]
"""
import asyncio
import sys

TOURNAMENT_ID = 3
DEFAULT_DROPS = ["ManaDorks", "The Ice Cubes", "Roto Chaff"]


async def main(drops):
    import random

    from database.db_session import db_session
    from models.tournament import Tournament, TournamentMatch, TournamentParticipant
    from services.tournament_formatter import create_standings_embed
    from services.tournament_service import (
        _pairable, advance_round, drop_team, get_standings_data, set_result,
    )
    from sqlalchemy import select

    rng = random.Random(20260906)

    async with db_session() as session:
        tournament = await session.get(Tournament, TOURNAMENT_ID)
        everyone = (await session.execute(
            select(TournamentParticipant).where(
                TournamentParticipant.tournament_id == TOURNAMENT_ID))).scalars().all()
        print(f"'{tournament.name}' — {tournament.status}, round "
              f"{tournament.current_round} of {tournament.total_rounds}, "
              f"cut to top {tournament.cut_to}")
        print(f"{len(everyone)} teams registered, {len(_pairable(everyone))} pairable\n")

    # --- the drops ----------------------------------------------------------
    print("== dropping ==")
    for team in drops:
        async with db_session() as session:
            try:
                p = await drop_team(session, TOURNAMENT_ID, team)
                print(f"  ✅ {p.team_name} dropped")
            except ValueError as e:
                print(f"  ❌ {team}: {e}")

    async with db_session() as session:
        left = _pairable((await session.execute(
            select(TournamentParticipant).where(
                TournamentParticipant.tournament_id == TOURNAMENT_ID))).scalars().all())
        print(f"\n{len(left)} teams still in — "
              f"{'odd, so one bye' if len(left) % 2 else 'even, so no bye'}\n")

    # --- the round in progress ---------------------------------------------
    print("== advancing to round 3 ==")
    async with db_session() as session:
        try:
            await advance_round(session, TOURNAMENT_ID, rng)
            print("  ⚠️  advanced with matches outstanding — that would be a bug")
        except ValueError as e:
            print(f"  refused, correctly: {e}")

    async with db_session() as session:
        from models.tournament import TournamentRound
        round_2 = (await session.execute(
            select(TournamentRound).where(
                TournamentRound.tournament_id == TOURNAMENT_ID,
                TournamentRound.round_number == 2))).scalars().first()
        open_matches = [m for m in (await session.execute(
            select(TournamentMatch).where(
                TournamentMatch.round_id == round_2.id))).scalars().all()
            if not m.is_bye and m.team_a_wins is None]
        print(f"  {len(open_matches)} match(es) still need a result; "
              f"recording them the way an organizer would")
        for match in open_matches:
            await set_result(session, match.id, 2, 0)

    async with db_session() as session:
        new_round = await advance_round(session, TOURNAMENT_ID, rng)
        print(f"  round {new_round.round_number} paired\n")

    # --- what the room would see -------------------------------------------
    async with db_session() as session:
        from models.tournament import TournamentRound
        round_3 = (await session.execute(
            select(TournamentRound).where(
                TournamentRound.tournament_id == TOURNAMENT_ID,
                TournamentRound.round_number == 3))).scalars().first()
        matches = (await session.execute(
            select(TournamentMatch).where(
                TournamentMatch.round_id == round_3.id))).scalars().all()
        by_id = {p.id: p.team_name for p in (await session.execute(
            select(TournamentParticipant).where(
                TournamentParticipant.tournament_id == TOURNAMENT_ID))).scalars().all()}

        playable = [m for m in matches if not m.is_bye]
        byes = [m for m in matches if m.is_bye]
        print(f"== round 3: {len(playable)} matches, {len(byes)} bye ==")
        for m in playable[:6]:
            print(f"  • {by_id[m.team_a_participant_id]} vs {by_id[m.team_b_participant_id]}")
        print(f"  … {len(playable) - 6} more")
        for m in byes:
            print(f"  • {by_id[m.team_a_participant_id]} — BYE")

        paired = {by_id[m.team_a_participant_id] for m in matches}
        paired |= {by_id[m.team_b_participant_id] for m in matches if m.team_b_participant_id}
        intruders = [d for d in drops if d in paired]
        print(f"\n  dropped teams appearing in round 3: {intruders or 'none'}")

    # --- the standings ------------------------------------------------------
    async with db_session() as session:
        tournament = await session.get(Tournament, TOURNAMENT_ID)
        standings = await get_standings_data(session, TOURNAMENT_ID)
        embed = create_standings_embed(tournament, standings)
        body = "\n".join(f.value for f in embed.fields)
    print("\n== standings rows for the dropped teams ==")
    for line in body.splitlines():
        if any(d in line for d in drops):
            print(f"  {line}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_DROPS))
