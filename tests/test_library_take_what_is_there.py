"""The choice /library borrow offers when the shelf cannot cover the deck.

The view is shown by the borrow flow and calls back into it, so the wiring
between the two is the thing to pin: a click on "take" has to start the same
borrow for the same player in the same server, carrying exactly the subset the
click agreed to -- and a click on "cancel" has to start nothing.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import stub_library

import library_views.take_what_is_there as view_mod
import services.library_loan_actions as actions
from library_views.callers import ContextCaller

pytestmark = pytest.mark.asyncio

DECK = [{"name": "Swamp", "qty": 7}, {"name": "Ghostly Wings", "qty": 1}]
SHORT = [{"name": "Ghostly Wings", "want": 1, "have": 0}]
TRIMMED = [{"name": "Swamp", "qty": 7}]


def _ctx():
    return SimpleNamespace(
        author=SimpleNamespace(id=1234),
        guild=SimpleNamespace(id=99), guild_id=99,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()))


def _click():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


@pytest.fixture
def offered(monkeypatch):
    """Run /library borrow into the shortfall, and hand back the view it sent.

    `detached` collects every followup either side spawns, so a test can drive
    the borrow a click starts -- or see that none was started.
    """
    detached: list = []

    async def run():
        monkeypatch.setattr(actions, "unusable_reason", lambda guild_id: None)
        stub_library(monkeypatch, actions)
        monkeypatch.setattr(actions, "active_loan", AsyncMock(return_value=SimpleNamespace(
            id=1, library_id="lib", state="assigned", cards=DECK)))
        monkeypatch.setattr(actions, "may_borrow", AsyncMock(return_value=True))
        monkeypatch.setattr(actions, "shortfall", AsyncMock(return_value=SHORT))
        # A status that ends the detached trade at once: what matters is how
        # it was asked for, not what the serve then does.
        monkeypatch.setattr(actions, "borrow_when_free",
                            AsyncMock(return_value=("no_loan", False)))
        monkeypatch.setattr(view_mod, "trim_to_available", AsyncMock(return_value=TRIMMED))
        for mod in (actions, view_mod):
            monkeypatch.setattr(mod, "spawn_followup",
                                lambda label, coro: detached.append(coro))

        ctx = _ctx()
        await actions.borrow(ContextCaller(ctx))
        view = ctx.followup.send.await_args.kwargs["view"]
        assert isinstance(view, view_mod.TakeWhatIsThereView)
        assert detached == [], "nothing is borrowed until the player chooses"
        return view, detached

    return run


async def test_taking_what_is_there_borrows_exactly_the_trimmed_deck(offered):
    """Fails if the view's take stops handing `trimmed` to its `collect`
    (e.g. `self.collect([])`, or re-reading the loan's full cards), or if the
    flow's `collect` drops `offering` on the way to `_borrow_when_ready`, or
    if either passes a different guild or player -- each of those borrows
    something other than what the click agreed to."""
    view, detached = await offered()
    click = _click()

    await view.take.callback(click)
    for coro in detached:
        await coro

    view_mod.trim_to_available.assert_awaited_once_with(99, 1)
    actions.borrow_when_free.assert_awaited_once_with(99, 1234, offering=TRIMMED)


async def test_cancelling_borrows_nothing(offered):
    """Fails if cancel starts a borrow -- e.g. spawning `self.collect(...)` the
    way take does -- or stops answering the click."""
    view, detached = await offered()
    click = _click()

    await view.cancel.callback(click)

    assert detached == []
    actions.borrow_when_free.assert_not_awaited()
    view_mod.trim_to_available.assert_not_awaited()
    click.response.send_message.assert_awaited_once()
