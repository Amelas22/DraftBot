"""The busy gate must read the JOB LIST, not /health's lifetime `jobs` counter.

Regression: the serve reports jobs:1 forever after the first trade (terminal jobs are
counted too), which wedged every deposit behind a permanent "custodian is busy".
"""
from unittest.mock import AsyncMock, patch

import pytest

from helpers import money_gate

HEALTHY = {"ok": True, "custodian": "Bot", "reconnecting": False, "queued": 0}


def _client(health, jobs):
    c = AsyncMock()
    c.health = AsyncMock(return_value=health)
    c._call = AsyncMock(return_value={"jobs": jobs})
    # active_jobs is the real implementation's filter, exercised via a stub listing
    async def active():
        return [j for j in jobs if j["state"] in ("queued", "running")]
    c.active_jobs = active
    return c


@pytest.mark.asyncio
async def test_terminal_jobs_do_not_make_the_custodian_busy():
    """A past failure (the live bug) must not block new trades."""
    client = _client({**HEALTHY, "jobs": 1},
                     [{"state": "failed", "id": "x", "detail": "could not resolve 'basic3'"}])
    with patch.object(money_gate, "get_client", return_value=client):
        assert await money_gate.serve_busy_reason() is None


@pytest.mark.asyncio
async def test_a_running_job_does_make_it_busy():
    client = _client({**HEALTHY, "jobs": 5}, [{"state": "running", "id": "y"}])
    with patch.object(money_gate, "get_client", return_value=client):
        reason = await money_gate.serve_busy_reason()
    assert reason and "busy with 1" in reason


@pytest.mark.asyncio
async def test_queued_counts_as_busy_and_unreachable_is_reported():
    client = _client({**HEALTHY, "jobs": 2}, [{"state": "queued", "id": "z"}])
    with patch.object(money_gate, "get_client", return_value=client):
        assert "busy" in (await money_gate.serve_busy_reason())

    down = _client(None, [])
    with patch.object(money_gate, "get_client", return_value=down):
        assert "isn't reachable" in (await money_gate.serve_busy_reason())

    reconnecting = _client({**HEALTHY, "reconnecting": True, "jobs": 0}, [])
    with patch.object(money_gate, "get_client", return_value=reconnecting):
        assert "reconnecting" in (await money_gate.serve_busy_reason())


def test_unresolvable_username_gets_an_actionable_hint():
    msg = money_gate.explain_trade_failure("could not resolve 'basic3' as an MTGO user")
    assert "/link_mtgo" in msg and "/mtgo_whoami" in msg


def test_cards_not_presented_gets_a_hint_and_other_errors_pass_through():
    assert "weren't added" in money_gate.explain_trade_failure(
        "not completed (cards not presented / took-from-us / guardrail)")
    assert money_gate.explain_trade_failure("some other problem") == "some other problem"
