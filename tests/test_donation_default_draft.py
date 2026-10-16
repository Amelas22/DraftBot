"""During a donation day, /draft opens a donation draft unless a manager asks otherwise."""
import pytest

from services.donation_day import resolve_draft_kind


@pytest.mark.parametrize("requested", [None, "donation"])
def test_anyone_gets_a_donation_draft_while_a_donation_day_runs(requested):
    assert resolve_draft_kind(requested, money_server=True, event_active=True,
                              is_manager=False) == ("donation", None)


def test_a_manager_can_still_open_a_staked_draft_during_a_donation_day():
    assert resolve_draft_kind("staked", money_server=True, event_active=True,
                              is_manager=True) == ("staked", None)


def test_a_player_cannot_opt_out_of_a_donation_draft_during_a_donation_day():
    kind, refusal = resolve_draft_kind("staked", money_server=True, event_active=True,
                                       is_manager=False)
    assert kind is None and refusal and "donation day" in refusal.lower()


@pytest.mark.parametrize("requested", [None, "staked"])
def test_outside_a_donation_day_the_default_is_staked(requested):
    assert resolve_draft_kind(requested, money_server=True, event_active=False,
                              is_manager=False) == ("staked", None)


def test_outside_a_donation_day_only_managers_open_donation_drafts():
    assert resolve_draft_kind("donation", money_server=True, event_active=False,
                              is_manager=True) == ("donation", None)
    kind, refusal = resolve_draft_kind("donation", money_server=True, event_active=False,
                                       is_manager=False)
    assert kind is None and refusal == "Only bot managers can open a donation draft."


def test_a_free_server_never_opens_a_donation_draft():
    assert resolve_draft_kind(None, money_server=False, event_active=True,
                              is_manager=True) == (None, None)
    kind, refusal = resolve_draft_kind("donation", money_server=False, event_active=False,
                                       is_manager=True)
    assert kind is None and refusal == "Donation drafts only run on money servers."
