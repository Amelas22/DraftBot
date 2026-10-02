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
from services.entry_notices import announce_refunds, refund_message


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
        "s1", friendly_id="acidic-slime-74",
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
    await announce_refunds("s1", friendly_id="d",
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
    await announce_refunds("s1", friendly_id="d",
                           refunded={"p1": 50}, capped={"p1": 80},
                           held={"p1": 100})

    assert len(dm.sent) == 1, dm.sent
    msg = dm.sent[0][1]
    assert "130" in msg, f"the two refunds should be totalled: {msg}"
    assert "cap" in msg.lower(), "and the cap should still be named"


@pytest.mark.asyncio
async def test_nobody_is_told_when_nothing_came_back(dm):
    assert await announce_refunds("s1", friendly_id="d",
                                  refunded={}, capped={}, held={"p1": 100}) == 0
    assert dm.sent == []


@pytest.mark.asyncio
async def test_a_zero_refund_is_not_announced(dm):
    """_trim only records positive refunds, but a zero must never produce a DM
    saying 0 tix came back."""
    await announce_refunds("s1", friendly_id="d",
                           refunded={"p1": 0}, capped={}, held={"p1": 100})

    assert dm.sent == []


@pytest.mark.asyncio
async def test_an_undelivered_notice_is_not_counted(dm):
    """Same contract as everywhere else: send_dm returns False, it does not
    raise, and a caller that ignores the return over-reports."""
    dm.delivers = False

    assert await announce_refunds("s1", friendly_id="d",
                                  refunded={"p1": 200}, capped={},
                                  held={"p1": 100}) == 0


@pytest.mark.asyncio
async def test_no_bot_means_no_notices_and_no_error(monkeypatch):
    """Migrations, the CLI and most tests run with no bot registered."""
    import bot_registry
    monkeypatch.setattr(bot_registry, "get_bot", lambda: None)

    assert await announce_refunds("s1", friendly_id="d",
                                  refunded={"p1": 200}, capped={},
                                  held={"p1": 100}) == 0


@pytest.mark.asyncio
async def test_a_replayed_match_does_not_tell_anybody_twice(dm, test_db):
    """team_creator replays match_pool after a restart. The second pass finds
    the sides already equal and refunds nothing, so its refund dicts come back
    empty and nobody is told again -- which is what stops a restart looking like
    the money moved a second time.
    """
    import services.draft_pool_service as pool
    import services.wallet_service as wallet_service
    from conftest import seed_session, seed_stakes

    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["a1", "a2"], ["b1", "b2"]))
    for player, amount in {"a1": 100, "a2": 100, "b1": 400, "b2": 20}.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)
    await seed_stakes("s1", {"a1": (100, False), "a2": (100, False),
                             "b1": (400, True), "b2": (20, False)})

    # `held` comes from the result, which is how team_creator reads it -- so this
    # also pins the MatchResult key at the boundary that consumes it.
    first = await pool.match_pool("g", "s1", ["a1", "a2"], ["b1", "b2"])
    told_first = await announce_refunds(
        "s1", refunded=first["refunded"], capped=first["capped"],
        held=first["held"], friendly_id="d")

    second = await pool.match_pool("g", "s1", ["a1", "a2"], ["b1", "b2"])
    told_again = await announce_refunds(
        "s1", refunded=second["refunded"], capped=second["capped"],
        held=second["held"], friendly_id="d")

    assert told_first >= 1, f"the first pass told nobody: {first}"
    assert told_again == 0, (
        f"a replay told players their money moved again: {second}, {dm.sent}")

    # And the figure the DM asserts has to be the POST-trim one. b1 declared 400
    # beside a single 20, so its ceiling snaps to 20 and levelling leaves every
    # player on 20. Returning match_pool's opening snapshot instead would send
    # b1 "you are playing for 400 tix" after handing 380 of it back -- which is
    # the one way threading `held` through the result could go wrong silently.
    b1 = next(msg for who, msg in dm.sent if who == "b1")
    assert "playing for **20 tix**" in b1, b1
    assert "400" not in b1, f"that is the pre-trim holding: {b1}"


def test_a_refund_that_levelling_took_is_not_blamed_on_the_cap():
    """Both ceilings can bite the same entry, and only one is the player's own.

    Side B {400 capped, 200} against {100, 100}: the cap returns 160 and
    levelling returns a further 140. Reporting the combined 300 under the cap's
    name sends the player to a preference that would have returned none of it --
    with the cap off, that 400 still levels down to exactly 100. So a message
    that names only the cap is not merely imprecise, it is advice that does not
    work.
    """
    msg = refund_message(300, 100, 160, "acidic-slime-74")

    assert "160" in msg, f"the cap's share has to be named: {msg}"
    assert "140" in msg, f"levelling's share has to be named: {msg}"
    assert "300" in msg, f"the total still has to be there: {msg}"
    assert "100" in msg, f"and what they are playing for: {msg}"


def test_a_single_reason_is_still_reported_as_one_sentence():
    """The split only earns its words when both ceilings actually bit. A pure
    levelling refund, or a pure cap refund, reads as one plain reason."""
    levelled = refund_message(200, 100, 0, "d")
    assert "levelled" in levelled, levelled
    assert "cap" not in levelled.lower(), f"no cap applied here: {levelled}"

    all_cap = refund_message(80, 120, 80, "d")
    assert "cap" in all_cap.lower(), all_cap
    assert "levelled" not in all_cap, f"levelling took nothing here: {all_cap}"
