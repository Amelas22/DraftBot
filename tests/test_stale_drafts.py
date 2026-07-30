"""Tests for the /cleanup_stale_drafts selection policy (helpers/stale_drafts).

Regression context: the command once selected every session older than
`hours_old` — on a real guild that meant ~800 finished drafts (sitting at
session_stage='pairings' with a victory message posted) queued for
completion-as-draws and DB deletion. "Stale" must mean fired-but-abandoned.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from helpers.stale_drafts import LOOKBACK_DAYS, cleanup_window, is_finished_draft, is_stale_draft


def _session(stage="pairings", victory_chat=None, victory_results=None, teams_started=True):
    return SimpleNamespace(
        session_stage=stage,
        victory_message_id_draft_chat=victory_chat,
        victory_message_id_results_channel=victory_results,
        teams_start_time=datetime(2026, 7, 29, 15, 18) if teams_started else None,
    )


# ---- is_finished_draft ---------------------------------------------------------------

def test_completed_stage_is_finished():
    assert is_finished_draft(_session(stage="completed"))


def test_victory_message_is_finished_even_at_pairings_stage():
    # The common real-world shape: fully played draft, stage never advanced.
    assert is_finished_draft(_session(stage="pairings", victory_chat="123"))
    assert is_finished_draft(_session(stage="pairings", victory_results="456"))


def test_no_completion_marker_is_not_finished():
    assert not is_finished_draft(_session(stage="pairings"))


# ---- is_stale_draft ------------------------------------------------------------------

def test_fired_and_unfinished_is_stale():
    assert is_stale_draft(_session(stage="pairings"))


def test_finished_draft_is_never_stale():
    # The ~800-draft regression: played drafts at 'pairings' with a victory
    # message must not be selected.
    assert not is_stale_draft(_session(stage="pairings", victory_chat="123"))
    assert not is_stale_draft(_session(stage="completed"))


def test_never_fired_lobby_is_not_stale():
    # Queue-inactivity cleanup owns unfired lobbies, not this command.
    assert not is_stale_draft(_session(stage=None, teams_started=False))


# ---- cleanup_window ------------------------------------------------------------------

def test_cleanup_window_bounds():
    now = datetime(2026, 7, 30, 12, 0)
    floor, cutoff = cleanup_window(24, now=now)
    assert cutoff == now - timedelta(hours=24)
    assert floor == now - timedelta(days=LOOKBACK_DAYS)
    assert floor < cutoff


def test_cleanup_window_larger_than_lookback_selects_nothing():
    # hours_old beyond the lookback window inverts the range -> empty selection,
    # by design: anything that old is history, not cleanup.
    now = datetime(2026, 7, 30, 12, 0)
    floor, cutoff = cleanup_window(24 * (LOOKBACK_DAYS + 5), now=now)
    assert cutoff < floor
