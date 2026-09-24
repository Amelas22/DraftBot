"""An order POST gets minutes; a read gets seconds.

The serve resolves every distinct card name against the MTGO collection before
it answers, so a POST's cost scales with NAMES, not cards. Measured against a
free serve: 25 names answered in 0.2s, 50 took 170s, and a cube chunk is ~300.
The session's 20-second default is right for reads and hopeless for those.

It matters more than a slow command. A timeout AFTER the connection is made is
reported as ambiguous -- the request may have landed and only the answer been
lost -- so the caller holds the depositor's cards and asks for a human. Every
full-cube deposit ended up there.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.mtgo_tradebot_client import ORDER_TIMEOUT_S, MtgoTradeBotClient


def _client_recording_timeouts():
    """A client whose session records the timeout each request was given."""
    client = MtgoTradeBotClient(url="http://serve", token="t")
    seen: "list" = []

    class _Resp:
        status = 202
        async def text(self): return '{"id": "job-1"}'
        async def json(self): return {"id": "job-1"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def request(method, url, **kw):
        seen.append(kw.get("timeout"))
        return _Resp()

    session = MagicMock()
    session.request = request
    client._get_session = lambda: session
    return client, seen


@pytest.mark.asyncio
async def test_an_order_post_is_given_the_long_budget():
    client, seen = _client_recording_timeouts()

    await client.deposit("someone", [{"name": "Swamp", "qty": 1}])

    assert seen and seen[0] is not None, "the order POST used the session default"
    assert seen[0].total == ORDER_TIMEOUT_S


@pytest.mark.asyncio
async def test_a_read_keeps_the_short_default():
    """A serve that cannot answer /health quickly is down, and finding that out
    fast is the whole point of asking."""
    client, seen = _client_recording_timeouts()

    await client.health()

    assert seen == [None], "a read must not inherit the order budget"


@pytest.mark.asyncio
async def test_every_order_creating_call_gets_it():
    """The set is exactly the calls that make a job on the serve -- the ones
    whose delivery matters, and so the ones flagged ambiguous on a timeout."""
    for call, args in (
        ("deposit", ("someone", [{"name": "Swamp", "qty": 1}])),
        ("borrow", ("someone", [{"name": "Swamp", "qty": 1}])),
        ("return_cards", ("someone",)),
        ("withdraw_cards", ("someone",)),
    ):
        client, seen = _client_recording_timeouts()
        await getattr(client, call)(*args)
        assert seen and seen[0] is not None and seen[0].total == ORDER_TIMEOUT_S, \
            f"{call} did not get the order budget"


def test_the_budget_clears_the_worst_measurement():
    """50 names took 170 seconds on a free serve and a chunk is six times that,
    so the budget is generous on purpose: waiting minutes for a slow answer is
    far cheaper than an ambiguous one, which strands cards and needs a human."""
    assert ORDER_TIMEOUT_S >= 300
