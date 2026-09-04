"""Rendering one self-contained draft table page.

The page is hosted, not opened from disk, and card art loads from Scryfall by
URL -- but everything else (data, CSS, JS) must be inline, because there is no
second request to fetch it from.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import draft_table_page

# The draft log ships as a committed test fixture -- same one
# test_draft_reconstruct.py uses (a real 6-seat 3-pack PowerLSV draft), needed
# for the three tests moved here from Task 1 that check build_payload()
# against ground truth.
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tests" / "fixtures" / "draft_log_6seat.json"


def a_draft_session(**overrides):
    base = dict(
        friendly_id="ashas-favor-55", cube="PowerLSV", session_id="sid-1",
        draft_start_time=None, session_type="premade",
        sign_ups={"1": "Alice", "2": "Bob"},
        team_a=["1"], team_b=["2"],
        team_a_name="Herr Bros", team_b_name="gypsy caravan",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def minimal_draft():
    """The smallest log reconstruct() accepts.

    Reshaped from the brief's version: passing_ring() needs, for every seat,
    both a (pack 0, pick 0) row -- to see what it opened and passed on -- and a
    (pack 0, pick 1) row -- to match against what a neighbour passed -- before
    it can resolve who sits downstream of whom. A single pick per seat leaves
    that match set empty ("matched 0/2 seats"). So each seat gets two picks:
    Alice opens [c1, c2], takes c1, passes {c2} to Bob; Bob opens [c3, c4],
    takes c3, passes {c4} to Alice; then each receives the other's passed
    booster and takes what's left.
    """
    return {
        "sessionID": "DBTEST",
        "users": {
            "u1": {"userName": "Alice", "seatNum": 0,
                   "picks": [
                       {"packNum": 0, "pickNum": 0, "pick": [0],
                        "booster": ["c1", "c2"]},
                       {"packNum": 0, "pickNum": 1, "pick": [0],
                        "booster": ["c4"]},
                   ]},
            "u2": {"userName": "Bob", "seatNum": 1,
                   "picks": [
                       {"packNum": 0, "pickNum": 0, "pick": [0],
                        "booster": ["c3", "c4"]},
                       {"packNum": 0, "pickNum": 1, "pick": [0],
                        "booster": ["c2"]},
                   ]},
        },
        "carddata": {
            "c1": {"name": "Bear", "mana_cost": "{1}{G}", "cmc": 2,
                   "colors": ["G"], "type": "Creature", "rarity": "common",
                   "image_uris": {"en": "https://cards.scryfall.io/border_crop/x.jpg"}},
            "c2": {"name": "Bird", "mana_cost": "{U}", "cmc": 1, "colors": ["U"],
                   "type": "Creature", "rarity": "common", "image_uris": {}},
            "c3": {"name": "Bolt", "mana_cost": "{R}", "cmc": 1, "colors": ["R"],
                   "type": "Instant", "rarity": "common", "image_uris": {}},
            "c4": {"name": "Bog", "mana_cost": "", "cmc": 0, "colors": [],
                   "type": "Land", "rarity": "common", "image_uris": {}},
        },
    }


@pytest.fixture(scope="module")
def draft():
    if not LOG.exists():
        pytest.skip(f"draft log not cached at {LOG}")
    return json.loads(LOG.read_text())


def test_display_name_strips_discord_emoji_tokens():
    """Custom-emoji tokens render as raw text in a browser."""
    assert draft_table_page.display_name("<:lotus:123> Alice") == "Alice"


def test_display_name_falls_back_when_a_name_is_only_emoji():
    assert draft_table_page.display_name("<:lotus:123>") == "<:lotus:123>"


@pytest.fixture
def no_oracle(monkeypatch):
    """Render without reaching Scryfall. No test in this file may hit the network."""
    async def _stub(ids):
        return {}
    monkeypatch.setattr(draft_table_page.scryfall_oracle, "enrich", _stub)


def test_session_meta_maps_each_signup_to_a_side(minimal_draft):
    meta = draft_table_page.session_meta_from(a_draft_session(), minimal_draft)

    assert meta["teams"] == {"Alice": "A", "Bob": "B"}
    assert meta["teamNames"] == {"A": "Herr Bros", "B": "gypsy caravan"}
    assert meta["friendlyId"] == "ashas-favor-55"


def test_session_meta_names_unnamed_teams(minimal_draft):
    """An unnamed draft used to tell players its teams were called None."""
    meta = draft_table_page.session_meta_from(
        a_draft_session(team_a_name=None, team_b_name=None), minimal_draft)

    assert meta["teamNames"] == {"A": "Team Red", "B": "Team Blue"}


def test_a_seat_renamed_in_draftmancer_still_gets_its_side(minimal_draft):
    """The viewer looks a seat up by the name the LOG gives it, and an unmatched
    seat is not merely unlabelled -- it falls back to side A, i.e. renders on
    the wrong team. Matching by seat order rather than by stored name keeps that
    from happening when a player renames in the Draftmancer client."""
    renamed = json.loads(json.dumps(minimal_draft))
    renamed["users"]["u2"]["userName"] = "Bob (renamed mid-draft)"

    meta = draft_table_page.session_meta_from(a_draft_session(), renamed)

    assert meta["teams"] == {"Alice": "A", "Bob (renamed mid-draft)": "B"}


def test_side_map_falls_back_to_signup_names_when_seats_cannot_align(minimal_draft):
    """map_discord_to_draftmancer refuses a positional alignment when the player
    count doesn't match. Falling back to the stored names then matches no fewer
    seats than this map did before."""
    short = json.loads(json.dumps(minimal_draft))
    del short["users"]["u2"]

    meta = draft_table_page.session_meta_from(a_draft_session(), short)

    assert meta["teams"] == {"Alice": "A", "Bob": "B"}


@pytest.mark.asyncio
async def test_render_inlines_the_payload_css_and_js(no_oracle, minimal_draft):
    html = await draft_table_page.render(
        minimal_draft,
        draft_table_page.session_meta_from(a_draft_session(), minimal_draft))

    assert html.startswith("<!DOCTYPE html>")
    assert "window.DRAFT = " in html
    assert "<style>" in html
    assert "ashas-favor-55" in html


@pytest.mark.asyncio
async def test_render_escapes_every_angle_bracket(no_oracle, minimal_draft):
    """"</script>" would end the data block early; "<!--<script" would put it
    into script-data-double-escaped state, after which "</script>" stops ending
    it. Escaping every "<" defeats both."""
    hostile = a_draft_session(friendly_id="</script><!--<script")
    html = await draft_table_page.render(
        minimal_draft, draft_table_page.session_meta_from(hostile, minimal_draft))

    data_block = html.split("window.DRAFT = ")[1].split(";</script>")[0]
    assert "<" not in data_block
    assert "\\u003c/script>" in data_block


# The three tests below moved here from Task 1's test_draft_reconstruct.py --
# that module doesn't create build_payload(), this one does. Their assertions
# are unchanged; only the import and the network seam are new. Originally
# these called build_payload() with no oracle stub, reaching api.scryfall.com
# for any uncached card -- passing only because a local disk cache happened to
# be warm. That cache is gone, and the new enrich() has no cache at all, so
# every one of these tests would otherwise hit the real network. They're
# stubbed the same way the render tests above are.

@pytest.mark.asyncio
async def test_payload_steps_carry_the_pack_number(monkeypatch, draft):
    """The viewer keys every step by (pack, pick, seat).

    Omitting `pack` still produces a well-formed page whose lookups all miss,
    so the failure is silent -- every seat renders as "no pick recorded".
    """
    async def no_oracle(ids):
        return {}
    monkeypatch.setattr(draft_table_page.scryfall_oracle, "enrich", no_oracle)

    payload = await draft_table_page.build_payload(
        draft, {"friendlyId": "x", "cube": "", "started": "",
                "type": "", "teamNames": {"A": "A", "B": "B"}, "teams": {}})
    keys = {(s["pack"], s["pick"], s["seat"])
            for b in payload["boosters"] for s in b["steps"]}
    assert len(keys) == len(payload["boosters"]) * 15
    for seat in payload["ring"]:
        assert (2, 7, seat) in keys, f"{seat} has no step at pack 2 pick 7"


@pytest.mark.asyncio
async def test_payload_carries_each_seat_pool_in_draft_order(monkeypatch, draft):
    """The viewer slices these lists by position, so order is the contract."""
    async def no_oracle(ids):
        return {}
    monkeypatch.setattr(draft_table_page.scryfall_oracle, "enrich", no_oracle)

    payload = await draft_table_page.build_payload(
        draft, {"friendlyId": "x", "cube": "", "started": "",
                "type": "", "teamNames": {"A": "A", "B": "B"}, "teams": {}})
    for seat in payload["ring"]:
        entries = payload["pools"][seat]
        order = [(e["pack"], e["pick"]) for e in entries]
        assert order == sorted(order), f"{seat}'s pool is not in draft order"
        assert all("card" in e and "gone" in e for e in entries)


@pytest.mark.asyncio
async def test_payload_carries_taken_in_per_booster(monkeypatch, draft):
    async def no_oracle(ids):
        return {}
    monkeypatch.setattr(draft_table_page.scryfall_oracle, "enrich", no_oracle)

    payload = await draft_table_page.build_payload(
        draft, {"friendlyId": "x", "cube": "", "started": "",
                "type": "", "teamNames": {"A": "A", "B": "B"}, "teams": {}})
    for booster in payload["boosters"]:
        assert booster["takenAt"], f"booster {booster['index']} has no takenAt map"
        for entry in booster["takenAt"].values():
            assert 1 <= entry["pick"] <= 15
            assert entry["seat"] in payload["ring"]
