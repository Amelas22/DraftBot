"""A trade the serve has forgotten is not the same as a trade that never ran.

The serve keeps its job list in memory. A restart loses it, and every job it
was tracking answers 404 forever after. Reading that as "the trade failed" is
the ledger asserting nothing crossed -- and for a deposit that costs a player
their own cube: it sits in the library's MTGO account with no record that they
are owed it back, so /mydeposits shows nothing and /withdraw cannot fetch it.

The boundary still knows what it is holding, so it is asked before anything is
written off.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import test_db  # noqa: F401  (fixture)
import services.card_deposit_service as svc

pytestmark = pytest.mark.asyncio

JOB = SimpleNamespace(job_id="job-gone", player_id="u1", mtgo_user="Someone",
                      library_id="lib", kind="card-deposit")


def _serve(*, positions, job=None):
    return SimpleNamespace(
        get_job=AsyncMock(return_value=job if job is not None else {"_missing": True}),
        positions=AsyncMock(return_value=positions),
    )


async def test_a_vanished_job_is_failed_when_the_serve_holds_nothing(test_db, monkeypatch):  # noqa: F811
    """Nothing crossed, so writing it off is the truth and the player is free
    to try again."""
    resolved = {}
    monkeypatch.setattr(svc, "_resolve", AsyncMock(
        side_effect=lambda jid, st: resolved.update({jid: st})))
    settled = {}

    await svc._settle_one(_serve(positions={"held": [], "lent": []}), JOB, settled)

    assert resolved == {"job-gone": "failed"}
    assert settled["job-gone"]["state"] == "failed"


async def test_a_vanished_job_is_left_alone_when_the_serve_still_holds_cards(test_db, monkeypatch):  # noqa: F811
    """The cards are on the far side of the boundary. Writing "failed" here is
    what loses them -- the row is the only thing that would ever make the
    library give them back."""
    resolved = {}
    monkeypatch.setattr(svc, "_resolve", AsyncMock(
        side_effect=lambda jid, st: resolved.update({jid: st})))
    settled = {}

    await svc._settle_one(
        _serve(positions={"held": [{"card": "Black Lotus", "qty": 1}], "lent": []}),
        JOB, settled)

    assert resolved == {}, "nothing may be written off while cards are held"
    assert settled == {}, "and the job stays pending for a human"


async def test_a_serve_that_cannot_be_asked_is_treated_as_holding(test_db, monkeypatch):  # noqa: F811
    """The whole point is to stop guessing. A serve that will not answer is the
    case with the least information, not a licence to assume the best."""
    resolved = {}
    monkeypatch.setattr(svc, "_resolve", AsyncMock(
        side_effect=lambda jid, st: resolved.update({jid: st})))
    client = _serve(positions=None)
    client.positions = AsyncMock(side_effect=RuntimeError("serve unreachable"))
    settled = {}

    await svc._settle_one(client, JOB, settled)

    assert resolved == {}
    assert settled == {}


async def test_a_job_the_serve_still_knows_about_is_untouched(test_db, monkeypatch):  # noqa: F811
    """Only a VANISHED job takes this path. One that is merely still running
    must not be reconciled against positions at all."""
    client = _serve(positions={"held": []}, job={"state": "running"})
    settled = {}

    await svc._settle_one(client, JOB, settled)

    assert settled == {}
    client.positions.assert_not_awaited(), "a running job is not a reconciliation"
