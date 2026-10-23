"""The scheduler's match example files: shape and internal consistency."""
import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "scheduler_page" / "fixtures"
SHARED = sorted(FIXTURES.glob("match-shared.*.json"))
ME = sorted(FIXTURES.glob("match-me.*.json"))
SCENARIOS = {"good", "poor", "nolib", "nofollow", "later"}


def load(path):
    return json.loads(path.read_text())


def test_every_scenario_has_both_files():
    assert {p.name.split(".")[1] for p in SHARED} == SCENARIOS
    assert {p.name.split(".")[1] for p in ME} == SCENARIOS


@pytest.mark.parametrize("path", SHARED, ids=lambda p: p.name)
def test_shared_shape(path):
    f = load(path)
    assert f["format"] == "draftbot.scheduler.match" and f["version"] == 1
    assert set(f["rules"]) == {"team_size", "min_own_players", "max_subs", "draft_cells",
                               "copy_offset_cells", "copy_cells", "sitting_cells"}
    assert set(f["match"]) == {"id", "tournament_id", "tournament_name", "round", "status", "thread_url"}
    team_of = {}
    for team in f["teams"]:
        for p in team["players"]:
            assert "plays_after_draft" in p and "needs_library" in p
            team_of[p["user"]] = team["id"]
    assert len(f["pairings"]) == 3
    for pr in f["pairings"]:
        a, b = pr["players"]
        assert team_of[a] != team_of[b]
    lib = f["library"]
    assert len(lib["copies_free"]) == f["window"]["cells"] == 336
    assert all(isinstance(n, int) and 0 <= n <= lib["copies_total"] for n in lib["copies_free"])
    assert all("user" in h for h in lib["holds"])


@pytest.mark.parametrize("path", SHARED, ids=lambda p: p.name)
def test_copy_need_matches_scenario(path):
    f = load(path)
    players = [p for t in f["teams"] for p in t["players"]]
    needs_copy = any(p["plays_after_draft"] and p["needs_library"] for p in players)
    scen = path.name.split(".")[1]
    assert needs_copy == (scen in {"good", "poor"})
    if scen == "nofollow":
        assert all(p["plays_after_draft"] in (False, None) for p in players)
    if f["booking"]["draft"]:
        assert f["booking"]["draft"]["library_copy"] == needs_copy


@pytest.mark.parametrize("path", ME, ids=lambda p: p.name)
def test_me_user_in_shared(path):
    me = load(path)
    shared = load(path.with_name(path.name.replace("match-me", "match-shared")))
    assert me["format"] == "draftbot.scheduler.match_me" and me["version"] == 1
    players = {p["user"]: p for t in shared["teams"] for p in t["players"]}
    assert me["user"] in players
    p = players[me["user"]]
    assert (me["plays_after_draft"], me["needs_library"]) == (p["plays_after_draft"], p["needs_library"])


def test_later_scenario():
    shared = load(FIXTURES / "match-shared.later.json")
    me = load(FIXTURES / "match-me.later.json")
    assert shared["match"]["status"] == "booked" and shared["booking"]["draft"]
    assert me["needs_library"] is True and me["plays_after_draft"] is False
    assert me["deck"]["assigned"] is True and me["deck"]["short_reason"]
    assert me["requests"][0]["kind"] == "book_sitting" and me["requests"][0]["status"] == "booked"
    sit = shared["booking"]["sittings"][0]
    assert sit["decks_reserved"] == [me["user"]]
    bits = base64.b64decode(me["deck"]["free_starts"])
    free = [bool(bits[i // 8] & (0x80 >> i % 8)) for i in range(336)]
    assert not all(free) and any(free)


@pytest.mark.parametrize("path", SHARED, ids=lambda p: p.name)
def test_generated_at_dates_the_scenario(path):
    f = load(path)
    me = load(path.with_name(path.name.replace("match-shared", "match-me")))
    assert me["generated_at"] == f["generated_at"]
    # Thursday 19:00 New York (EDT, UTC-4) of the window: something is still bookable in principle.
    assert f["generated_at"] < "2026-10-22T23:00:00Z"
    if f["booking"]["draft"]:
        assert f["generated_at"] > f["booking"]["draft"]["start_utc"]
    else:
        assert f["generated_at"] < f["window"]["start_utc"]
