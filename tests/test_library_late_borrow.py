"""Asking for a library deck after the draft.

`/library borrow` and the panel's Borrow button used to answer "no deck waiting"
to anybody the library had not handed a deck when the draft ended. Now a
drafter with nothing waiting asks for their pool there and then
(draft_deck_assignment.assign_on_request) and, if it is handed over, collects it
in the same press.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_library_commands import _run

pytestmark = pytest.mark.asyncio

LOAN = SimpleNamespace(cards=[{"name": "Swamp", "qty": 7}], job_id="j1",
                       state="assigned", id=1, library_id=None)


async def test_a_drafter_with_nothing_waiting_is_handed_their_deck_and_collects_it(
        monkeypatch):
    import services.library_loan_actions as mod

    said = await _run(monkeypatch, "borrow", "dispatched",
                      draft="s1", asks="assigned", handed=LOAN)

    mod.assign_on_request.assert_awaited_once()
    assert mod.assign_on_request.await_args.args[0] == "s1"
    assert "ready to collect" in said, said


@pytest.mark.parametrize("outcome,text", [
    ("over", "matches in that draft are done"),
    ("busy", "/library return"),
    ("already", "already had"),
    ("not_offered", "doesn't lend"),
    ("no_log", "try again"),
    ("empty", "ask an admin"),
])
async def test_why_a_request_was_refused_is_said(monkeypatch, outcome, text):
    import services.library_loan_actions as mod

    said = await _run(monkeypatch, "borrow", "dispatched", draft="s1", asks=outcome)

    assert text.lower() in said.lower(), said
    mod.borrow_when_free.assert_not_awaited()


async def test_somebody_in_no_recent_draft_hears_what_they_always_did(monkeypatch):
    import services.library_loan_actions as mod

    said = await _run(monkeypatch, "borrow", "no_loan", draft=None)

    assert "no deck" in said.lower(), said
    mod.assign_on_request.assert_not_awaited()


async def test_the_gate_answers_in_its_own_words(monkeypatch):
    import services.library_loan_actions as mod

    monkeypatch.setattr(mod, "why_not", AsyncMock(return_value="🔒 3+ drafts here"))

    said = await _run(monkeypatch, "borrow", "dispatched", draft="s1",
                      asks="not_allowed")

    assert "3+ drafts here" in said, said
