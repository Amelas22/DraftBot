"""Telling a player that part of their entry came back.

Four strings in the prize-pool copy promise money "comes straight back", "is
returned before the draft starts", "is returned immediately". Until this, nothing
told the player it had: draft_pool_service logged the refund and moved on, so
somebody who declared 300 and was levelled to 100 watched 200 reappear with no
message and nothing anywhere attributing it. A promise the person it was made to
cannot verify is worse than no promise.
"""
import pytest

import notification_service as ns
from services.entry_notices import announce_refunds


class _Recorder:
    """Answers the way send_dm does: True delivered, False not."""

    def __init__(self, delivers=True):
        self.sent = []
        self.delivers = delivers

    async def __call__(self, bot, user_id, message, label=None, view=None):
        self.sent.append((str(user_id), message))
        return self.delivers


@pytest.fixture
def dm(monkeypatch):
    import bot_registry
    rec = _Recorder()
    monkeypatch.setattr(bot_registry, "get_bot", lambda: object())
    monkeypatch.setattr(ns, "send_dm", rec)
    return rec


@pytest.mark.asyncio
async def test_a_levelled_player_is_told_what_came_back(dm):
    """The common case: their side was heavier, so the excess was returned."""
    sent = await announce_refunds(
        "g", "s1", friendly_id="acidic-slime-74",
        refunded={"p1": 200}, capped={}, held={"p1": 100})

    assert sent == 1, dm.sent
    who, msg = dm.sent[0]
    assert who == "p1"
    assert "200" in msg, msg
    assert "100" in msg, "it must say what they are actually playing for"


@pytest.mark.asyncio
async def test_the_reason_distinguishes_a_cap_from_levelling(dm):
    """Two different things happened to two players and each should be told
    which -- a cap is their own setting and levelling is not."""
    await announce_refunds("g", "s1", friendly_id="d",
                           refunded={"p1": 50}, capped={"p2": 80},
                           held={"p1": 100, "p2": 120})

    by_player = dict(dm.sent)
    assert "cap" in by_player["p2"].lower(), by_player["p2"]
    assert "cap" not in by_player["p1"].lower(), (
        f"a levelling refund was blamed on a cap: {by_player['p1']}")


@pytest.mark.asyncio
async def test_a_player_who_had_both_gets_one_message(dm):
    """Capping runs before levelling, so one player can be trimmed twice. Two
    DMs about one draft reads as a bug."""
    await announce_refunds("g", "s1", friendly_id="d",
                           refunded={"p1": 50}, capped={"p1": 80},
                           held={"p1": 100})

    assert len(dm.sent) == 1, dm.sent
    msg = dm.sent[0][1]
    assert "130" in msg, f"the two refunds should be totalled: {msg}"
    assert "cap" in msg.lower(), "and the cap should still be named"


@pytest.mark.asyncio
async def test_nobody_is_told_when_nothing_came_back(dm):
    assert await announce_refunds("g", "s1", friendly_id="d",
                                  refunded={}, capped={}, held={"p1": 100}) == 0
    assert dm.sent == []


@pytest.mark.asyncio
async def test_a_zero_refund_is_not_announced(dm):
    """_trim only records positive refunds, but a zero must never produce a DM
    saying 0 tix came back."""
    await announce_refunds("g", "s1", friendly_id="d",
                           refunded={"p1": 0}, capped={}, held={"p1": 100})

    assert dm.sent == []


@pytest.mark.asyncio
async def test_an_undelivered_notice_is_not_counted(dm):
    """Same contract as everywhere else: send_dm returns False, it does not
    raise, and a caller that ignores the return over-reports."""
    dm.delivers = False

    assert await announce_refunds("g", "s1", friendly_id="d",
                                  refunded={"p1": 200}, capped={},
                                  held={"p1": 100}) == 0


@pytest.mark.asyncio
async def test_no_bot_means_no_notices_and_no_error(monkeypatch):
    """Migrations, the CLI and most tests run with no bot registered."""
    import bot_registry
    monkeypatch.setattr(bot_registry, "get_bot", lambda: None)

    assert await announce_refunds("g", "s1", friendly_id="d",
                                  refunded={"p1": 200}, capped={},
                                  held={"p1": 100}) == 0
