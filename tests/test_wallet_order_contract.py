"""The keys the wallet cog reads are the keys the service returns.

A rebase once left the whole order layer defined twice in the service, and the
copy Python bound was the older one -- which returned `credited`/`delivered`
where the cog had moved on to `moved`. Every deposit and every withdraw raised
KeyError inside a background task, so the player got the opening message and
then silence.

The suite stayed green throughout: the service tests asserted the old keys and
the cog tests mocked the service away, so nothing ever crossed the boundary
where the two have to agree. These tests are that crossing.
"""
import ast
import inspect
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from services import mtgo_resolution_service as resolution


def test_the_service_defines_each_order_function_once():
    """A duplicate definition is silent: the later one simply wins, and which
    one that is depends on edit order rather than on intent."""
    tree = ast.parse(inspect.getsource(resolution))
    seen = defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seen[node.name].append(node.lineno)

    dupes = {name: lines for name, lines in seen.items() if len(lines) > 1}
    assert not dupes, f"shadowed definitions: {dupes}"


@pytest.mark.asyncio
async def test_a_deposit_order_returns_what_the_cog_reads(test_db, monkeypatch):  # noqa: F811
    """The contract itself, driven end to end rather than asserted twice."""
    monkeypatch.setenv("MTGO_MAX_CARDS_PER_TRADE", "300")

    async def free():
        return None
    monkeypatch.setattr(resolution, "serve_busy_reason", free)

    async def started(g, p, u, n, **kw):
        return {"ok": True, "job_id": "job-1"}

    async def finished(job_id, g, p, n, u):
        return {"ok": True}

    monkeypatch.setattr(resolution, "start_deposit", started)
    monkeypatch.setattr(resolution, "finish_deposit", finished)

    res = await resolution.run_deposit_order("g1", "p1", "Someone", 50)

    assert res["moved"] == 50
    assert set(res) >= {"moved", "jobs", "error", "busy", "pending"}


@pytest.mark.asyncio
async def test_the_deposit_command_survives_a_completed_order(test_db, monkeypatch):  # noqa: F811
    """Drives the cog's own follow-up against the real service shape.

    The boundary is the point: mocking `run_deposit_order` here would restore
    exactly the blind spot that let a KeyError ship.
    """
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    cog.bot = MagicMock()
    ctx = MagicMock()
    ctx.guild.id, ctx.author.id = 1, 2
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    async def order(*a, **kw):
        # Exactly what _run_order returns for a clean single-trade order.
        return {"moved": 5, "jobs": ["job-1"], "error": None,
                "busy": False, "pending": False}

    # spawn_followup is called synchronously and schedules the coroutine, so
    # the stand-in has to be sync too -- an async one is simply never awaited,
    # and the follow-up this test is about never runs.
    captured: "list" = []

    def capture(label, coro):
        captured.append(coro)

    with patch("cogs.wallet_cog.gate_serve", return_value=None), \
         patch("cogs.wallet_cog.linked_username", new=AsyncMock(return_value="me")), \
         patch("cogs.wallet_cog.custodian_name", new=AsyncMock(return_value="TheCustodian")), \
         patch("cogs.wallet_cog.resolution.run_deposit_order", new=order), \
         patch("cogs.wallet_cog.resolution.settle_deposit_inflow",
               new=AsyncMock(return_value=([], []))), \
         patch("cogs.wallet_cog.wallet_service.get_balance",
               new=AsyncMock(return_value=5)), \
         patch("cogs.wallet_cog.refresh_boards", new=AsyncMock()), \
         patch("cogs.wallet_cog.escrow.open_boards_for_captain",
               new=AsyncMock(return_value=[])), \
         patch("cogs.wallet_cog.spawn_followup", new=capture):
        try:
            await WalletCommands.wallet_deposit.callback(cog, ctx, 5)
        except KeyError as exc:                      # the failure this pins
            pytest.fail(f"the cog read a key the service does not return: {exc}")

        assert captured, "the command scheduled no follow-up"
        try:
            await captured[0]
        except KeyError as exc:
            pytest.fail(f"the cog read a key the service does not return: {exc}")

    sent = [c.args[0] for c in ctx.followup.send.await_args_list]
    assert any("Deposit confirmed" in m for m in sent), sent
