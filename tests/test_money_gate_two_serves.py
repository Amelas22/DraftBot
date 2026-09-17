"""These helpers now serve two MTGO accounts, not one.

The wallet's custodian holds tix; the card library holds cards. Both are
TradeBot serves, both work one trade at a time, and both have a display name a
player needs in order to know whose trade window to accept. A helper hardcoded
to one of them tells a borrower waiting on the library to expect a trade from
the wallet's account, and reports the wrong serve's busy state.
"""
from unittest.mock import AsyncMock

import pytest

import helpers.money_gate as gate

pytestmark = pytest.mark.asyncio


def _client(custodian, ok=True, active=None, reconnecting=False):
    c = AsyncMock()
    c.enabled = True
    c.health = AsyncMock(return_value={"ok": ok, "custodian": custodian,
                                       "reconnecting": reconnecting})
    c.active_jobs = AsyncMock(return_value=active or [])
    return c


@pytest.fixture(autouse=True)
def _clear_cache():
    gate._custodian_cache = {}
    yield
    gate._custodian_cache = {}


async def test_each_serve_reports_its_own_custodian(monkeypatch):
    wallet, library = _client("Sealed01"), _client("Team01")
    monkeypatch.setattr(gate, "get_client", lambda: wallet)

    assert await gate.custodian_name() == "Sealed01"
    assert await gate.custodian_name(library) == "Team01"


async def test_one_serve_being_busy_does_not_block_the_other(monkeypatch):
    """The whole point of a second account: a wallet withdrawal in progress must
    not stop anyone borrowing a deck."""
    wallet = _client("Sealed01", active=[{"id": "j1"}])
    library = _client("Team01", active=[])
    monkeypatch.setattr(gate, "get_client", lambda: wallet)

    assert await gate.serve_busy_reason() is not None
    assert await gate.serve_busy_reason(library) is None


async def test_a_busy_library_says_which_account_is_busy(monkeypatch):
    library = _client("Team01", active=[{"id": "j1"}])
    monkeypatch.setattr(gate, "get_client", lambda: _client("Sealed01"))

    reason = await gate.serve_busy_reason(library)

    assert reason and "one person at a time" in reason


async def test_an_unreachable_serve_is_reported_as_such(monkeypatch):
    library = _client("Team01", ok=False)
    monkeypatch.setattr(gate, "get_client", lambda: _client("Sealed01"))

    reason = await gate.serve_busy_reason(library)

    assert reason and "nothing has been charged" in reason


async def test_the_cache_does_not_leak_one_name_onto_the_other(monkeypatch):
    """A single global cache would answer 'Sealed01' for the library forever
    after the first wallet call, which is exactly the bug worth preventing."""
    wallet, library = _client("Sealed01"), _client("Team01")
    monkeypatch.setattr(gate, "get_client", lambda: wallet)

    await gate.custodian_name()            # warms the wallet entry
    assert await gate.custodian_name(library) == "Team01"
    assert await gate.custodian_name() == "Sealed01"
