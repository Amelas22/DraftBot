"""The public league page's tournament payload: standings, pairings, rosters.

Read-only, and deliberately built from the same functions Discord's standings
use -- `get_standings_data` for the order and `omw_percentages` for the
tiebreak it is ordered by. Re-deriving either here would let the public page
and the Discord embed disagree about who is winning, which is the one failure
this module has to make impossible.

The payload is embedded into league_site/index.html by `inject` at publish
time (see scripts/upload_league_page.py), so the page needs no second request
and stays a single self-contained file.
"""
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from draft_organization.swiss import omw_percentages
from models.player import PlayerStats
from models.sign_up_history import SignUpHistory
from models.tournament import (
    Tournament,
    TournamentMatch,
    TournamentRound,
)
from services.tournament_service import (
    get_active_tournament,
    get_rosters,
    get_standings_data,
)

# The block `inject` writes into. PLACEHOLDER_JSON is what index.html ships with
# in git, so a checkout that has never been published renders its empty states
# rather than a JSON syntax error -- and so the file in git never carries a
# snapshot of anyone's roster.
PLACEHOLDER_ID = "tournament-data"
PLACEHOLDER_JSON = '{"teams":[],"standings":[],"rounds":[]}'

# Shown for a captain with no PlayerStats row in this guild -- someone who
# registered a team but has never drafted here. Their snowflake would be worse
# than useless on a public page, so it is never rendered.
UNKNOWN_CAPTAIN = "Captain"


async def _captain_names(session: Any, guild_id: str,
                         user_ids: list[str]) -> dict[str, str]:
    """{user_id: display name} for the captains we can name.

    Captains are the one roster slot with no name of their own:
    TournamentTeamMember deliberately excludes them (captain_user_id stays the
    single authority for who owns a team), and the Discord board never needs
    one because it renders `<@id>` and lets Discord resolve it. A web page has
    no such renderer, so the name is looked up here instead -- from the DB, so
    the whole page stays buildable from a database copy with no Discord call.

    PlayerStats alone is not enough: it is keyed by guild, and on the live
    league 15 of 40 captains had never drafted in that guild, so they would all
    have rendered as "Captain". Signup history covers all but one of those.
    Sources are consulted best-first -- this guild before any other, a ranked
    name before a signup snapshot -- and the first one to answer wins.
    """
    if not user_ids:
        return {}

    guild_id = str(guild_id)
    stats_here: dict[str, str] = {}
    stats_elsewhere: dict[str, str] = {}
    for player_id, player_guild, name in (await session.execute(
        select(PlayerStats.player_id, PlayerStats.guild_id, PlayerStats.display_name)
        .where(PlayerStats.player_id.in_(user_ids))
    )).all():
        if not name:
            continue
        target = stats_here if str(player_guild) == guild_id else stats_elsewhere
        target.setdefault(player_id, name)

    # Ascending, so a later signup overwrites the name it replaced: these are
    # snapshots taken at join time, and the most recent is the current one.
    signups_here: dict[str, str] = {}
    signups_elsewhere: dict[str, str] = {}
    for user_id, signup_guild, name in (await session.execute(
        select(SignUpHistory.user_id, SignUpHistory.guild_id,
               SignUpHistory.user_display_name)
        .where(SignUpHistory.user_id.in_(user_ids))
        .order_by(SignUpHistory.timestamp)
    )).all():
        if not name:
            continue
        target = signups_here if str(signup_guild) == guild_id else signups_elsewhere
        target[user_id] = name

    resolved: dict[str, str] = {}
    for source in (stats_here, signups_here, stats_elsewhere, signups_elsewhere):
        for user_id, name in source.items():
            resolved.setdefault(user_id, name)
    return resolved


async def _rounds(session: Any, tournament_id: int) -> list[dict[str, Any]]:
    """Every round's pairings, oldest first, with scores where reported."""
    rounds = (await session.execute(
        select(TournamentRound)
        .where(TournamentRound.tournament_id == tournament_id)
        .order_by(TournamentRound.round_number)
    )).scalars().all()
    if not rounds:
        return []

    by_round: dict[int, list[TournamentMatch]] = {r.id: [] for r in rounds}
    matches = (await session.execute(
        select(TournamentMatch)
        .where(TournamentMatch.round_id.in_(list(by_round)))
        .order_by(TournamentMatch.id)
    )).scalars().all()
    for match in matches:
        by_round[match.round_id].append(match)

    return [
        {
            "number": round_.round_number,
            "stage": round_.stage,
            "matches": [
                {
                    "a": match.team_a_participant_id,
                    "b": match.team_b_participant_id,
                    "a_wins": match.team_a_wins,
                    "b_wins": match.team_b_wins,
                    "bye": bool(match.is_bye),
                }
                for match in by_round[round_.id]
            ],
        }
        for round_ in rounds
    ]


async def build_tournament_data(session: Any, tournament_id: int) -> dict[str, Any]:
    """The whole public payload for one tournament.

    Teams are keyed by participant id and referenced by id from both the
    standings and the pairings, so the page resolves a name or a roster the
    same way wherever a team appears.
    """
    tournament = await session.get(Tournament, tournament_id)
    if tournament is None:
        raise ValueError(f"No tournament with id {tournament_id}")

    standings = await get_standings_data(session, tournament_id)
    rosters = await get_rosters(session, tournament_id)
    captains = await _captain_names(
        session, tournament.guild_id, [p.captain_user_id for p in standings])

    # The same swiss-only match graph get_standings_data ranked by, so the
    # displayed tiebreak is the one that produced the displayed order.
    swiss = (await session.execute(
        select(TournamentMatch)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentRound.tournament_id == tournament_id)
        .where(TournamentRound.stage != "playoff")
    )).scalars().all()
    omw = omw_percentages(standings, swiss)

    return {
        "name": tournament.name,
        "status": tournament.status,
        "format": tournament.format,
        "current_round": tournament.current_round,
        "total_rounds": tournament.total_rounds,
        "cut_to": tournament.cut_to,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "teams": [
            {
                "id": p.id,
                "name": p.team_name,
                "captain": captains.get(p.captain_user_id, UNKNOWN_CAPTAIN),
                "members": [m.display_name for m in rosters.get(p.id, [])],
                "seed": p.seed,
                # On the team, not the standings row: the pairings resolve a
                # team through this same id, so a marker kept on the standings
                # row would leave a dropped team unmarked in the pairings.
                "dropped": p.dropped_at is not None,
            }
            for p in standings
        ],
        "standings": [
            {
                "rank": rank,
                "team_id": p.id,
                "points": p.points,
                "record": p.record,
                "omw": round(omw[p.id], 4),
                "game_wins": p.game_wins,
                "game_losses": p.game_losses,
                "byes": p.byes,
            }
            for rank, p in enumerate(standings, start=1)
        ],
        "rounds": await _rounds(session, tournament_id),
    }


def inject(html: str, data: dict[str, Any], placeholder_id: str = PLACEHOLDER_ID) -> str:
    """Replace the page's JSON payload block with `data`.

    `</` is escaped because the payload carries user-supplied text (team names,
    display names): a team called `</script>` would otherwise close the block
    early and put whatever followed it on the page as live markup. Escaping it
    inside a JSON string is invisible to JSON.parse.
    """
    opening = f'<script id="{placeholder_id}" type="application/json">'
    start = html.find(opening)
    if start == -1:
        raise ValueError(f'No <script id="{placeholder_id}"> block in the page')
    body = start + len(opening)
    end = html.find("</script>", body)
    if end == -1:
        raise ValueError(f'Unclosed <script id="{placeholder_id}"> block in the page')

    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return html[:body] + payload + html[end:]


def empty_payload() -> dict[str, Any]:
    """A fresh payload with nothing in it.

    A function rather than a shared constant: the lists are the page's own
    collections and a caller is free to build on them, so handing out the same
    object twice would let one publish leak into the next.
    """
    return {"teams": [], "standings": [], "rounds": []}


async def build_for_guild(session: Any, guild_id: str) -> dict[str, Any]:
    """The guild's active tournament payload, or an empty one if it has none.

    Publishing must not depend on a tournament being live: between events the
    site is still the rules page, and it should go up with its tournament tabs
    showing their empty state rather than fail.
    """
    tournament = await get_active_tournament(session, guild_id)
    if tournament is None:
        return empty_payload()
    return await build_tournament_data(session, tournament.id)


def summarize(data: dict[str, Any]) -> str:
    """One line describing what a payload contains, for the publish log.

    A bye counts as decided -- nobody has to play it, and leaving it in the
    denominator would make a completed round read as permanently unfinished.
    """
    name = data.get("name")
    if not name:
        return "no active tournament (empty tournament tabs)"
    matches = [m for r in data["rounds"] for m in r["matches"]]
    decided = sum(1 for m in matches if m["a_wins"] is not None or m["bye"])
    return (f"{name}: {len(data['teams'])} teams, "
            f"round {data['current_round']}/{data['total_rounds']}, "
            f"{decided}/{len(matches)} matches decided")
