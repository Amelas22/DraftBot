"""Tests for the results-channel helpers in utils.

Which channel a draft belongs in (results_channel_name_for), and resolving
that name against a guild that has duplicates of it (find_postable_results_channel).
"""
from types import SimpleNamespace

from utils import find_postable_results_channel, results_channel_name_for


class _Perms:
    def __init__(self, view=True, send=True):
        self.view_channel = view
        self.send_messages = send


class _Channel:
    def __init__(self, cid, name, perms):
        self.id = cid
        self.name = name
        self._perms = perms

    def permissions_for(self, _member):
        return self._perms


class _Guild:
    def __init__(self, channels):
        self.text_channels = channels
        self.me = object()


def test_single_postable_channel_is_returned():
    ch = _Channel(1, "league-draft-results", _Perms(view=True, send=True))
    guild = _Guild([ch])
    assert find_postable_results_channel(guild, "league-draft-results") is ch


def test_no_matching_name_returns_none():
    guild = _Guild([_Channel(1, "general", _Perms())])
    assert find_postable_results_channel(guild, "league-draft-results") is None


def test_prefers_postable_channel_among_duplicates():
    # First by position is the stale one the bot can't post in; correct one is second.
    stale = _Channel(1220, "league-draft-results", _Perms(view=False, send=False))
    live = _Channel(1518, "league-draft-results", _Perms(view=True, send=True))
    guild = _Guild([stale, live])
    assert find_postable_results_channel(guild, "league-draft-results") is live


def test_skips_channel_with_view_but_no_send():
    view_only = _Channel(1, "league-draft-results", _Perms(view=True, send=False))
    full = _Channel(2, "league-draft-results", _Perms(view=True, send=True))
    guild = _Guild([view_only, full])
    assert find_postable_results_channel(guild, "league-draft-results") is full


def test_falls_back_to_first_match_when_none_postable():
    a = _Channel(1, "league-draft-results", _Perms(view=False, send=False))
    b = _Channel(2, "league-draft-results", _Perms(view=True, send=False))
    guild = _Guild([a, b])
    # No postable channel exists; preserve prior behavior (first match).
    assert find_postable_results_channel(guild, "league-draft-results") is a


# ---- results_channel_name_for -------------------------------------------------------
# The rule is "did this draft record into a tournament", which is exactly
# tournament_match_id. These drafts deliberately carry NO session_type: the
# routing used to read it, and leaving the attribute off entirely means any
# return to that habit raises AttributeError here instead of passing silently.

def test_a_linked_draft_posts_to_the_league_channel():
    assert results_channel_name_for(
        SimpleNamespace(tournament_match_id=81)) == "league-draft-results"


def test_an_unlinked_draft_posts_to_the_normal_channel():
    # The case that used to land in the league channel: a premade draft played
    # outside the season -- named teams, no tournament match.
    assert results_channel_name_for(
        SimpleNamespace(tournament_match_id=None)) == "team-draft-results"
