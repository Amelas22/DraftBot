"""Tests for utils.find_postable_results_channel — duplicate-named channel handling."""
from utils import find_postable_results_channel


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
