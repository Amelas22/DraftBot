"""determine_draft_outcome decorates upset victories and never blocks on failure."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import utils


class _Member:
    def __init__(self, member_id):
        self.display_name = f"P{member_id}"
        self.roles = []


class _Guild:
    id = 123

    def get_member(self, member_id):
        return _Member(member_id)


class _Bot:
    def get_guild(self, guild_id):
        return _Guild()


def _session(session_type="random"):
    return SimpleNamespace(
        session_id="s1",
        guild_id="123",
        session_type=session_type,
        team_a=["1", "2", "3"],
        team_b=["4", "5", "6"],
        team_a_name="Alpha",
        team_b_name="Beta",
        teams_start_time=datetime(2026, 7, 31),
        draft_start_time=datetime(2026, 7, 31),
        draft_id="d1",
    )


@pytest.fixture(autouse=True)
def _simple_display_names(monkeypatch):
    monkeypatch.setattr(utils, "get_display_name", lambda member, guild: member.display_name)


def _patch_stats(monkeypatch, stats_map):
    async def fake_fetch(guild_id, player_ids):
        return stats_map
    monkeypatch.setattr(utils, "_fetch_player_stats_map", fake_fetch)


STRONG = (30.0, 1.0, 30)  # display 1738; three of these vs three priors -> ~80/20


@pytest.mark.asyncio
async def test_upset_win_decorates_title_and_description(monkeypatch):
    # Team A (priors, display 1500) beat team B (display 1738s): winner prob ~0.20 -> legendary
    _patch_stats(monkeypatch, {"4": STRONG, "5": STRONG, "6": STRONG})
    title, description, _ = await utils.determine_draft_outcome(
        _Bot(), _session(), team_a_wins=5, team_b_wins=2, half_matches=3, total_matches=7
    )
    assert title.startswith("🌟 LEGENDARY UPSET — Congratulations to P1, P2, P3")
    assert "~4:1 odds" in description
    assert "P4" not in title and "P4" not in description  # loser names stay out


@pytest.mark.asyncio
async def test_routine_win_is_undecorated(monkeypatch):
    # Favorites (team B, display 1738s) win: no flair, no odds anywhere
    _patch_stats(monkeypatch, {"4": STRONG, "5": STRONG, "6": STRONG})
    title, description, _ = await utils.determine_draft_outcome(
        _Bot(), _session(), team_a_wins=2, team_b_wins=5, half_matches=3, total_matches=7
    )
    assert "UPSET" not in title
    # NB: don't assert on ":1" — the Draft Start Discord timestamp contains it
    assert "underdog" not in description and "odds" not in description


@pytest.mark.asyncio
async def test_unrated_session_type_skips_stats_fetch(monkeypatch):
    calls = []

    async def recording_fetch(guild_id, player_ids):
        calls.append((guild_id, player_ids))
        return {}

    monkeypatch.setattr(utils, "_fetch_player_stats_map", recording_fetch)
    title, _, _ = await utils.determine_draft_outcome(
        _Bot(), _session(session_type="winston"), team_a_wins=5, team_b_wins=2,
        half_matches=3, total_matches=7,
    )
    assert calls == []
    assert "UPSET" not in title


@pytest.mark.asyncio
async def test_stats_failure_still_posts_plain_victory(monkeypatch):
    async def broken_fetch(guild_id, player_ids):
        raise RuntimeError("db down")
    monkeypatch.setattr(utils, "_fetch_player_stats_map", broken_fetch)
    title, description, _ = await utils.determine_draft_outcome(
        _Bot(), _session(), team_a_wins=5, team_b_wins=2, half_matches=3, total_matches=7
    )
    assert title.startswith("Congratulations to P1, P2, P3")
    assert "UPSET" not in title


@pytest.mark.asyncio
async def test_premade_upset_win_decorates_team_name_title(monkeypatch):
    # Team A (priors, display 1500) beats team B (display 1738s) in a premade
    # match: winner prob ~0.20 -> legendary. Premade titles are built from
    # team_a_name/team_b_name, not player names.
    _patch_stats(monkeypatch, {"4": STRONG, "5": STRONG, "6": STRONG})
    title, description, _ = await utils.determine_draft_outcome(
        _Bot(), _session(session_type="premade"),
        team_a_wins=5, team_b_wins=2, half_matches=3, total_matches=7,
    )
    assert title == "🌟 LEGENDARY UPSET — Alpha has won the match!"
    assert "~4:1 odds" in description


@pytest.mark.asyncio
async def test_draw_gets_no_decoration(monkeypatch):
    _patch_stats(monkeypatch, {"4": STRONG, "5": STRONG, "6": STRONG})
    title, _, _ = await utils.determine_draft_outcome(
        _Bot(), _session(), team_a_wins=3, team_b_wins=3, half_matches=3, total_matches=6
    )
    assert title == "The Draft is a Draw!"
