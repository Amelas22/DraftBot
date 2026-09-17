"""Whether a server runs a card library, and what it holds against a deck.

Absence means the feature is OFF, not free. A guild that wants to lend without
a deposit says so with `collateral_tix: 0` and owns that decision; a guild that
has said nothing has not opted into lending its cards at all. Reading a missing
or unusable value as "charge nothing" invents a policy nobody chose, and the
thing it invents is the one that gives cards away.
"""
import pytest

from config import card_library_collateral, library_enabled


@pytest.fixture
def cfg(monkeypatch):
    def _set(features):
        monkeypatch.setattr("config.get_config", lambda gid: {"features": features})
    return _set


def test_a_server_sets_its_own_collateral(cfg):
    cfg({"card_library": {"enabled": True, "collateral_tix": 5}})
    assert card_library_collateral("g1") == 5
    assert library_enabled("g1")


def test_free_lending_has_to_be_asked_for_explicitly(cfg):
    """0 is a real answer -- but only when someone wrote it down."""
    cfg({"card_library": {"enabled": True, "collateral_tix": 0}})
    assert card_library_collateral("g1") == 0
    assert library_enabled("g1")


def test_a_server_that_never_configured_the_library_has_no_library(cfg):
    cfg({})
    assert card_library_collateral("g1") is None
    assert not library_enabled("g1")


def test_enabling_without_saying_what_to_hold_is_not_a_library(cfg):
    """The gap the old reading fell into: enabled, no amount, lend for free."""
    cfg({"card_library": {"enabled": True}})
    assert card_library_collateral("g1") is None
    assert not library_enabled("g1")


def test_a_disabled_library_is_off_whatever_else_it_says(cfg):
    cfg({"card_library": {"enabled": False, "collateral_tix": 5}})
    assert card_library_collateral("g1") is None
    assert not library_enabled("g1")


@pytest.mark.parametrize("bad", ["5", -5, None, 2.5, True])
def test_an_unusable_amount_switches_the_library_off_rather_than_free(cfg, bad):
    """A typo must not quietly drop the deposit a server asked for. Refusing to
    lend is recoverable; lending uncollateralised is not."""
    cfg({"card_library": {"enabled": True, "collateral_tix": bad}})
    assert card_library_collateral("g1") is None
    assert not library_enabled("g1")
