"""Tests for services/league_site_data.py -- the public league page's payload."""
import json
import random
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from models.player import PlayerStats
from models.sign_up_history import SignUpHistory
from services.league_site_data import (
    PLACEHOLDER_JSON,
    build_for_guild,
    build_tournament_data,
    inject,
    summarize,
)
from services.tournament_service import (
    add_teammate,
    create_tournament,
    drop_team,
    find_participant_by_name,
    register_team,
    set_result,
    start_tournament,
)

GUILD = "g1"


async def seed_league(session, teams=("Alpha", "Bravo"), total_rounds=3, cut_to=None):
    """A registered tournament with `teams`, captain N for the Nth team."""
    tournament = await create_tournament(
        session, GUILD, "Lotus League", total_rounds, cut_to=cut_to)
    await session.commit()
    for i, name in enumerate(teams, start=1):
        await register_team(session, tournament.id, name, str(i))
    await session.commit()
    return tournament


async def name_player(session, user_id, display_name):
    session.add(PlayerStats(player_id=str(user_id), guild_id=GUILD,
                            display_name=display_name))
    await session.commit()


# ---- shape and metadata -----------------------------------------------------

@pytest.mark.asyncio
async def test_payload_carries_tournament_metadata(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session, total_rounds=6, cut_to=8)
        data = await build_tournament_data(session, tournament.id)

    assert data["name"] == "Lotus League"
    assert data["total_rounds"] == 6
    assert data["cut_to"] == 8
    assert data["status"] == "registration"


# ---- teams and rosters ------------------------------------------------------

@pytest.mark.asyncio
async def test_team_lists_captain_and_roster_by_display_name(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await name_player(session, "1", "aber")
        alpha = await find_participant_by_name(session, tournament.id, "Alpha")
        await add_teammate(session, alpha, "77", "sandydogmtg")
        await add_teammate(session, alpha, "88", "daxerz")
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    team = next(t for t in data["teams"] if t["name"] == "Alpha")
    assert team["captain"] == "aber"
    assert team["members"] == ["sandydogmtg", "daxerz"]


@pytest.mark.asyncio
async def test_team_with_empty_roster_still_names_its_captain(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await name_player(session, "2", "quinn")
        data = await build_tournament_data(session, tournament.id)

    bravo = next(t for t in data["teams"] if t["name"] == "Bravo")
    assert bravo["captain"] == "quinn"
    assert bravo["members"] == []


@pytest.mark.asyncio
async def test_captain_with_no_recorded_name_falls_back(match_control_db):
    # A captain who has never drafted in this guild has no PlayerStats row. The
    # page must still render a team rather than a blank or a raw snowflake.
    async with match_control_db() as session:
        tournament = await seed_league(session)
        data = await build_tournament_data(session, tournament.id)

    assert all(t["captain"] for t in data["teams"])
    assert not any(t["captain"] == "1" for t in data["teams"])


@pytest.mark.asyncio
async def test_a_dropped_team_is_flagged_so_the_page_can_mark_it(match_control_db):
    """A dropped team keeps its place and its record -- both still count towards
    the tiebreaks of everyone it played -- so the payload has to say it has gone.
    Unmarked, it reads as a team the pairings have quietly stopped including.

    The flag rides on the team rather than the standings row because the pairings
    resolve a team through the same id, and a marker the pairings cannot see
    would leave a dropped team unmarked in half the places it appears.
    """
    async with match_control_db() as session:
        tournament = await seed_league(
            session, teams=("Alpha", "Bravo", "Charlie", "Delta"))
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await drop_team(session, tournament.id, "Bravo")
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    teams = {t["name"]: t for t in data["teams"]}
    assert teams["Bravo"]["dropped"] is True
    assert teams["Alpha"]["dropped"] is False


# ---- standings --------------------------------------------------------------

@pytest.mark.asyncio
async def test_standings_follow_the_ranked_order(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await set_result(session, matches[0].id, 2, 1)
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    winner_id = matches[0].team_a_participant_id
    assert [s["rank"] for s in data["standings"]] == [1, 2]
    assert data["standings"][0]["team_id"] == winner_id
    assert data["standings"][0]["points"] == 3
    # Draws are not allowed in this tournament, so the record is W-L.
    assert data["standings"][0]["record"] == "1-0"


@pytest.mark.asyncio
async def test_standings_carry_the_omw_tiebreak(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        data = await build_tournament_data(session, tournament.id)

    # No matches played: every team sits at the MTR floor. Rounded for the
    # payload -- the page shows one decimal place of a percentage.
    assert data["standings"][0]["omw"] == round(1 / 3, 4)


# ---- pairings ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_reports_its_matches_with_scores(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await set_result(session, matches[0].id, 2, 1)
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    match = data["rounds"][0]["matches"][0]
    assert data["rounds"][0]["number"] == 1
    assert (match["a_wins"], match["b_wins"]) == (2, 1)
    assert match["bye"] is False


@pytest.mark.asyncio
async def test_unreported_match_has_no_score(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    match = data["rounds"][0]["matches"][0]
    assert match["a_wins"] is None and match["b_wins"] is None


@pytest.mark.asyncio
async def test_bye_is_marked_and_has_no_opponent(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session, teams=("Alpha", "Bravo", "Charlie"))
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    byes = [m for m in data["rounds"][0]["matches"] if m["bye"]]
    assert len(byes) == 1
    assert byes[0]["b"] is None


@pytest.mark.asyncio
async def test_every_match_id_resolves_to_a_listed_team(match_control_db):
    # The page renders names and rosters by looking ids up in `teams`; a pairing
    # pointing at a team that isn't there would render as a blank row.
    async with match_control_db() as session:
        tournament = await seed_league(session, teams=("Alpha", "Bravo", "Charlie"))
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    known = {t["id"] for t in data["teams"]}
    for round_ in data["rounds"]:
        for match in round_["matches"]:
            assert match["a"] in known
            assert match["b"] is None or match["b"] in known
    assert {s["team_id"] for s in data["standings"]} <= known


# ---- injection --------------------------------------------------------------

def test_inject_replaces_the_placeholder_payload():
    html = ('<script id="tournament-data" type="application/json">'
            f'{PLACEHOLDER_JSON}</script>')

    out = inject(html, {"name": "Lotus League", "teams": []})

    assert json.loads(out.split(">", 1)[1].rsplit("<", 1)[0])["name"] == "Lotus League"


def test_inject_escapes_a_closing_script_tag():
    # A team named "</script>" would otherwise break out of the JSON block and
    # let arbitrary markup onto the page.
    out = inject('<script id="tournament-data" type="application/json">{}</script>',
                 {"teams": [{"name": "</script><img src=x onerror=alert(1)>"}]})

    assert "</script><img" not in out
    assert out.count("</script>") == 1


def test_inject_refuses_html_without_the_placeholder():
    with pytest.raises(ValueError, match="tournament-data"):
        inject("<html><body>no placeholder</body></html>", {"teams": []})


# ---- build_for_guild --------------------------------------------------------

@pytest.mark.asyncio
async def test_build_for_guild_finds_the_active_tournament(match_control_db):
    async with match_control_db() as session:
        await seed_league(session)
        data = await build_for_guild(session, GUILD)

    assert data["name"] == "Lotus League"


@pytest.mark.asyncio
async def test_build_for_guild_returns_an_empty_payload_between_events(match_control_db):
    # Publishing is how the rules get updated too, so a guild with no live
    # tournament must still produce a page rather than raise.
    async with match_control_db() as session:
        data = await build_for_guild(session, "guild-with-nothing")

    assert data == {"teams": [], "standings": [], "rounds": []}


@pytest.mark.asyncio
async def test_empty_payload_is_not_shared_between_calls(match_control_db):
    # A caller mutating one empty payload must not poison the next one.
    async with match_control_db() as session:
        first = await build_for_guild(session, "guild-with-nothing")
        first["teams"].append({"name": "mutated"})
        second = await build_for_guild(session, "guild-with-nothing")

    assert second["teams"] == []


# ---- summarize --------------------------------------------------------------

def test_summarize_counts_decided_matches():
    data = {"name": "Lotus League", "current_round": 2, "total_rounds": 6,
            "teams": [{"id": 1}, {"id": 2}, {"id": 3}],
            "rounds": [{"matches": [{"a_wins": 2, "bye": False},
                                    {"a_wins": None, "bye": True}]},
                       {"matches": [{"a_wins": None, "bye": False}]}]}

    line = summarize(data)

    assert "3 teams" in line
    assert "round 2/6" in line
    assert "2/3 matches decided" in line


def test_summarize_says_so_when_there_is_no_tournament():
    assert "no active tournament" in summarize(
        {"teams": [], "standings": [], "rounds": []})


# ---- the shipped page -------------------------------------------------------

SITE = Path(__file__).resolve().parent.parent / "league_site" / "index.html"


@pytest.mark.skipif(not SITE.exists(),
                    reason="league_site/ is git-excluded; only present locally")
def test_index_html_carries_an_injectable_placeholder():
    # inject() raises if the block is missing, and publishing is a manual step
    # run against production data -- so the failure has to surface here, not at
    # the moment someone is trying to put a round's pairings up.
    out = inject(SITE.read_text(), {"name": "Lotus League", "teams": []})

    assert '"name":"Lotus League"' in out
    assert PLACEHOLDER_JSON in SITE.read_text()


# ---- resolving captain names ------------------------------------------------

async def add_signup(session, user_id, display_name, guild=GUILD, when=None):
    session.add(SignUpHistory(
        id=str(uuid.uuid4()), session_id="s1", user_id=str(user_id),
        user_display_name=display_name, action="join", guild_id=guild,
        timestamp=when or datetime(2026, 1, 1)))
    await session.commit()


async def captain_of(session, tournament_id, team_name):
    data = await build_tournament_data(session, tournament_id)
    return next(t for t in data["teams"] if t["name"] == team_name)["captain"]


@pytest.mark.asyncio
async def test_captain_named_from_a_draft_signup_when_never_ranked(match_control_db):
    # Most captains have a PlayerStats row, but a third of them have never
    # drafted in the league's own guild -- their name lives in their signups.
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await add_signup(session, "1", "aber")

        assert await captain_of(session, tournament.id, "Alpha") == "aber"


@pytest.mark.asyncio
async def test_player_stats_beats_a_signup_name(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await name_player(session, "1", "current name")
        await add_signup(session, "1", "old signup name")

        assert await captain_of(session, tournament.id, "Alpha") == "current name"


@pytest.mark.asyncio
async def test_latest_signup_name_wins(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await add_signup(session, "1", "old", when=datetime(2025, 1, 1))
        await add_signup(session, "1", "renamed", when=datetime(2026, 6, 1))

        assert await captain_of(session, tournament.id, "Alpha") == "renamed"


@pytest.mark.asyncio
async def test_this_guilds_name_beats_another_guilds(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await add_signup(session, "1", "elsewhere", guild="other-guild",
                         when=datetime(2026, 6, 1))
        await add_signup(session, "1", "here", when=datetime(2025, 1, 1))

        assert await captain_of(session, tournament.id, "Alpha") == "here"


@pytest.mark.asyncio
async def test_another_guilds_name_is_better_than_none(match_control_db):
    async with match_control_db() as session:
        tournament = await seed_league(session)
        await add_signup(session, "1", "elsewhere", guild="other-guild")

        assert await captain_of(session, tournament.id, "Alpha") == "elsewhere"


@pytest.mark.asyncio
async def test_record_shows_a_draw_if_one_is_ever_recorded(match_control_db):
    # The column is dropped because draws are not allowed, not because the
    # schema forbids them -- a record that had one must not silently lose it.
    async with match_control_db() as session:
        tournament = await seed_league(session)
        alpha = await find_participant_by_name(session, tournament.id, "Alpha")
        alpha.match_wins, alpha.match_draws = 1, 1
        await session.commit()

        data = await build_tournament_data(session, tournament.id)

    row = next(s for s in data["standings"] if s["team_id"] == alpha.id)
    assert row["record"] == "1-0-1"
